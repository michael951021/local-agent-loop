#!/usr/bin/env python3
"""PreToolUse hook (Bash): block anything that publishes to GitHub, and direct calls to Ollama.

Blocks git push, gh commands that create/edit/comment, and write requests through gh api or curl.
Read-only gh/git/curl calls pass. Exit 2 blocks the call; stderr is shown to the agent.
"""
import json
import os
import re
import sys

WRITE_METHOD = r"(POST|PATCH|PUT|DELETE)"
RULES = [
    (r"^git(\s+(-C|-c|--git-dir|--work-tree)[\s=]\S+|\s+--?[\w-]+)*\s+push\b", "git push"),
    (r"^gh\s+(pr|issue)\s+(create|comment|edit|close|reopen|merge|review|develop|lock|pin|transfer|delete|ready)\b",
     "gh pr/issue write command"),
    (r"^gh\s+(repo\s+(create|fork|edit|delete|sync)|release\s+create|gist\s+create|label\s+(create|edit|delete))\b",
     "gh write command"),
    (r"^gh\s+api\b.*(-X|--method)[\s=]*" + WRITE_METHOD + r"\b", "gh api write request"),
    (r"^curl\b.*github\.com.*(-X\s*" + WRITE_METHOD + r"\b|--request\s+" + WRITE_METHOD + r"\b|\s-d\b|--data)",
     "curl write request to GitHub"),
    (r"^curl\b.*(-X\s*" + WRITE_METHOD + r"\b|--request\s+" + WRITE_METHOD + r"\b|\s-d\b|--data).*github\.com",
     "curl write request to GitHub"),
]


def segments(cmd: str) -> list[str]:
    """Split a shell command into simple commands, dropping heredoc bodies, leading env
    assignments and wrappers, so rules only match what actually runs (not grep/echo text)."""
    cmd = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\b", " ", cmd, flags=re.S)
    out = []
    for seg in re.split(r"[|;&()\n`]+|\$\(", cmd):
        seg = seg.strip()
        seg = re.sub(r"^((\w+=\S*|sudo|env|command|exec|time|timeout\s+\S+|nohup)\s+)+", "", seg)
        if seg:
            out.append(seg)
    return out


def gh_api_field_post(seg: str) -> bool:
    """gh api turns into a POST when given fields/input without an explicit method."""
    if not re.match(r"gh\s+api\b", seg) or re.search(r"(-X|--method)[\s=]*GET\b", seg):
        return False
    if re.match(r"gh\s+api\s+graphql\b", seg):
        return bool(re.search(r"\bmutation\b", seg))
    return bool(re.search(r"\s(-f|-F|--field|--raw-field|--input)\b", seg))


def main() -> int:
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Bash":
        return 0
    cmd = (data.get("tool_input") or {}).get("command", "")
    if os.environ.get("AGENT_BACKEND") == "llama" and re.search(r"(127\.0\.0\.1|localhost|0\.0\.0\.0):11434", cmd):
        print("Blocked: 127.0.0.1:11434 is Ollama, which does not serve the model right now (llama-server does); "
              "calling it loads a second copy of the model on the CPU. Use $OLLAMA_BASE_URL (OpenAI-compatible /v1) "
              "or $OLLAMA_HOST (answers /api/tags) instead, e.g. curl \"$OLLAMA_BASE_URL/chat/completions\".",
              file=sys.stderr)
        return 2
    what = None
    for seg in segments(cmd):
        what = next((w for p, w in RULES if re.search(p, seg)), None)
        if not what and gh_api_field_post(seg):
            what = "gh api request with fields (POST by default; add -X GET for reads)"
        if what:
            break
    if what:
        print(f"Blocked by no-publish policy ({what}). This loop never publishes to GitHub; "
              "a human does that after review. Use read-only commands.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
