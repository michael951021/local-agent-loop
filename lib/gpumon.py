#!/usr/bin/env python3
"""Sample GPU utilization, VRAM, power and temperature every 5 s into logs/gpu/YYYYMMDD.csv.

Started (idempotently) by pretty.py for every agent run; exits once no agent log has been
written for IDLE_EXIT seconds (or the GPUs are idle, e.g. jobflow calling the model server without an agent).
Columns: epoch,gpu,util_pct,mem_used_mib,mem_total_mib,power_w,temp_c,fan_pct

Writes WARNING_FILE (~/GPU_WARNING.txt) when a GPU stays at/above WARN_C for a minute (RTX 3090: target 83 °C,
max operating 93, slowdown 95, shutdown 98), or shows 0% fan while hot. Delete the file once you have looked.
"""
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT, PIDFILE = ROOT / "logs" / "gpu", ROOT / "run" / "gpumon.pid"
IDLE_EXIT = 900
QUERY = "index,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu,fan.speed"
WARNING_FILE = Path.home() / "GPU_WARNING.txt"
WARN_C, CRIT_C, FAN_HOT_C, HOLD_S = 88, 93, 80, 60


def running():
    try:
        os.kill(int(PIDFILE.read_text()), 0)
        return True
    except (OSError, ValueError):
        return False


def last_activity():
    return max((p.stat().st_mtime for p in (ROOT / "logs").glob("*.jsonl")), default=0)


def warn(gpu, kind, temp, fan, secs):
    msg = (f"{time.strftime('%Y-%m-%d %H:%M:%S')}  GPU{gpu}  {kind}: {temp:.0f} °C, fan {fan}, "
           f"for {secs:.0f} s (warn {WARN_C}, max operating {CRIT_C}, shutdown 98)\n")
    new = not WARNING_FILE.exists()
    with open(WARNING_FILE, "a") as f:
        if new:
            f.write("GPU temperature warnings from loop/lib/gpumon.py (history: loop/logs/gpu/*.csv).\n"
                    "Check airflow/fans; `./agent stop NAME --now` cools things down. Delete this file once handled.\n\n")
        f.write(msg)


def check(p, state, now):
    """p = one CSV row split; state[gpu] = {kind: first-seen time, ...}; warns once per episode per kind."""
    try:
        gpu, temp = p[0].strip(), float(p[5])
        fan = p[6].strip() if len(p) > 6 else "?"
    except (ValueError, IndexError):
        return
    st = state.setdefault(gpu, {})
    conds = {"CRITICAL": temp >= CRIT_C, "HOT": temp >= WARN_C, "FAN 0% WHILE HOT": temp >= FAN_HOT_C and fan == "0"}
    for kind, on in conds.items():
        if not on:
            st.pop(kind, None)
            st.pop(kind + "!", None)
            continue
        st.setdefault(kind, now)
        if now - st[kind] >= HOLD_S and not st.get(kind + "!"):
            st[kind + "!"] = True
            warn(gpu, kind, temp, fan + ("%" if fan.isdigit() else ""), now - st[kind])


def main():
    if running():
        return
    OUT.mkdir(parents=True, exist_ok=True)
    PIDFILE.parent.mkdir(exist_ok=True)
    PIDFILE.write_text(str(os.getpid()))
    smi = subprocess.Popen(["nvidia-smi", f"--query-gpu={QUERY}", "--format=csv,noheader,nounits", "-lms", "5000"],
                           stdout=subprocess.PIPE, text=True)
    checked = busy = time.time()
    state = {}
    try:
        for line in smi.stdout:
            now = time.time()
            p = line.split(",")
            with open(OUT / time.strftime("%Y%m%d.csv"), "a") as f:
                f.write(f"{now:.0f}," + ",".join(v.strip() for v in p) + "\n")
            check(p, state, now)
            try:
                if float(p[1]) > 10:
                    busy = now
            except (ValueError, IndexError):
                pass
            if now - checked > 60:
                checked = now
                if now - max(last_activity(), busy) > IDLE_EXIT:
                    break
    finally:
        smi.terminate()
        PIDFILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
