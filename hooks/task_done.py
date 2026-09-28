#!/usr/bin/env python3
"""PostToolUse hook: once the run's own task is checked off, keep the agent from starting another one.

The harness passes the task line in AGENT_TASK and its file in AGENT_TASK_SRC. After every tool call,
if that line reads `- [x]`, the agent is told (exit 2: stderr goes to the model) to finish only the
wrap-up steps and end the run. This survives compaction, which can summarize away "do only that task".
Without AGENT_TASK (chat, merge-conflict runs) it does nothing.
"""
import json
import os
import subprocess
import sys


def main() -> int:
    task, src = os.environ.get("AGENT_TASK", "").strip(), os.environ.get("AGENT_TASK_SRC", "TODO.md")
    if not task:
        return 0
    try:
        data = json.load(sys.stdin)
        cwd = data.get("cwd") or "/work"
        lines = open(os.path.join(cwd, src), errors="replace").read().splitlines()
    except (OSError, ValueError):
        return 0
    if not any(l.strip().lower() == f"- [x] {task}".lower() for l in lines):
        return 0
    clean = subprocess.run(["git", "-C", cwd, "status", "--porcelain"], capture_output=True, text=True).stdout.strip() == ""
    if clean:
        print("Your task is checked off and everything is committed. End the run now with the final message "
              "(RESULT: done / SUMMARY: ...). "
              "Do not start another TODO item: the harness assigns the next task (other agents own the others).",
              file=sys.stderr)
    else:
        print("Your task's line is checked off. Finish only the wrap-up (NOTES.md, TASKLOG.md, git commit), then "
              "end the run. Do not start another TODO item: other agents or later runs own them.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
