#!/usr/bin/env python3
"""Stand-in for `claude -p` in harness tests (AGENT_CLAUDE_BIN=tests/fake_claude.py).

Does what loop.md asks, instantly and without a model, driven by markers in the task text:
  [file PATH]   write the task's name as line 1 of PATH (two such tasks at once = merge conflict)
  [sleep N]     take N seconds (default 3), so agents overlap
  [fail]        do nothing, so the task is retried
It checks the task's box, rewrites NOTES.md, appends to TASKLOG.md and commits, and prints a
plausible stream-json transcript. Given the merge-conflict prompt, it merges and keeps both sides.
"""
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

prompt = sys.argv[sys.argv.index("-p") + 1]
sid = str(uuid.uuid4())
now = lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
git = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True)


def emit(**ev):
    print(json.dumps({**ev, "session_id": sid, "timestamp": now()}, separators=(",", ":")), flush=True)


def msg(n, text, tokens):
    emit(type="assistant", message={"id": f"msg_{n}", "role": "assistant", "usage": {"input_tokens": tokens},
                                    "content": [{"type": "thinking", "thinking": "x" * 400}, {"type": "text", "text": text}]})


emit(type="system", subtype="init", model="fake")
msg(1, "reading the task", 16000)

m = re.search(r"Run `git merge (\S+)`", prompt)
if m:
    r = git("merge", m.group(1))
    for f in git("diff", "--name-only", "--diff-filter=U").stdout.split():
        text = open(f).read()
        text = re.sub(r"^(<<<<<<<|=======|>>>>>>>).*\n", "", text, flags=re.M)
        open(f, "w").write(text)
        git("add", f)
    git("commit", "-q", "--no-edit")
    note = "resolved merge conflict"
else:
    m = re.search(r"## Your task \((\S+) line (\d+)\)\n(.+)", prompt)
    src, line, task = m.group(1), int(m.group(2)), m.group(3)
    time.sleep(float((re.search(r"\[sleep ([\d.]+)\]", task) or [0, 3])[1]))
    if "[fail]" in task:
        note = "failed on purpose"
    else:
        f = re.search(r"\[file (\S+)\]", task)
        if f:
            os.makedirs(os.path.dirname(f.group(1)) or ".", exist_ok=True)
            open(f.group(1), "w").write(task.split(":")[0] + "\n")
        lines = open(src).read().split("\n")
        lines[line - 1] = lines[line - 1].replace("- [ ]", "- [x]", 1)
        open(src, "w").write("\n".join(lines))
        name = task.split(":")[0]
        open("NOTES.md", "w").write(f"# Notes\nlast task: {name}\nworker: {os.environ.get('AGENT_WORKER')}\n")
        with open("TASKLOG.md", "a") as log:
            log.write(f"- {name}\n  - Goal: {name}\n  - Approach: fake\n  - Blockers: none\n")
        if os.path.isdir("shared"):   # the .agent-shared directory, visible to every agent
            with open("shared/seen", "a") as s:
                s.write(f"{os.environ.get('AGENT_WORKER')} {name}\n")
        git("add", "-A")
        git("commit", "-q", "-m", f"fake: {name}")
        note = f"done: {name}"

msg(2, note, 17000)
emit(type="result", subtype="success", is_error=False, num_turns=2, duration_ms=1000)
