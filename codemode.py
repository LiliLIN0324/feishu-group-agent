# 在飞书里直接改代码：消息以「改代码」「/code」「代码：」开头时，在本机指定仓库跑一次可写的无头 agent。
# 只对 write.allowed_users 开放。改动前后用 git 对象做对比，支持「/撤销」只退回本轮改过的文件。
import json, os, re, shutil, subprocess, threading, time

from providers.cursor import CursorProvider, agent_base_cmd

GIT_FALLBACKS = [
    os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Git", "cmd", "git.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\cmd\git.exe"),
    os.path.expandvars(r"%USERPROFILE%\.qoder\bin\git\mingw64\bin\git.exe"),
]


def find_git():
    g = shutil.which("git")
    if g: return g
    for p in GIT_FALLBACKS:
        if os.path.isfile(p): return p
    return "git"


def find_codex():
    env = os.environ.get("AGENTBOT_CODEX_CMD")
    if env: return json.loads(env)
    node = shutil.which("node")
    if node and node.lower().endswith((".cmd", ".bat", ".ps1")):
        alt = os.path.join(os.path.dirname(node), "node.exe")
        if os.path.isfile(alt): node = alt
    if not node: return None
    for js in (os.path.join(os.path.dirname(node), "node_modules", "@openai", "codex", "bin", "codex.js"),
               os.path.expandvars(r"%APPDATA%\npm\node_modules\@openai\codex\bin\codex.js")):
        if os.path.isfile(js): return [node, js]
    return None


QUOTA_RX = re.compile(r"usage limit|quota|credit|not logged in|unauthorized|ActionRequiredError", re.I)

PROMPT = """你在一个真实项目仓库里干活，工作区：{ws}

任务：
{task}

必须遵守（违反会影响别人正在进行的工作）：
1. 先读仓库根目录的 COLLAB.md（如果存在），严格照它执行：标注「独占」「禁改」的文件和区域不要动；确实需要动就停下来，在总结里说明原因。
2. 这个仓库里可能已经有别人未提交的改动。不要 checkout / reset / clean / stash，不要 git commit，不要 git push，不要新建分支或标签。
3. 只改完成本任务必需的文件；不要顺手重构、整文件格式化、删除无关文件，不要改 .env 或任何密钥文件。
4. 改完做最小验证（只跑与改动直接相关的一项检查）；超过 5 分钟没跑完就停掉并说明原因。
5. 最后用中文写一段不超过 500 字的总结：改了什么、涉及哪些文件、验证结果、风险。不要贴大段代码或完整 diff。"""


