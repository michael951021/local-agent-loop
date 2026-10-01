#!/usr/bin/env python3
"""Agent pool: keeps SLOTS agents busy across every project, newest project first.

  pool.py [SLOTS]      (./agent pool [N]; default NUM_PARALLEL)

Every POLL seconds: busy = live workers holding a claim (run/*/workers/wK.json or main.json, any project, pool
or not) plus running A/B trials (run/ab/EXP/*.pid; lib/ab.py only starts those in slots the loop leaves free). When busy < SLOTS on two polls in a row (a worker between tasks drops its claim for a moment), the
newest project (first commit) with a ready TODO task and no idle worker of its own gets `./agent worker NAME`
in a new window of tmux session `pool`, with POOL=1: that worker exits as soon as it has nothing to claim, so
the slot comes back here instead of sleeping on a dependency chain.
Skips projects in POOL_SKIP (space-separated; names containing "smoke" always) and projects running a single-agent `main` loop.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import sched  # noqa: E402

RUN, PROJECTS = ROOT / "run", ROOT / "projects"
POLL, SPAWN_GRACE = 30, 120
SESSION = "pool"


def created(d):
    r = subprocess.run(["git", "-C", str(d), "log", "--reverse", "--format=%ct"], capture_output=True, text=True)
    return int((r.stdout.split() or ["0"])[0])


def workers(name):
    """{wid: has_claim} for the project's live workers."""
    out = {}
    for pid in (RUN / name / "workers").glob("*.pid"):
        if sched.alive(pid.read_text().strip() or 0):
            out[pid.stem] = (pid.parent / f"{pid.stem}.json").exists()
    return out


def ready(name):
    d = PROJECTS / name
    st = sched.State(str(RUN / name))
    try:
        rows = sched.plan(sched.Files(d), st.claims(), st.load("failed.json", {}))
    except Exception as e:   # a malformed TODO.md must not take the pool down
        print(f"  {name}: {e}", flush=True)
        return 0
    return sum(1 for _, s, _ in rows if s == "ready")


def projects():
    skip = set(os.environ.get("POOL_SKIP", "").split())
    names = [p.name for p in PROJECTS.iterdir()
             if (p / ".git").exists() and (p / "TODO.md").exists() and p.name not in skip and "smoke" not in p.name]
    return sorted(names, key=lambda n: created(PROJECTS / n), reverse=True)


def spawn(name):
    env = " ".join(f"{v}={os.environ[v]}" for v in ("BACKEND", "MAX_TURNS", "ITER_TIMEOUT", "NET") if v in os.environ)
    cmd = f"env POOL=1 {env} {ROOT}/agent worker {name}; echo '[pool worker exited]'; sleep 600"
    if subprocess.run(["tmux", "has-session", "-t", f"={SESSION}"], capture_output=True).returncode:
        subprocess.run(["tmux", "new-session", "-d", "-s", SESSION, "-n", name, "-x", "200", "-y", "50", cmd])
    else:
        subprocess.run(["tmux", "new-window", "-d", "-t", SESSION, "-n", name, cmd])


def main():
    slots = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("NUM_PARALLEL", 2))
    print(f"pool: keeping {slots} agents busy, newest project first (Ctrl-C stops the pool, not its agents)", flush=True)
    short, recent = 0, {}   # recent: project -> time of our last spawn there
    last = None
    while True:
        names = projects()
        live = {n: workers(n) for n in names}
        trials = sum(1 for p in (RUN / "ab").glob("*/*.pid") if sched.alive(p.read_text().strip() or 0))
        busy = sum(c for w in live.values() for c in w.values()) + trials
        pending = sum(1 for n, t in recent.items() if time.time() - t < SPAWN_GRACE and not any(live.get(n, {}).values()))
        short = short + 1 if busy + pending < slots else 0
        line = f"busy {busy}/{slots} · " + ", ".join(f"{n}:{sum(w.values())}" for n, w in live.items() if w) \
            + (f", A/B trials:{trials}" if trials else "")
        if line != last:
            print(time.strftime("%H:%M:%S"), line, flush=True)
            last = line
        if short >= 2:
            for n in names:
                w = live[n]
                if "main" in w or time.time() - recent.get(n, 0) < SPAWN_GRACE:
                    continue
                idle = sum(1 for c in w.values() if not c)
                if ready(n) > idle:
                    print(time.strftime("%H:%M:%S"), f"→ free slot: starting an agent on {n}", flush=True)
                    spawn(n)
                    recent[n] = time.time()
                    short = 0
                    break
        time.sleep(POLL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\npool stopped (its agents keep running; ./agent stop NAME stops them)")
