# 测试用假「改代码」引擎：按提示词在仓库里改两个文件，再按 cursor 或 codex 的格式回一句总结。
import json, os, sys, time

args = sys.argv[1:]
prompt = args[-1] if args else ""
ws = os.getcwd()
for flag in ("--workspace", "--cd"):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args): ws = args[i + 1]
outfile = ""
if "-o" in args:
    i = args.index("-o")
    if i + 1 < len(args): outfile = args[i + 1]
codex = bool(outfile)


def apply_changes():
    p = os.path.join(ws, "a.txt")
    with open(p, "a", encoding="utf-8") as f:
        f.write("line from agent\n")
    with open(os.path.join(ws, "new_by_agent.txt"), "w", encoding="utf-8") as f:
        f.write("created by agent\n")


summary = "改好了：a.txt 追加一行，新建 new_by_agent.txt。验证：没跑测试。"

if "[[QUOTA]]" in prompt and not codex:
    sys.stderr.write("Weekly usage limit reached. It resets in 2 days.\n")
    sys.exit(1)

apply_changes()
if codex:
    with open(outfile, "w", encoding="utf-8") as f:
        f.write(summary)
    sys.exit(0)


def out(e):
    sys.stdout.write(json.dumps(e) + "\n")
    sys.stdout.flush()


sid = "fake-code-session"
out({"type": "system", "subtype": "init", "session_id": sid, "model": "fake"})
out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": summary}]},
     "session_id": sid, "timestamp_ms": int(time.time() * 1000)})
out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": summary}]},
     "session_id": sid})
out({"type": "result", "subtype": "success", "is_error": False, "result": summary, "session_id": sid})