class CodeMode:
    def __init__(self, cfg, base, state_dir, log, dry=False):
        self.cfg = cfg
        self.c = cfg.get("code") or {}
        self.base = base
        self.log = log
        self.state_dir = state_dir
        self.dry = dry
        self.git = find_git()
        self.lock = threading.Lock()
        self._cursor_dead_until = 0.0
        self.last_engine = ""
        self.rx = re.compile(r"^\s*(?:/code|改代码|代码[:：])\s*([\s\S]*)$", re.I)
        self.undo_rx = re.compile(r"^\s*(?:/撤销|撤销刚才的改动|撤销上次改动|退回刚才的改动)\s*$")
        self._provider = None
        if self.workspace:
            agent = dict(cfg.get("agent") or {})
            agent["max_runtime_sec"] = self.c.get("max_runtime_sec", 1200)
            agent["stall_sec"] = self.c.get("stall_sec", 300)
            self._provider = CursorProvider({"agent": agent}, log, self.workspace)

    @property
    def workspace(self):
        ws = str(self.c.get("workspace") or "").strip()
        if not ws: return ""
        ws = os.path.expanduser(os.path.expandvars(ws))
        return os.path.abspath(ws if os.path.isabs(ws) else os.path.join(self.base, ws))

    def enabled(self):
        return bool(self.c.get("enabled")) and bool(self.workspace)

    def match(self, text):
        if not self.enabled(): return None
        m = self.rx.match(text or "")
        if not m: return None
        return m.group(1).strip()[: int(self.c.get("max_task_chars", 4000))]

    def is_undo(self, text):
        return self.enabled() and bool(self.undo_rx.match(text or ""))

    def allowed(self, uid):
        users = self.c.get("allowed_users")
        if not isinstance(users, dict) or not users:
            users = (self.cfg.get("write") or {}).get("allowed_users") or {}
        return uid in users

    def _rehearse(self):
        # 和飞书写入一样：演练默认不真的动手，验收时才用环境变量打开。
        return self.dry and os.environ.get("AGENTBOT_DRY_LIVE_WRITES") != "1"

    # ---------- git ----------
    def _git(self, *args, timeout=None):
        timeout = timeout or self.c.get("git_timeout_sec", 120)
        p = subprocess.run([self.git, "-c", "core.quotepath=false", "-C", self.workspace] + list(args),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")

    def _z_paths(self, *args):
        rc, out, err = self._git(*(list(args) + ["-z"]))
        if rc != 0:
            self.log.warning("git %s failed: %s", " ".join(args), err.strip()[:200])
            return []
        return [p for p in out.split("\0") if p]

    def _snapshot(self):
        # stash create 只生成一个提交对象，不动工作区和暂存区，也不出现在 stash 列表里。
        rc, out, _ = self._git("stash", "create")
        snap = out.strip()
        if rc == 0 and snap: return snap
        _, out, _ = self._git("rev-parse", "HEAD")
        return out.strip()

    def _state_path(self):
        return os.path.join(self.state_dir, "code_last.json")

    # ---------- 跑一次 ----------
    def run(self, task, ctx, on_stage=None):
        ws = self.workspace
        if not os.path.isdir(os.path.join(ws, ".git")):
            return "工作区不可用：找不到 git 仓库。"
        if self._rehearse():
            return "演练模式不真的改代码。要实际执行，请在本机环境变量里设置 AGENTBOT_DRY_LIVE_WRITES=1。"
        if not self.lock.acquire(timeout=1):
            return "上一轮改代码还没结束，等它完成或超时后再发。"
        t0 = time.time()
        try:
            head0 = self._git("rev-parse", "HEAD")[1].strip()
            snap = self._snapshot()
            if not snap: return "这个仓库还没有任何提交，无法做改动快照，先手动提交一次再来。"
            before = set(self._z_paths("ls-files", "--others", "--exclude-standard"))
            if on_stage: on_stage("正在读代码、准备修改…")
            rc, out, err, why, st = self._run_engine(task, on_stage)
            after = set(self._z_paths("ls-files", "--others", "--exclude-standard"))
            new_files = sorted(after - before)
            changed = self._z_paths("diff", "--name-only", snap)
            head1 = self._git("rev-parse", "HEAD")[1].strip()
            stats_rows, stats_short = self._diff_stats(snap, changed, new_files)
            diff_text, diff_cut = self._diff_text(snap)
            summary = (out or "").strip()[: int(self.c.get("max_chars", 2200))]
            secs = int(round(time.time() - t0))
            self._save({"ts": time.time(), "user": ctx.get("user"), "chat": ctx.get("chat_id"),
                        "task": task, "snapshot": snap, "head": head0,
                        "paths": changed, "new_files": new_files, "summary": summary})
            self.log.info("code done user=%s rc=%s changed=%d new=%d why=%s %.0fs",
                          ctx.get("user"), rc, len(changed), len(new_files), why or "-", secs)
            return self._compose(summary, stats_rows, stats_short, diff_text, diff_cut, why, err, secs,
                                 head0 != head1 and bool(head1), self.last_engine)
        except subprocess.TimeoutExpired:
            return "git 操作超时，这次没有拿到结果。仓库里可能已经有一部分改动，回「/撤销」可以退回。"
        finally:
            self.lock.release()

    def _run_engine(self, task, on_stage):
        names = [str(x).lower() for x in (self.c.get("engines") or ["cursor"])]
        errs = []
        for name in names:
            if name == "cursor":
                if time.time() < self._cursor_dead_until:
                    errs.append("cursor：额度用完，跳过"); continue
                res = self._run_cursor(task, on_stage)
            elif name == "codex":
                res = self._run_codex(task, on_stage)
            else:
                errs.append("%s：未知引擎" % name); continue
            rc, out, err, why, st = res
            self.last_engine = name
            if why:
                if st is not None and not out: out = (st.partial_text() or "").strip() or None
                return rc, out, err, why, st
            if out:
                return res
            if name == "cursor" and QUOTA_RX.search(err or ""):
                cooldown = float(self.c.get("cursor_cooldown_sec", 21600))
                self._cursor_dead_until = time.time() + cooldown
                self.log.warning("code mode: cursor 额度不可用，%.1f 小时内改用下一个引擎", cooldown / 3600)
            errs.append("%s：%s" % (name, (err or "").strip()[:160] or "没有返回内容"))
        self.last_engine = ""
        return -1, None, "；".join(errs), "", None

    def _run_cursor(self, task, on_stage):
        model = (self.c.get("model") or (self.cfg.get("agent") or {}).get("model") or "").strip()
        cmd = list(agent_base_cmd()) + ["-p", "--force", "--trust",
                                        "--output-format", "stream-json", "--stream-partial-output",
                                        "--workspace", self.workspace]
        if model: cmd += ["--model", model]
        cmd += [PROMPT.format(ws=self.workspace, task=task)]
        return self._provider.stream_agent(cmd, on_stage)

    def _run_codex(self, task, on_stage):
        base = find_codex()
        if not base: return -1, None, "找不到 codex 命令（可用 AGENTBOT_CODEX_CMD 指定）", "", None
        outfile = os.path.join(self.state_dir, "code_last_message.txt")
        try: os.remove(outfile)
        except OSError: pass
        max_rt = float(self.c.get("max_runtime_sec", 1200))
        cmd = base + ["exec", "--cd", self.workspace, "-o", outfile, "-c", 'approval_policy="never"']
        sandbox = str(self.c.get("codex_sandbox", "workspace-write") or "").strip()
        if sandbox: cmd += ["--sandbox", sandbox]
        model = str(self.c.get("codex_model") or "").strip()
        if model: cmd += ["-m", model]
        cmd += [PROMPT.format(ws=self.workspace, task=task)]
        if on_stage: on_stage("正在改代码…")
        try:
            p = subprocess.run(cmd, cwd=self.workspace, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=max_rt)
        except subprocess.TimeoutExpired:
            return -1, None, "", "max_runtime %.0fs" % max_rt, None
        out = ""
        if os.path.isfile(outfile):
            try: out = open(outfile, encoding="utf-8", errors="replace").read().strip()
            except OSError: pass
        if not out: out = (p.stdout or "").strip()[-2000:]
        self.log.info("codex exec rc=%s out_chars=%d err=%s", p.returncode, len(out), (p.stderr or "")[:200])
        return p.returncode, (out or None), (p.stderr or ""), "", None

    # ---------- 结果整理 ----------
    def _diff_stats(self, snap, changed, new_files):
        rc, out, _ = self._git("diff", "--numstat", snap)
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3: continue
            a, d, p = parts[0], parts[1], parts[2]
            if p not in changed: continue
            rows.append("M %s  +%s −%s" % (p, a, d) if a != "-" else "M %s  （二进制）" % p)
        rows += ["A %s  （新建）" % p for p in new_files]
        rc, short, _ = self._git("diff", "--shortstat", snap)
        short = short.strip().replace(",", "，")
        if new_files: short = (short + "；" if short else "") + "新建 %d 个文件" % len(new_files)
        rows_text = "\n".join(rows[:25]) + (("\n…还有 %d 个文件" % (len(rows) - 25)) if len(rows) > 25 else "")
        return rows_text, (short or "没有文件改动")

    def _diff_text(self, snap):
        rc, out, _ = self._git("diff", "--unified=1", snap)
        lines = out.splitlines()
        cap = int(self.c.get("diff_max_lines", 120))
        cut = len(lines) > cap
        text = "\n".join(lines[:cap])
        if len(text) > 4000:
            text = text[:4000]; cut = True
        return text.strip(), cut

    def _compose(self, summary, stats_rows, stats_short, diff_text, diff_cut, why, err, secs, head_moved, engine=""):
        head = "改到一半被停了（%s）" % why if why else "改完了"
        line = "%s · 用时 %d 分 %d 秒" % (head, secs // 60, secs % 60)
        if engine: line += " · %s" % engine
        parts = [line]
        parts.append(summary or "（agent 没有返回文字总结）")
        if err and not summary:
            parts.append("运行信息：" + err.strip()[-300:])
        parts.append("【改动】" + stats_short + ("\n" + stats_rows if stats_rows else ""))
        if head_moved:
            parts.append("注意：仓库 HEAD 变了（agent 可能自己提交了），自动撤销不可用，请手动检查。")
        if diff_text:
            parts.append("【diff】\n" + diff_text + ("\n…diff 太长，只显示前 %d 行" % int(self.c.get("diff_max_lines", 120)) if diff_cut else ""))
        parts.append("要退回这次改动，回「/撤销」。")
        return "\n\n".join(parts)[:6000]

    # ---------- 撤销 ----------
    def undo(self, uid):
        if self._rehearse():
            return "演练模式不真的退回文件。要实际执行，请在本机环境变量里设置 AGENTBOT_DRY_LIVE_WRITES=1。"
        p = self._state_path()
        if not os.path.isfile(p): return "没有可撤销的改动记录。"
        try: rec = json.load(open(p, encoding="utf-8"))
        except Exception: return "撤销记录损坏，没法自动退回。"
        ttl = int(self.c.get("undo_ttl_sec", 7200))
        if time.time() - float(rec.get("ts") or 0) > ttl:
            return "上次改动已经超过 %d 小时，自动撤销记录失效，需要手动用 git 回退。" % (ttl // 3600)
        if rec.get("user") != uid: return "只有发起那次改动的人可以撤销。"
        head_now = self._git("rev-parse", "HEAD")[1].strip()
        if head_now != rec.get("head"):
            return "仓库在那次改动之后多了提交，自动撤销不安全，请手动处理。"
        snap = rec.get("snapshot") or rec.get("head")
        done = 0
        for path in rec.get("paths") or []:
            if not self._safe(path): continue
            rc, _, _ = self._git("cat-file", "-e", "%s:%s" % (snap, path))
            if rc == 0:
                rc, _, err = self._git("checkout", snap, "--", path)
                if rc == 0: done += 1
                else: self.log.warning("undo checkout %s: %s", path, err.strip()[:200])
            else:
                fp = os.path.join(self.workspace, path)
                if os.path.isfile(fp): os.remove(fp); done += 1
        for path in rec.get("new_files") or []:
            if not self._safe(path): continue
            fp = os.path.join(self.workspace, path)
            if os.path.isfile(fp): os.remove(fp); done += 1
        try: os.remove(p)
        except OSError: pass
        self.log.info("code undo user=%s files=%d", uid, done)
        return "已退回上次改动的 %d 个文件。改之前就有的未提交改动没有被碰。" % done

    @staticmethod
    def _safe(p):
        p = (p or "").replace("\\", "/")
        if not p or p.startswith("/") or re.match(r"^[a-zA-Z]:", p): return False
        return ".." not in p.split("/")

    def _save(self, rec):
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = self._state_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f: json.dump(rec, f, ensure_ascii=False)
            os.replace(tmp, self._state_path())
        except OSError as e:
            self.log.warning("save code state failed: %s", e)
