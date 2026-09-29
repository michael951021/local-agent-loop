#!/usr/bin/env python3
"""Bounded working state for SessionStart, including resumed/compacted sessions."""
import os
from pathlib import Path
import subprocess


def snapshot(root):
    parts = ["## Current working state"]
    for label, command in (("Recent commits", ["git", "log", "--oneline", "-3"]),
                           ("Uncommitted changes", ["git", "status", "--short"])):
        try:
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=5)
            parts += [f"### {label}", "\n".join(result.stdout.splitlines()[:15])[:1600]]
        except (OSError, subprocess.TimeoutExpired):
            parts.append(f"{label}: unavailable")
    notes = root / "NOTES.md"
    if notes.is_file():
        with notes.open(errors="replace") as source:
            text = source.read(4001)
        parts += ["### NOTES.md snapshot (already in context)", text[:4000]]
        if len(text) > 4000:
            parts.append("[NOTES truncated at 4000 characters; search for omitted details only if needed.]")
    # The harness supplies the assigned task in its prompt. Listing other open
    # tickets here encouraged redundant TODO reads and out-of-scope work.
    return "\n".join(parts)


if __name__ == "__main__":
    print(snapshot(Path(os.environ.get("AGENT_PROJECT_DIR", "/work"))))
