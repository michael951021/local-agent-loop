#!/usr/bin/env python3
"""PreToolUse/Read: require smaller ranges before large text enters context.

Exit 2 is the same blocking contract used by no_publish.py. This is a Read
budget, not a shell sandbox: Bash output is handled by capture.py and audited
in reports. No file is modified and no result is silently truncated.
"""
import json
import os
from pathlib import Path
import sys


def positive_env(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def reason(data):
    if not isinstance(data, dict) or data.get("tool_name") != "Read":
        return None
    inp = data.get("tool_input") or {}
    path = Path(inp.get("file_path") or "")
    if not path.is_absolute():
        path = Path(data.get("cwd") or "/work") / path
    if not path.is_file():
        return None
    if path.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ipynb"}:
        return None  # these use the tool's non-text rendering
    max_lines = positive_env("AGENT_READ_MAX_LINES", 240)
    max_chars = positive_env("AGENT_READ_MAX_CHARS", 16000)
    try:
        offset = int(inp.get("offset", 1))
        limit = int(inp.get("limit", 2000))
        if offset < 1 or limit < 1:
            return None  # leave invalid arguments to the tool
        with path.open(errors="replace") as source:
            for _ in range(offset - 1):
                if not source.readline():
                    return None
            chars = 0
            for index in range(min(limit, max_lines + 1)):
                line = source.readline(max_chars + 1)
                if not line:
                    return None
                chars += len(line)
                if index >= max_lines or chars > max_chars:
                    return (f"Read budget: this range exceeds {max_lines} lines or {max_chars} characters. "
                            f"Locate the relevant symbol with rg -n, then Read with offset and limit "
                            f"(start with 80 lines). For long command output use "
                            f"python3 /opt/agent/hooks/capture.py --log <new-log-path> -- <command>. "
                            "The file has not been read into context; nothing was changed.")
    except (OSError, ValueError, TypeError):
        return None
    return None


def main():
    try:
        message = reason(json.load(sys.stdin))
    except (ValueError, TypeError):
        return 0
    if message:
        print(message, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
