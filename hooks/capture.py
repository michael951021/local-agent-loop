#!/usr/bin/env python3
"""Save full command output as evidence; return a bounded tail and its exit code.

python3 /opt/agent/hooks/capture.py --log evidence/test-1.log --timeout 300 -- pytest -q
Runs argv directly, without a shell. Use a new log path for each attempt.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not 0 < args.timeout <= 10800:
        parser.error("supply a command after -- and a timeout between 0 and 10800 seconds")
    args.log.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(args.log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as error:
        parser.exit(2, f"capture: {error}\n")
    started = time.monotonic()
    with os.fdopen(fd, "w") as output:
        output.write(json.dumps({"command": command, "cwd": os.getcwd(), "timeout": args.timeout}) + "\n")
        output.flush()
        try:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        except OSError as error:
            output.write(f"Could not start command: {error}\n")
            code = 127
        else:
            try:
                code = process.wait(timeout=args.timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                # Kill the process group, including grandchildren that hold the log open.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                code = 124 if isinstance(error, subprocess.TimeoutExpired) else 130
                output.write(f"\nStopped: {'timeout' if code == 124 else 'interrupted'}\n")
        code = 128 - code if code < 0 else code
        output.write(f"\nEXIT_CODE={code} ELAPSED_SECONDS={time.monotonic() - started:.2f}\n")
    with args.log.open("rb") as output:
        size = output.seek(0, os.SEEK_END)
        output.seek(max(0, size - 6000))
        tail = output.read().decode("utf-8", errors="replace")
    lines = tail.splitlines()
    print(f"Full output: {args.log} ({size} bytes). Exit code: {code}.")
    if size > 6000 or len(lines) > 40:
        print("[tail only; search the saved log for earlier errors]")
    print("\n".join(lines[-40:]))
    return code


if __name__ == "__main__":
    sys.exit(main())
