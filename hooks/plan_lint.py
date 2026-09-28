#!/usr/bin/env python3
"""PostToolUse hook: after an edit to TODO.md, every open task needs a `why:` line and every section a
`> why:` line (see plan.py; this is its stdlib-only core, since the sandbox has no harness venv).

Tells the agent in the same turn (exit 2: stderr goes to the model), so fixing it costs no extra run.
"""
import json
import os
import re
import sys

TASK = re.compile(r"^\s*- \[ \] (.*)$")
HEAD = re.compile(r"^#{2,6}\s+(.*)$")


def problems(lines):
    out = []
    for i, line in enumerate(lines):
        nxt = [l for l in lines[i + 1:i + 6] if l.strip() and not l.lstrip().startswith(("<!--", "-->"))]
        m = TASK.match(line)
        if m and not (nxt and re.match(r"^\s+why:", nxt[0], re.I)):
            out.append(f"line {i + 1}: task has no indented `why:` line under it: {m.group(1)[:70]}")
        h = HEAD.match(line)
        if h and not any(re.match(r"^\s*>\s*why:", l, re.I) for l in nxt[:2]):
            # only sections that still hold open tasks need one
            rest = []
            for l in lines[i + 1:]:
                if HEAD.match(l) and len(l) - len(l.lstrip("#")) <= len(line) - len(line.lstrip("#")):
                    break
                rest.append(l)
            if any(TASK.match(l) for l in rest):
                out.append(f"line {i + 1}: section has no `> why:` line under its heading: {h.group(1)[:70]}")
    return out


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    path = (data.get("tool_input") or {}).get("file_path") or ""
    if os.path.basename(path) != "TODO.md":
        return 0
    try:
        lines = open(path, errors="replace").read().splitlines()
    except OSError:
        return 0
    probs = problems(lines)
    if not probs:
        return 0
    print("TODO.md: each open task needs an indented `why:` line right under it (≤30 words: how it serves its "
          "section), and each section a `> why:` line under its heading (≤45 words: how it serves the level "
          "above). Fix these now:\n" + "\n".join(probs[:10]), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
