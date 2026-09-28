#!/usr/bin/env python3
"""Sample GPU utilization, VRAM, power and temperature every 5 s into logs/gpu/YYYYMMDD.csv.

Started (idempotently) by pretty.py for every agent run; exits once no agent log has been
written for IDLE_EXIT seconds. Columns: epoch,gpu,util_pct,mem_used_mib,mem_total_mib,power_w,temp_c
"""
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT, PIDFILE = ROOT / "logs" / "gpu", ROOT / "run" / "gpumon.pid"
IDLE_EXIT = 900
QUERY = "index,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu"


def running():
    try:
        os.kill(int(PIDFILE.read_text()), 0)
        return True
    except (OSError, ValueError):
        return False


def last_activity():
    return max((p.stat().st_mtime for p in (ROOT / "logs").glob("*.jsonl")), default=0)


def main():
    if running():
        return
    OUT.mkdir(parents=True, exist_ok=True)
    PIDFILE.parent.mkdir(exist_ok=True)
    PIDFILE.write_text(str(os.getpid()))
    smi = subprocess.Popen(["nvidia-smi", f"--query-gpu={QUERY}", "--format=csv,noheader,nounits", "-lms", "5000"],
                           stdout=subprocess.PIPE, text=True)
    checked = time.time()
    try:
        for line in smi.stdout:
            now = time.time()
            with open(OUT / time.strftime("%Y%m%d.csv"), "a") as f:
                f.write(f"{now:.0f}," + ",".join(v.strip() for v in line.split(",")) + "\n")
            if now - checked > 60:
                checked = now
                if now - last_activity() > IDLE_EXIT:
                    break
    finally:
        smi.terminate()
        PIDFILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
