#!/usr/bin/env python3
"""Harness-side close-out of a run whose final RESULT block says done (prompts/loop.md step 7).

    closeout.py DIR SRC TASK BASE FINAL_TEXT

Checks the task's `- [ ]` line in DIR/SRC and, unless the run already changed TASKLOG.md since BASE,
appends the TASKLOG entry from the RESULT block (SUMMARY / APPROACH / BLOCKERS). The agent no longer
spends turns on either. Prints what it did; exits 1 when RESULT is not done (nothing changed).
"""
import re
import subprocess
import sys
from pathlib import Path


def field(name, text):
    """Value of the last `NAME: ...` line (markdown decoration ignored), as agent's final_field does."""
    hits = re.findall(rf"^\W*{name}\W*:\s*(.*)$", text or "", flags=re.I | re.M)
    return re.sub(r"[*`]", "", hits[-1]).strip() if hits else ""


def check_line(path, task):
    """Turn `- [ ] TASK` into `- [x] TASK` (first match, indentation kept). True if it changed."""
    lines = path.read_text().split("\n")
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("- [ ] ") and stripped[6:].rstrip() == task.rstrip():
            lines[i] = line.replace("- [ ] ", "- [x] ", 1)
            path.write_text("\n".join(lines))
            return True
    return False


def tasklog_touched(d, base):
    r = subprocess.run(["git", "-C", str(d), "diff", "--quiet", base, "--", "TASKLOG.md"], capture_output=True)
    return r.returncode == 1


def title(task):
    return re.split(r"\s+—\s+", re.sub(r"\*\*", "", task), maxsplit=1)[0].strip()[:120]


def main():
    d, src, task, base, final = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
    if not field("RESULT", final).lower().startswith("done"):
        sys.exit(1)
    if check_line(d / src, task):
        print(f"checked the task's line in {src}")
    if not tasklog_touched(d, base):
        log = d / "TASKLOG.md"
        head = "" if log.exists() else "# Task log\n"
        approach = field("APPROACH", final) or field("SUMMARY", final) or "(no summary given)"
        entry = (f"## {title(task)}\n- Goal: {title(task)}\n- Approach: {approach}\n"
                 f"- Blockers: {field('BLOCKERS', final) or 'none'}\n")
        text = log.read_text() if log.exists() else head
        log.write_text(text + ("" if text.endswith("\n") or not text else "\n") + entry)
        print("appended the TASKLOG.md entry")


if __name__ == "__main__":
    main()
