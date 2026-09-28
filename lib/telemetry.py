"""Data layer for the reports: everything is read from files the harness already writes.

  logs/*.jsonl            Claude Code stream-json, framed per run by {"type":"harness"} lines
  logs/requests/*.jsonl   keepalive.py ledger: one line per model request, tagged with its agent
  journalctl -u ollama    llama.cpp per-request timings (prompt size, prefill, generation, slot)
  logs/gpu/*.csv          gpumon.py samples every 5 s
  projects/*/.git         commits

Nothing here renders; ctxreport.py (one run) and fleet.py (all agents) turn these into pages.
"""
import json
import re
import subprocess
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
TAGS = re.compile(r"\s*\((?:id|after): [^)]*\)")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


def clean_task(t):
    """Task text without scheduler tags."""
    return TAGS.sub("", t or "").strip()


def _split_title():
    import importlib.util   # sched.py, not the standard library's sched module
    spec = importlib.util.spec_from_file_location("agent_sched", ROOT / "lib" / "sched.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.split_title


split_title = _split_title()
_titles = {}


def task_title(task, project=None):
    """(title, spec) for display. Runs recorded before TODO lines had titles get the current title of
    the same (id: x) line."""
    title, spec = split_title(task)
    if not project or (task or "").lstrip().startswith("**"):
        return title, spec
    if project not in _titles:   # current titled lines: by (id: x), and all of them for fuzzy matching
        by_id, all_ = {}, []
        todo = ROOT / "projects" / project / "TODO.md"
        for line in (todo.read_text().splitlines() if todo.exists() else []):
            if "**" not in line:
                continue
            ts = split_title(re.sub(r"^\s*- \[.\] ", "", line))
            i = re.search(r"\(id: *([\w.-]+)\)", line)
            if i:
                by_id[i.group(1)] = ts
            all_.append(ts)
        _titles[project] = (by_id, all_)
    by_id, all_ = _titles[project]
    m = re.search(r"\(id: *([\w.-]+)\)", task or "")
    if m and m.group(1) in by_id:
        return by_id[m.group(1)]
    # Untagged lines were reworded when titles were added: take the current line sharing the most words.
    words = lambda t: set(re.findall(r"[a-z0-9_.\-/]{3,}", t.lower()))
    old = words(clean_task(task))
    sim = lambda ts: len(old & words(ts[0] + " " + ts[1])) / max(1, len(old | words(ts[0] + " " + ts[1])))
    best = max(all_, key=sim, default=None)
    return best if best and sim(best) >= 0.25 else (title, spec)


def config(name, default):
    m = re.search(rf"^{name}=\$?{{?(?:{name}:-)?(\d+)", (ROOT / "config.env").read_text(), re.M)
    return int(m.group(1)) if m else default


def agent_tag(start):
    """The keepalive tag of a run: 'project' for the in-place agent, 'project/wK' for a worktree agent."""
    if not start:
        return None
    w = start.get("worker") or "main"
    return start["project"] if w == "main" else f"{start['project']}/{w}"


# ── harness runs ──────────────────────────────────────────────────────────────

def log_runs(log):
    """Runs in a log, in order: {start, end, sessions}. Sessions before the first harness line
    (logs from before the harness wrote them) form runs without start/end."""
    runs, by_id, cur = [], {}, None
    for line in open(log, errors="replace"):
        if line.startswith('{"type":"harness"'):
            d = json.loads(line)
            if d.get("event") == "start":
                cur = {"start": d, "end": None, "sessions": []}
                runs.append(cur)
                by_id[d["run"]] = cur
            elif d.get("event") == "end" and d.get("run") in by_id:
                by_id[d["run"]]["end"] = d
        elif '"subtype":"init"' in line:
            sid = json.loads(line).get("session_id")
            if cur is None:
                cur = {"start": None, "end": None, "sessions": []}
                runs.append(cur)
            if sid and sid not in cur["sessions"]:
                cur["sessions"].append(sid)
    return runs


def harness_runs(since):
    """Every framed run in logs/ that started after `since` (for the fleet view)."""
    out = subprocess.run(["grep", "-H", '^{"type":"harness"', *map(str, LOGS.glob("*.jsonl"))],
                         capture_output=True, text=True).stdout
    runs = {}
    for line in out.splitlines():
        path, _, js = line.partition(":")
        try:
            d = json.loads(js)
        except json.JSONDecodeError:
            continue
        r = runs.setdefault(d["run"], {"log": Path(path).name, "start": None, "end": None})
        r[d["event"]] = d
    return sorted((r for r in runs.values() if r["start"] and r["start"]["t"] >= since),
                  key=lambda r: r["start"]["t"])


def read_session(log, sid):
    events = []
    for line in open(log, errors="replace"):
        if sid not in line or line.startswith('{"type":"system","subtype":"thinking_tokens"'):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("session_id") == sid:
            events.append(d)
    return events


# ── Ollama journal ────────────────────────────────────────────────────────────

RX = {
    "start": re.compile(r"id\s+(\d+) \| task (\d+) \| new prompt, n_ctx_slot = (\d+).*task\.n_tokens = (\d+)"),
    "full": re.compile(r"task (\d+) \| forcing full prompt re-processing"),
    "prefill": re.compile(r"task (\d+) \| prompt eval time =\s*([\d.]+) ms /\s*(\d+) tokens"),
    "gen": re.compile(r"task (\d+) \|\s+eval time =\s*([\d.]+) ms /\s*(\d+) tokens"),
    "progress": re.compile(r"task (\d+) \| n_gen =\s*(\d+), tg =\s*([\d.]+)"),
    "pp": re.compile(r"task (\d+) \| prompt processing, n_tokens =\s*(\d+), progress = [\d.]+, t =\s*([\d.]+) s"),
    "end": re.compile(r"task (\d+) \| stop processing: n_tokens = (\d+)"),
    "cancel": re.compile(r"cancel task, id_task = (\d+)"),
}


def journal(t0, t1, grep=None):
    """Log lines of every model server in [t0, t1], merged by time: Ollama's system unit and the
    llama-server user unit started by ./llamasrv (same llama.cpp log format)."""
    out = []
    for scope in (["-u", "ollama"], ["--user", "-u", "llama-agent"]):
        args = ["journalctl", *scope, "--no-pager", "-o", "short-unix",
                "--since", f"@{int(t0)}", "--until", f"@{int(t1)}"]
        if grep:
            args += ["-g", grep]
        try:
            out += subprocess.run(args, capture_output=True, text=True, timeout=180).stdout.splitlines()
        except (OSError, subprocess.TimeoutExpired):
            pass
    out.sort(key=lambda l: l.split(" ", 1)[0])
    return "\n".join(out)


def prompt_tokens(usage):
    """Whole prompt size from Anthropic-style usage. Ollama puts it all in input_tokens; llama-server
    (and Anthropic) count cached tokens separately in cache_read/cache_creation_input_tokens."""
    u = usage or {}
    return sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def ollama_requests(t0, t1):
    """One dict per llama.cpp task that started in [t0, t1]: prompt size, reuse, prefill/generation
    time, tokens out, slot, and whether it was cancelled."""
    reqs = {}
    for line in journal(t0 - 30, t1 + 30, grep="task").splitlines():
        try:
            t = float(line.split(" ", 1)[0])
        except ValueError:
            continue
        for kind, rx in RX.items():
            m = rx.search(line)
            if not m:
                continue
            proc = line.split(" ", 3)[2]   # e.g. ollama[170211]: task ids restart per server process
            if kind == "start":
                reqs[proc + m.group(2)] = {"t": t, "slot": int(m.group(1)), "n_ctx": int(m.group(3)), "prompt": int(m.group(4)),
                                    "processed": None,
                                    "prefill_s": None, "gen": 0, "gen_s": 0.0, "tps": None, "full": False,
                                    "cancelled": False, "end": None, "server": proc.split("[")[0]}
                break
            r = reqs.get(proc + m.group(1))
            if not r:
                break
            if kind == "full":
                r["full"] = True
            elif kind == "prefill":
                r["prefill_s"], r["processed"] = float(m.group(2)) / 1000, int(m.group(3))
            elif kind == "gen":
                r["gen_s"], r["gen"] = float(m.group(2)) / 1000, int(m.group(3))
                r["tps"] = r["gen"] / r["gen_s"] if r["gen_s"] else None
            elif kind == "progress":
                r["gen"], r["tps"], r["last"] = int(m.group(2)), float(m.group(3)), t
            elif kind == "pp":
                r["pp_last"] = (t, float(m.group(3)))
            elif kind == "end":
                r["end"] = t
            elif kind == "cancel":
                r["cancelled"], r["end"] = True, r["end"] or t
            break
    now, res = time.time(), []
    for r in reqs.values():
        if not (t0 - 5 <= r["t"] <= t1 + 5):
            continue
        running = r["end"] is None and not r["cancelled"]
        end = r["end"] or (min(now, t1) if running else r.get("last", r["t"]))
        if r["prefill_s"] is None:   # cancelled or still running: estimate from progress lines
            r["prefill_s"] = r.get("pp_last", (0, 0.0))[1] if r.get("pp_last") else (end - r["t"] if not r["gen"] else 0.0)
            r["gen_s"] = max(0.0, end - r["t"] - r["prefill_s"])
        r["processed"] = r["processed"] if r["processed"] is not None else r["prompt"]
        r["reused"] = max(0, r["prompt"] - r["processed"])
        r["end"], r["running"] = end, running
        r["wall_s"] = round(end - r["t"], 1)
        r.pop("pp_last", None)
        r.pop("last", None)
        res.append(r)
    return sorted(res, key=lambda r: r["t"])


def vram_budget(days=3):
    """How the model's VRAM is split, from Ollama's most recent model load (MiB)."""
    out = journal(time.time() - days * 86400, time.time() + 60, grep="model buffer size|llama_kv_cache: size|"
                  "llama_memory_recurrent: size|compute buffer size|n_seq_max|n_ctx_seq  ")
    lines = out.splitlines()
    starts = [i for i, l in enumerate(lines) if "CUDA0 model buffer size" in l]
    if not starts:
        return None
    b = {"weights": 0.0, "kv": 0.0, "recurrent": 0.0, "compute": 0.0, "slots": 1, "ctx_slot": None, "kv_cells": None}
    for l in lines[starts[-1]:]:
        mib = re.search(r"=\s*([\d.]+) MiB", l)
        if "CUDA" in l and "model buffer size" in l:
            b["weights"] += float(mib.group(1))
        elif "llama_kv_cache: size" in l:
            b["kv"] = float(mib.group(1))
            m = re.search(r"\((\d+) cells", l)
            b["kv_cells"] = int(m.group(1)) if m else None
        elif "llama_memory_recurrent: size" in l:
            b["recurrent"] = float(mib.group(1))
        elif "CUDA" in l and "compute buffer size" in l and "sched_reserve" in l:
            b["compute"] += float(mib.group(1))
        elif "n_seq_max" in l:
            b["slots"] = int(l.rsplit("=", 1)[1])
        elif "n_ctx_seq  " in l:
            b["ctx_slot"] = int(l.rsplit("=", 1)[1])
    return b


# ── request ledger (keepalive.py) and attribution ────────────────────────────

def ledger(t0, t1):
    rows = []
    days = {time.strftime("%Y%m%d", time.localtime(x)) for x in range(int(t0) - 86400, int(t1) + 86400, 3600)}
    for day in sorted(days):
        f = LOGS / "requests" / f"{day}.jsonl"
        if f.exists():
            for line in f.read_text().splitlines():
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("t1", e["t0"]) >= t0 - 60 and e["t0"] <= t1 + 60:
                    rows.append(e)
    return sorted(rows, key=lambda e: e["t0"])


def attribute(reqs, rows):
    """Tag each Ollama request with the agent whose proxied request was open when it started.
    Ollama's prompt is ~5k tokens more than the input_tokens it reports, which breaks ties."""
    free = list(rows)
    for r in reqs:
        cands = [e for e in free if e["t0"] - 1.5 <= r["t"] <= e.get("t1", r["t"]) + 1]
        if len(cands) > 1 and all(prompt_tokens(e) for e in cands):
            cands.sort(key=lambda e: abs(r["prompt"] - prompt_tokens(e) - 5000))
        if cands:
            e = cands[0]
            free.remove(e)
            r["tag"], r["input_tokens"] = e["tag"], prompt_tokens(e) or None
        else:
            r["tag"] = "other clients"
    return reqs


def fair_share(reqs, t0=None, t1=None):
    """GPU time per tag: while n requests run at once, each is charged 1/n of the time.
    Returns ({tag: seconds}, busy seconds, seconds with 2+ requests running)."""
    pts = []
    for r in reqs:
        a, b = r["t"], r["end"]
        if t0 is not None:
            a, b = max(a, t0), min(b, t1)
        if b > a:
            pts += [(a, 1, r["tag"]), (b, -1, r["tag"])]
    pts.sort(key=lambda p: (p[0], p[1]))
    share, busy, overlap, active, last = defaultdict(float), 0.0, 0.0, defaultdict(int), None
    for t, d, tag in pts:
        n = sum(active.values())
        if last is not None and n and t > last:
            dt = t - last
            busy += dt
            overlap += dt if n > 1 else 0
            for k, c in active.items():
                if c:
                    share[k] += dt * c / n
        active[tag] += d
        last = t
    return dict(share), busy, overlap


# ── GPU samples ───────────────────────────────────────────────────────────────

def gpu_samples(t0, t1, step=None):
    """[{t, g: {gpu: [util %, used GB, total GB, W, °C]}}]; with step, averaged into buckets."""
    rows = defaultdict(dict)
    days = sorted({time.strftime("%Y%m%d", time.localtime(x)) for x in range(int(t0), int(t1) + 86400, 86400)})
    for day in days:
        f = LOGS / "gpu" / f"{day}.csv"
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            p = line.split(",")
            try:
                t = int(p[0])
                if t0 - 5 <= t <= t1 + 5:
                    rows[t][int(p[1])] = [float(p[2]), float(p[3]) / 1024, float(p[4]) / 1024, float(p[5]), float(p[6])]
            except (ValueError, IndexError):
                continue
    out = [{"t": t, "g": g} for t, g in sorted(rows.items())]
    if not step or not out:
        return out
    buckets = defaultdict(list)
    for s in out:
        buckets[int(s["t"] // step * step)].append(s["g"])
    res = []
    for t, gs in sorted(buckets.items()):
        ids = sorted({i for g in gs for i in g})
        res.append({"t": t, "g": {i: [sum(g[i][k] for g in gs if i in g) / sum(1 for g in gs if i in g)
                                      for k in range(5)] for i in ids}})
    return res


# ── git ───────────────────────────────────────────────────────────────────────

def commits(project, base, head):
    pdir = ROOT / "projects" / project
    if not (base and head and (pdir / ".git").exists()) or base == head:
        return []
    return subprocess.run(["git", "-C", str(pdir), "log", "--first-parent", "--no-merges", "--format=%h %s",
                           f"{base}..{head}"], capture_output=True, text=True).stdout.splitlines()


def task_state(start, end):
    """What became of a run's task: done, split, open, interrupted, merge-fix, or running.
    New runs record it; for older ones it is read from git (the task file at the run's last commit)."""
    if not end:
        return "running"
    if end.get("task_state"):
        return end["task_state"]
    if start.get("merge_fix"):
        return "merge-fix"
    task, src, head = start.get("task"), start.get("src") or "TODO.md", end.get("head")
    pdir = ROOT / "projects" / (start.get("project") or "")
    lines = []
    if task and head and (pdir / ".git").exists():
        text = subprocess.run(["git", "-C", str(pdir), "show", f"{head}:{src}"], capture_output=True, text=True).stdout
        lines = [l.strip().lower() for l in text.splitlines()]
    if f"- [x] {task}".lower() in lines:
        return "done"   # also for a run cut off after it had checked its task
    if end.get("merged") == "paused":
        return "interrupted"
    if not lines:
        return None
    return "open" if f"- [ ] {task}".lower() in lines else "split"


# The result of a run in plain words: (label, css class, one-line explanation).
def result(start, end):
    st, merged = task_state(start or {}, end), (end or {}).get("merged")
    fix = {"yes": "merged", "conflict": "still conflicting, tried again", "parked": "gave up, work parked on a branch"}
    if st == "running":
        return "… running", "", "still running (or its agent was killed before it could finish)"
    if st == "merge-fix":
        return "↻ merge fix", "warn" if merged != "yes" else "", "resolved a merge conflict from the previous run: " + fix.get(merged, merged or "?")
    if st == "interrupted":
        return "⏸ interrupted", "warn", "stopped mid-task (stop --now, time limit or crash); the same agent continues it"
    if st == "done":
        if merged == "conflict":
            return "✔ done", "warn", "task finished; merging hit a conflict, fixed by the next run"
        if merged == "paused":
            return "✔ done", "warn", "task finished before the run was cut off (stop or time limit); merged by its next run"
        if merged == "parked":
            return "✔ done", "bad", "task finished, but its work could not be merged (parked on a branch)"
        return "✔ done", "good", "task finished and merged" if merged in ("yes", "n/a", None) else "task finished"
    if st == "split":
        return "✂ split", "", "split its task into smaller TODO lines and did the first"
    if st == "open":
        return "✖ not finished", "bad", "ended with its task unchecked; the task is tried again"
    return "?", "", "unknown (no harness record)"


def guess_task(project, t0, t1):
    """For runs without harness lines: the TODO line checked off during the run."""
    pdir = ROOT / "projects" / project
    if not (pdir / ".git").exists():
        return None, []
    git = lambda *a: subprocess.run(["git", "-C", str(pdir), *a], capture_output=True, text=True).stdout
    cs = git("log", f"--since=@{int(t0)}", f"--until=@{int(t1) + 120}", "--format=%h %s").splitlines()
    diff = git("log", "-p", f"--since=@{int(t0)}", f"--until=@{int(t1) + 120}", "--format=", "--", "*.md")
    m = re.search(r"^\+\s*- \[x\] (.+)$", diff, re.M)
    return (m.group(1) if m else None), cs
