#!/usr/bin/env python3
"""Render `claude -p --output-format stream-json` as readable progress lines."""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DIM, BOLD, CYAN, GREEN, RED, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[32m", "\033[31m", "\033[0m"


def short(value, n=160):
    s = value if isinstance(value, str) else json.dumps(value)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def emit(line):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{DIM}{ts}{RESET} {line}")


def spawn(*args):
    """Run a helper detached, so it outlives this pipeline and never blocks the agent."""
    with open(ROOT / "logs" / "ctxreport.log", "a") as err:
        subprocess.Popen([sys.executable, *map(str, args)], stdin=subprocess.DEVNULL, stdout=err,
                         stderr=err, start_new_session=True)


def report(trigger):
    # Delay so tee has flushed the last events to the log before the report reads it.
    if session:
        spawn("-c", "import subprocess,sys,time; time.sleep(3); subprocess.run(sys.argv[1:])",
              sys.executable, ROOT / "ctxreport.py", "--session", session, "--trigger", trigger)


session = None
spawn(ROOT / "gpumon.py")   # no-op if already sampling

for line in sys.stdin:
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        emit(line.rstrip("\n"))
        continue
    kind = ev.get("type")
    session = ev.get("session_id") or session
    if kind == "system" and ev.get("subtype") == "compact_boundary":
        pre = (ev.get("compact_metadata") or {}).get("pre_tokens")
        emit(f"{DIM}⟲ context compacted (was {pre} tokens) — writing report{RESET}")
        report("compact")
    if kind == "assistant":
        for block in ev["message"].get("content", []):
            if block.get("type") == "text" and block["text"].strip():
                emit(f"{BOLD}●{RESET} {block['text'].strip()}")
            elif block.get("type") == "tool_use":
                inp = block.get("input", {})
                arg = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp
                emit(f"{CYAN}→ {block['name']}{RESET} {DIM}{short(arg)}{RESET}")
    elif kind == "user":
        for block in ev["message"].get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                emit(f"  {RED}✗ {short(block.get('content'))}{RESET}")
    elif kind == "result":
        color = GREEN if not ev.get("is_error") else RED
        secs = ev.get("duration_ms", 0) / 1000
        emit(f"{color}■ {ev.get('subtype')} — {ev.get('num_turns')} turns, {secs:.0f}s{RESET}")
    sys.stdout.flush()

# ./agent writes the end report itself once the run is committed and merged (AGENT_RUN is set);
# runs started some other way get it here.
if not os.environ.get("AGENT_RUN"):
    report("end")
