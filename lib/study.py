#!/usr/bin/env python3
"""Aggregate every agent run into one study of how the loop spends its time, context and GPU.

  study.py            collect (cached per log file) and write reports/study.html, study.json, study/*.csv
  study.py --json     only print the collected numbers

Sources (all already written by the harness; see telemetry.py): the Claude Code transcripts in
logs/*.jsonl (tools, timings, context per call), the model server's journal (per-request prefill and
generation), the keepalive ledger (which agent sent each request), logs/gpu/*.csv (5 s GPU samples).
Test projects (smoke*, captest, hooktest) are left out.
"""
import bisect
import csv
import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contextaudit
import ctxreport  # noqa: E402  (analyse(): context per call, the categories)
import reportui  # noqa: E402
from telemetry import (LOGS, ROOT, agent_tag, attribute, gpu_samples, harness_runs, ledger,  # noqa: E402
                       ollama_requests, result, task_title)

OUT = ROOT / "reports"
CACHE = ROOT / "run" / "study-cache"
CACHE_VERSION = 2  # context audit schema; invalidate old derived data
SKIP = re.compile(r"^(smoke|captest|hooktest|zz)")
# When the model server changed (local time, from the harness history and the llama-agent journal).
ERAS = [("Ollama, 1 slot", 0),
        ("llama-server, 2 × 96K", time.mktime((2026, 9, 27, 4, 13, 0, 0, 0, -1))),
        ("llama-server, 2 × 128K, q8 KV, tensor split, MTP", time.mktime((2026, 9, 27, 6, 28, 0, 0, 0, -1)))]


def era(t):
    return max(i for i, (_, t0) in enumerate(ERAS) if t >= t0)


# ── bash commands by what they do ─────────────────────────────────────────────

PREFIX = re.compile(r"^\s*(?:env\s+|cd\s+\S+\s*(?:&&|;)\s*|[A-Z_][A-Z0-9_]*=\S*\s+|timeout\s+\S+\s+|set -\w+\s*(?:&&|;)\s*|time\s+|source \S+\s*(?:&&|;)\s*)+")
PY = r"(?:\S*/)?(?:python3?|uv run(?: python3?)?)"
BASH_CATS = [   # first match on the leading command wins
    ("tests", rf"^(?:{PY}\s+(?:-m\s+pytest|\S*test_\w+\.py|\S*/?tests?/)|pytest|go test|cargo test|npm test|make test|bash \S*smoke\S*)"),
    ("inline python", rf"^{PY}\s+(?:-\s*<<|-c\b)"),
    ("project scripts", rf"^{PY}\s+(?:-m\s+)?\S*scripts[/.]"),
    ("python other", rf"^{PY}\b"),
    ("git", r"^git\b"),
    ("GitHub API", r"^(?:gh|curl)\b"),
    ("build / install", r"^(?:uv|pip|go (?:build|install|mod|get|vet)|cargo|npm|make|rustup)\b"),
    ("edit via shell", r"^(?:sed -i|perl -pi|patch|tee)\b|^cat\s*>|^cat\s*<<.*>"),
    ("read files", r"^(?:cat|head|tail|sed|less|nl|wc|diff|jq|sqlite3|stat|file)\b"),
    ("search files", r"^(?:grep|rg|find|ls|tree|awk|du)\b"),
    ("file management", r"^(?:mkdir|rm|rmdir|cp|mv|touch|chmod|ln|tar|unzip)\b"),
    ("shell loops", r"^(?:for|while|if)\b"),
    ("status checks", r"^(?:echo|sleep|date|pwd|true|which|ps|kill|nvidia-smi|test|\[)\b"),
]
BASH_RX = [(n, re.compile(p)) for n, p in BASH_CATS]


def bash_cat(cmd):
    if re.search(r"\bsleep\s+\d{2,}|do sleep\b", cmd or ""):
        return "waiting on background jobs"
    # classify by the first segment that does something (skip echo/printf separators)
    segs = [x.strip() for x in re.split(r"\s*(?:;|&&|\|\||\n)\s*", cmd or "") if x.strip()]
    segs = [x for x in segs if not re.match(r"^(?:echo|printf|true|set|cd|export|source)\b|^\.\s|^:\s*$|^[A-Z_][A-Z0-9_]*=\S*$", x)] or segs or [""]
    c = PREFIX.sub("", segs[0] if not (cmd or "").lstrip().startswith(("for ", "while ", "if ")) else cmd).strip()
    if c.startswith(("for ", "while ")):   # a loop is what its body runs
        m = re.search(r"\bdo\s+(.*)", c, re.S)
        if m:
            inner = bash_cat(m.group(1))
            if inner != "other":
                return inner
    for n, rx in BASH_RX:
        if rx.search(c):
            return n
    return "other"


def gen_kind(b):
    """What a generated content block is: thinking, reply text, or a call of tool X (Bash: its category)."""
    if b["type"] == "thinking":
        return "thinking", len(b.get("thinking", ""))
    if b["type"] == "text":
        return "reply text", len(b.get("text", ""))
    if b["type"] == "tool_use":
        inp = b.get("input", {})
        return "call: " + b["name"], len(json.dumps(inp))
    return None, 0


# ── per-log collection (cached) ───────────────────────────────────────────────

def parse_log(log):
    """Every harness run in one transcript file: context per call, tool calls with their timing,
    generated characters by kind."""
    runs, cur, events = [], None, []

    def close():
        if cur is None or not events:
            return
        a = ctxreport.analyse(events)
        tools, gen, pending = [], Counter(), {}
        for d in events:
            if d.get("parent_tool_use_id"):
                continue
            t = ctxreport.ts(d.get("timestamp"))
            if d.get("type") == "assistant":
                for b in d["message"].get("content", []):
                    k, n = gen_kind(b)
                    if k:
                        gen[k] += n
                    if b["type"] == "tool_use":
                        cmd = (b.get("input") or {}).get("command", "")
                        pending[b["id"]] = {"name": b["name"], "cat": bash_cat(cmd) if b["name"] == "Bash" else None,
                                            "t": t, "gen": n, "cmd": cmd[:160]}
            elif d.get("type") == "user" and isinstance(d["message"].get("content"), list):
                for b in d["message"]["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                        p = pending.pop(b["tool_use_id"])
                        p["exec_s"] = round(t - p["t"], 2) if t and p["t"] else None
                        p["result"] = len(ctxreport.text_of(b.get("content")))
                        p.pop("t")
                        tools.append(p)
        cur.update(calls=[{"t": c["t"], "total": c["total"], "comp": c["comp"]} for c in a["calls"]],
                   compactions=len(a["compactions"]), retries=a["retries"], tools=tools, gen=dict(gen),
                   t0=a["t0"], t1=a["t1"], audit=a["audit"])
        if cur.get("pseudo"):
            cur["start"]["t"] = a["t0"]
            cur["end"] = {"t": a["t1"], "task_state": None}
        if cur["start"]["t"]:
            runs.append(cur)

    with open(log, errors="replace") as source:
        for line in source:
            if line.startswith('{"type":"system","subtype":"thinking_tokens"'):
                continue
            if line.startswith('{"type":"harness"'):
                d = json.loads(line)
                if d.get("event") == "start":
                    close()
                    cur, events = {"start": d, "end": None}, []
                elif d.get("event") == "end" and cur is not None and d.get("run") == cur["start"]["run"]:
                    cur["end"] = d
                continue
            if '"subtype":"init"' in line and (cur is None or cur.get("pseudo")):
                d = json.loads(line)
                if cur is None or cur["start"]["run"] != d.get("session_id"):
                    close()
                    m = re.match(r"(.+?)(?:-(w\d+))?-\d{8}-\d{6}\.jsonl$", Path(log).name)
                    cur, events = {"start": {"run": d.get("session_id"), "project": m.group(1) if m else "?",
                                             "worker": m.group(2) or "main" if m else "main", "t": None},
                                   "end": None, "pseudo": True}, []
            if cur is None or not ('"type":"assistant"' in line or '"type":"user"' in line or '"type":"system"' in line
                                   or '"type":"result"' in line):
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    close()
    return runs


def load_runs():
    CACHE.mkdir(parents=True, exist_ok=True)
    runs = []
    for log in sorted(LOGS.glob("*.jsonl")):
        if SKIP.match(log.name):
            continue
        st = log.stat()
        key = CACHE / f"{log.stem}.json"
        if key.exists():
            c = json.loads(key.read_text())
            if c.get("version") == CACHE_VERSION and c["size"] == st.st_size and c["mtime"] == st.st_mtime:
                runs += c["runs"]
                continue
        rs = parse_log(log)
        key.write_text(json.dumps({"version": CACHE_VERSION, "size": st.st_size, "mtime": st.st_mtime, "runs": rs}))
        runs += rs
    return [r for r in runs if r["start"].get("project") and not SKIP.match(r["start"]["project"]) and r["calls"]]


# ── aggregation ───────────────────────────────────────────────────────────────

def pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))]


def hist(xs, edges):
    """Counts per bucket [edges[i], edges[i+1]); the last bucket is open."""
    out = [0] * len(edges)
    for x in xs:
        if x is None:
            continue
        i = max(j for j, e in enumerate(edges) if x >= e) if x >= edges[0] else 0
        out[i] += 1
    return out


def collect():
    runs = load_runs()
    t_first = min(r["start"]["t"] for r in runs)
    t_last = max((r["end"] or {}).get("t") or r["t1"] or r["start"]["t"] for r in runs)
    now = time.time()

    # Model requests, each tagged with its agent. Before the ledger existed (Ollama era) a request
    # belongs to the one run whose window holds it.
    reqs = attribute(ollama_requests(t_first - 60, t_last + 60), ledger(t_first - 60, t_last + 60))
    windows = [(r["start"]["t"], (r["end"] or {}).get("t") or r["t1"] or now, agent_tag(r["start"])) for r in runs]
    for q in reqs:
        if q["tag"] in ("other clients", "untagged"):
            owners = {w[2] for w in windows if w[0] - 5 <= q["t"] <= w[1] + 5}
            q["tag"] = owners.pop() if len(owners) == 1 else q["tag"]
    for q in reqs:
        if q["running"] and q["t"] < now - 3600:
            q["gen_s"] = q["gen"] / q["tps"] if q["tps"] else 0.0
            q["end"], q["running"] = q["t"] + (q["prefill_s"] or 0) + q["gen_s"], False
    reqs = [q for q in reqs if q["tag"] not in ("other clients", "untagged") and not SKIP.match(q["tag"])]
    for q in reqs:
        q["era"] = era(q["t"])

    # Concurrency: was another agent's request generating while this one generated?
    by_tag = defaultdict(list)
    for q in reqs:
        by_tag[q["tag"]].append(q)
    starts = {tag: [o["t"] for o in qs] for tag, qs in by_tag.items()}   # reqs are sorted by t
    for q in reqs:
        a, b = q["t"] + (q["prefill_s"] or 0), q["end"]
        q["shared"] = False
        for tag, qs in by_tag.items():
            if tag == q["tag"]:
                continue
            i = bisect.bisect_left(starts[tag], b)   # candidates start before b; look back over the long ones
            if any(o["end"] > a for o in qs[max(0, i - 20):i]):
                q["shared"] = True
                break

    # ── per run ──
    rows = []
    for r in runs:
        st, en = r["start"], r["end"]
        t0, t1 = st["t"], (en or {}).get("t") or r["t1"] or t0
        tag = agent_tag(st)
        mine = [q for q in by_tag.get(tag, []) if t0 - 5 <= q["t"] <= t1 + 5]
        label = result(st, en)[0] if not r.get("pseudo") else "(before run records)"
        tool_s = sum(x["exec_s"] or 0 for x in r["tools"] if (x["exec_s"] or 0) < 4 * 3600)
        rows.append({
            "t0": t0, "t1": t1, "tag": tag, "worker": st.get("worker") or "main", "era": era(t0),
            "title": task_title(st.get("task") or "", st["project"])[0] if st.get("task") else "",
            "result": label, "wall_s": t1 - t0, "prefill_s": sum(q["prefill_s"] or 0 for q in mine),
            "gen_s": sum(q["gen_s"] or 0 for q in mine), "tool_s": tool_s, "requests": len(mine),
            "gen_tok": sum(q["gen"] for q in mine), "turns": len(r["calls"]), "compactions": r["compactions"],
            "peak": max(c["total"] for c in r["calls"]), "window": st.get("num_ctx") or max((q["n_ctx"] for q in mine), default=131072),
            "audit": r["audit"], "run": st["run"], "policy": st.get("context_policy", "unrecorded"),
            "harness_revision": st.get("harness_revision", "unrecorded"),
            "read_max_lines": st.get("read_max_lines"), "read_max_chars": st.get("read_max_chars"),
            "calls": r["calls"], "tools": r["tools"], "gen": r["gen"], "checklist": bool(st.get("parent")),
        })
    for x in rows:
        x["other_s"] = max(0.0, x["wall_s"] - x["prefill_s"] - x["gen_s"] - x["tool_s"])

    # ── where the time goes, by era ──
    time_split = []
    for i, (name, _) in enumerate(ERAS):
        rs = [x for x in rows if x["era"] == i]
        if rs:
            time_split.append({"era": name, "runs": len(rs), **{k: round(sum(x[k] for x in rs)) for k in
                               ("prefill_s", "gen_s", "tool_s", "other_s", "wall_s")}})

    # ── generated tokens by kind: characters calibrated against the tokens the server counted ──
    gen_chars = Counter()
    for x in rows:
        gen_chars.update(x["gen"])
    tok_total = sum(x["gen_tok"] for x in rows)
    cpt = sum(gen_chars.values()) / tok_total if tok_total else 3.5
    llama = [q for q in reqs if q["era"] >= 1 and q["tps"]]
    med_tps = statistics.median(q["tps"] for q in llama) if llama else None
    gen_kinds = sorted(({"kind": k, "tokens": round(v / cpt), "gen_min": round(v / cpt / med_tps / 60, 1) if med_tps else None}
                        for k, v in gen_chars.items()), key=lambda d: -d["tokens"])

    # ── tools: calls, generation, execution time, what the results cost in context ──
    tool_rows, bash_rows = defaultdict(lambda: Counter()), defaultdict(lambda: Counter())
    examples = defaultdict(list)
    for x in rows:
        for t in x["tools"]:
            for agg, key in ((tool_rows, t["name"]), (bash_rows, t["cat"])):
                if key is None:
                    continue
                agg[key]["calls"] += 1
                agg[key]["gen_tok"] += t["gen"] / cpt
                agg[key]["exec_s"] += min(t["exec_s"] or 0, 4 * 3600)
                agg[key]["result_tok"] += (t.get("result") or 0) / cpt
            if t["cat"] and len(examples[t["cat"]]) < 4 and t["cmd"]:
                examples[t["cat"]].append(t["cmd"][:100])
    fold = lambda agg: sorted(({"name": k, **{kk: round(vv) for kk, vv in v.items()}} for k, v in agg.items()),
                              key=lambda d: -d["exec_s"])

    # ── context over time: percentiles by minute into the run, and the average mix by turn ──
    ctx_min = defaultdict(list)
    mix_turn = defaultdict(lambda: defaultdict(list))
    cats = [c for c, _ in ctxreport.CATS]
    for x in rows:
        if x["era"] < 2:
            continue   # the window was smaller before; keep one comparable population
        for i, c in enumerate(x["calls"]):
            if c["t"]:
                ctx_min[int((c["t"] - x["t0"]) / 60)].append(c["total"])
            b = i // 5 * 5
            for k in cats:
                mix_turn[b][k].append(c["comp"].get(k, 0))
    minutes = [m for m in sorted(ctx_min) if m <= 120 and len(ctx_min[m]) >= 15]
    ctx_time = {"min": minutes, **{f"p{int(q * 100)}": [pct(ctx_min[m], q) for m in minutes] for q in (0.1, 0.5, 0.9)},
                "n": [len(ctx_min[m]) for m in minutes]}
    turns = [b for b in sorted(mix_turn) if len(mix_turn[b]["base"]) >= 15]
    ctx_mix = {"turn": turns, "n": [len(mix_turn[b]["base"]) for b in turns],
               **{k: [round(statistics.mean(mix_turn[b][k])) for b in turns] for k in cats}}
    growth = Counter()
    for x in rows:
        prev = None
        for c in x["calls"]:
            if prev:
                for k in cats:
                    d = c["comp"].get(k, 0) - prev.get(k, 0)
                    if d > 0 and c["total"] >= prev_total:
                        growth[k] += d
            prev, prev_total = c["comp"], c["total"]

    # ── one agent vs two ──
    ll = [q for q in reqs if q["era"] >= 1]
    alone = [q["tps"] for q in ll if q["tps"] and not q["shared"]]
    shared = [q["tps"] for q in ll if q["tps"] and q["shared"]]
    pp = lambda qs: [q["processed"] / q["prefill_s"] for q in qs if q["prefill_s"] and q["processed"] > 1000]
    # System throughput per 10-minute bucket, by how many agents had runs going.
    buckets = defaultdict(lambda: {"tok": 0, "agents": set(), "done": 0, "done_ck": 0})
    for q in ll:
        buckets[int(q["t"] // 600)]["tok"] += q["gen"]
    for x in rows:
        if x["era"] < 1:
            continue
        for b in range(int(x["t0"] // 600), int(x["t1"] // 600) + 1):
            buckets[b]["agents"].add(x["tag"])
        if x["result"].startswith("✔"):
            buckets[int(x["t1"] // 600)]["done"] += 1
            buckets[int(x["t1"] // 600)]["done_ck"] += x["checklist"]
    par = {}
    for n in (1, 2):
        bs = [v for v in buckets.values() if len(v["agents"]) == n and v["tok"]]
        if bs:
            par[n] = {"buckets": len(bs), "tok_per_min": round(sum(v["tok"] for v in bs) / (len(bs) * 10), 1),
                      "done_per_hour": round(sum(v["done"] for v in bs) / (len(bs) / 6), 2),
                      "steps_per_hour": round(sum(v["done_ck"] for v in bs) / (len(bs) / 6), 2)}
    tps_edges = list(range(0, 100, 5))
    one_two = {"alone_med": pct(alone, .5), "shared_med": pct(shared, .5), "alone_n": len(alone), "shared_n": len(shared),
               "pp_alone_med": pct(pp([q for q in ll if not q["shared"]]), .5), "pp_shared_med": pct(pp([q for q in ll if q["shared"]]), .5),
               "edges": tps_edges, "alone_hist": hist(alone, tps_edges), "shared_hist": hist(shared, tps_edges), "system": par}

    # ── throughput and cache reuse over time (hourly) ──
    hourly = defaultdict(lambda: {"gen": 0, "gen_s": 0.0, "prompt": 0, "reused": 0, "tags": Counter(), "prefill_s": 0.0})
    for q in reqs:
        h = hourly[int(q["t"] // 3600)]
        h["gen"] += q["gen"]
        h["gen_s"] += q["gen_s"] or 0
        h["prompt"] += q["prompt"]
        h["reused"] += q["reused"]
        h["prefill_s"] += q["prefill_s"] or 0
        h["tags"][q["tag"]] += q["gen"]
    hours = sorted(hourly)
    tags = sorted({t for h in hourly.values() for t in h["tags"]})
    over_time = {"t": [h * 3600 for h in hours], "tags": tags,
                 "gen_per_tag": [[round(hourly[h]["tags"][t] / 60, 1) for h in hours] for t in tags],   # tokens/min
                 "tps": [round(hourly[h]["gen"] / hourly[h]["gen_s"], 1) if hourly[h]["gen_s"] else None for h in hours],
                 "reuse": [round(100 * hourly[h]["reused"] / hourly[h]["prompt"], 1) if hourly[h]["prompt"] else None for h in hours],
                 "prefill_share": [round(100 * hourly[h]["prefill_s"] / max(1, hourly[h]["prefill_s"] + hourly[h]["gen_s"]), 1) for h in hours],
                 "eras": [{"name": n, "t": t0} for n, t0 in ERAS]}
    # Per-request generation speed over time (every request, for the scatter-like line).
    tps_series = {"t": [round(q["t"]) for q in reqs if q["tps"]], "tps": [round(q["tps"], 1) for q in reqs if q["tps"]],
                  "shared": [q["shared"] for q in reqs if q["tps"]]}

    # ── GPU ──
    g = gpu_samples(t_first, t_last, step=300)
    gpu_ids = sorted({i for s in g for i in s["g"]})
    raw = gpu_samples(t_first, t_last)
    util_all = [v[0] for s in raw for v in s["g"].values()]
    power_tot = [sum(v[3] for v in s["g"].values()) for s in raw]
    busy = [p for p, s in zip(power_tot, raw) if any(v[0] > 50 for v in s["g"].values())]
    gpu = {"ids": gpu_ids, "t": [s["t"] for s in g],
           "util": [[round(s["g"][i][0]) if i in s["g"] else None for s in g] for i in gpu_ids],
           "vram": [[round(s["g"][i][1], 2) if i in s["g"] else None for s in g] for i in gpu_ids],
           "power": [[round(s["g"][i][3]) if i in s["g"] else None for s in g] for i in gpu_ids],
           "cap_gb": round(max((v[2] for s in raw for v in s["g"].values()), default=24), 1),
           "util_edges": list(range(0, 101, 10)), "util_hist": hist(util_all, list(range(0, 101, 10))),
           "power_edges": list(range(0, 801, 50)), "power_hist": hist(power_tot, list(range(0, 801, 50))),
           "kwh": round(sum(power_tot) * 5 / 3.6e6, 2), "power_busy_med": pct(busy, .5),
           "hours_sampled": round(len(raw) * 5 / 3600, 1)}

    # ── distributions over runs ──
    done = [x for x in rows if x["result"].startswith("✔")]
    dist = {
        "wall_edges": [0, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180], "wall": hist([x["wall_s"] / 60 for x in rows], [0, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180]),
        "turn_edges": [0, 10, 20, 40, 60, 80, 100, 150, 200, 300], "turns": hist([x["turns"] for x in rows], [0, 10, 20, 40, 60, 80, 100, 150, 200, 300]),
        "peak_edges": list(range(0, 101, 10)), "peak": hist([100 * x["peak"] / x["window"] for x in rows if x["era"] == 2], list(range(0, 101, 10))),
        "results": Counter(x["result"] for x in rows).most_common(),
        "wall_med_done": round(statistics.median(x["wall_s"] for x in done) / 60, 1) if done else None,
        "compactions": Counter(min(x["compactions"], 3) for x in rows),
    }

    totals = {"runs": len(rows), "done": len(done), "agent_hours": round(sum(x["wall_s"] for x in rows) / 3600, 1),
              "span_hours": round((t_last - t_first) / 3600, 1), "requests": len(reqs),
              "gen_tok": sum(q["gen"] for q in reqs), "prompt_tok": sum(q["prompt"] for q in reqs),
              "processed_tok": sum(q["processed"] for q in reqs), "tool_calls": sum(len(x["tools"]) for x in rows),
              "chars_per_token": round(cpt, 2), "med_tps": round(med_tps, 1) if med_tps else None,
              "from": time.strftime("%Y-%m-%d %H:%M", time.localtime(t_first)), "to": time.strftime("%Y-%m-%d %H:%M", time.localtime(t_last)),
              "built": time.strftime("%Y-%m-%d %H:%M")}
    rb = lambda qs: round(100 * sum(q["reused"] for q in qs) / max(1, sum(q["prompt"] for q in qs)), 1)
    reuse_by_era = {"ollama": rb([q for q in reqs if q["era"] == 0]), "llama": rb([q for q in reqs if q["era"] >= 1])}
    links = ctxreport.report_links()
    audits = [{"run": x["run"], "t0": x["t0"], "title": x["title"], "tag": x["tag"], "era": x["era"],
               "report": links.get(x["run"]), **{k: x[k] for k in ("policy", "harness_revision", "read_max_lines", "read_max_chars")}, **{k: x["audit"][k] for k in contextaudit.FIELDS}} for x in rows]
    return {"audit": contextaudit.aggregate(x["audit"] for x in rows), "audit_runs": audits, "totals": totals, "time_split": time_split, "reuse_by_era": reuse_by_era, "gen_kinds": gen_kinds, "tools": fold(tool_rows),
            "bash": fold(bash_rows), "bash_examples": dict(examples), "ctx_time": ctx_time, "ctx_mix": ctx_mix,
            "cats": [{"key": k, "name": n} for k, n in ctxreport.CATS], "growth": dict(growth), "one_two": one_two,
            "over_time": over_time, "tps_series": tps_series, "gpu": gpu, "dist": dist,
            "runs": [{k: x[k] for k in ("run", "policy", "harness_revision", "t0", "tag", "era", "title", "result", "wall_s", "prefill_s", "gen_s", "tool_s",
                                        "other_s", "requests", "gen_tok", "turns", "compactions", "peak", "window")} for x in rows]}


def write_csv(d):
    out = OUT / "study"
    out.mkdir(parents=True, exist_ok=True)
    tables = {"runs.csv": d["runs"], "tools.csv": d["tools"], "bash.csv": d["bash"], "generated.csv": d["gen_kinds"],
              "time_split.csv": d["time_split"], "context_audit.csv": d["audit_runs"]}
    for name, rows in tables.items():
        if rows:
            with open(out / name, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
    ot = d["over_time"]
    with open(out / "hourly.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["hour_start", "gen_tps", "cache_reuse_pct", "prefill_share_pct", *[f"tok_per_min_{t}" for t in ot["tags"]]])
        for i, t in enumerate(ot["t"]):
            w.writerow([time.strftime("%Y-%m-%d %H:%M", time.localtime(t)), ot["tps"][i], ot["reuse"][i], ot["prefill_share"][i],
                        *[s[i] for s in ot["gen_per_tag"]]])


def main():
    d = collect()
    if "--json" in sys.argv:
        print(json.dumps({k: v for k, v in d.items() if k not in ("runs", "tps_series", "gpu", "over_time")}, indent=1))
        return
    OUT.mkdir(exist_ok=True)
    (OUT / "study.json").write_text(json.dumps(d))
    write_csv(d)
    (OUT / "study.html").write_text(reportui.page("Loop study", d, contextaudit.JS + TEMPLATE_JS))
    print(OUT / "study.html")


TEMPLATE_JS = r"""const T=D.totals,OT=D.over_time,G=D.gpu,O=D.one_two,DI=D.dist;
const dt=t=>{const d=new Date(t*1000);return d.toLocaleDateString('en-US',{month:'short',day:'numeric'})+' '+String(d.getHours()).padStart(2,'0')+':00'};
const M=v=>v>=1e6?(v/1e6).toFixed(1)+'M':k(v),ds=t=>{const d=new Date(t*1000);return d.getDate()+'·'+String(d.getHours()).padStart(2,'0')+'h'};
const hrs=s=>(s/3600).toFixed(1)+' h',pc=(a,b)=>b?Math.round(100*a/b)+'%':'–';
const P=(...xs)=>xs.map(x=>`<p style="max-width:900px;margin:6px 0">${x}</p>`).join('');
const eraMarks=xs=>[{i:xs.findIndex(t=>t>=OT.eras[1].t),label:'→ llama-server'}].filter(m=>m.i>=0);
let h=`<h1>Loop study: where a local coding agent spends its time</h1><div class="sub">${T.runs} runs (${T.done} tasks done) over ${T.span_hours} h,
 ${T.from} → ${T.to} local time · ${T.agent_hours} agent-hours · built ${T.built} · <a href="index.html">all run reports</a> · <a href="fleet.html">fleet</a>
 · data: <a href="study.json">study.json</a>, <a href="study/runs.csv">runs.csv</a>, <a href="study/hourly.csv">hourly.csv</a>, <a href="study/tools.csv">tools.csv</a>, <a href="study/bash.csv">bash.csv</a>, <a href="study/generated.csv">generated.csv</a></div>`;
h+='<div class="tiles">'+[tile('Model requests',k(T.requests),`${M(T.gen_tok)} tokens generated`),
 tile('Prompt tokens',M(T.prompt_tok),`only ${M(T.processed_tok)} (${pc(T.processed_tok,T.prompt_tok)}) actually re-read`),
 tile('Median speed',T.med_tps+' tok/s','per request, llama-server era'),tile('Tool calls',k(T.tool_calls),'Bash, Read, Edit, Write, …'),
 tile('Energy',G.kwh+' kWh',`both GPUs, ${G.hours_sampled} h sampled · ~${Math.round(G.power_busy_med)} W busy`),
 tile('Median task',DI.wall_med_done+' min','wall clock, tasks that finished')].join('')+'</div>';
h+=card('The setup and the problem','',P(
 `One box, two RTX 3090s (24 GB each), one local model (Qwen 27B, 8-bit) serving Claude Code as the agent. The loop hands each agent one TODO line per run, in a sandbox, and commits the result; the project here (contrib-loop) builds a pipeline and then uses it to find, reproduce and fix bugs in public ML-infrastructure repos.`,
 `Everything the agents do passes through that one model, so <b>the loop is as fast as the GPUs can read prompts and write tokens</b>. The questions this page answers: where the wall-clock time goes, what the model spends its tokens on, how the context fills up, whether a second agent helps, and what the hardware is doing. Every number is measured from files the harness writes anyway (transcripts, the model server's journal, a per-request ledger, 5-second GPU samples).`));

// 1. over time
h+=card('1 · Throughput over three days','Tokens generated per minute, each hour, stacked by agent. The line marks the switch from Ollama to llama-server (Sep 27 04:13; the 2 × 128K setup followed at 06:28).','<div id="thr"></div>'+legend(OT.tags.map((t,i)=>({n:t,c:col(i)}))))+
 '<div class="grid2">'+card('Prompt cache reuse','% of prompt tokens the server did not have to re-read, per hour','<div id="reuse"></div>')+
 card('Generation speed per request','Hourly average tokens/s of one request','<div id="tpsh"></div>')+'</div>'+
 card('What changed, and why these charts move','',P(
 `<b>Day 1 (Sep 26) on Ollama:</b> a single agent at ~25 tok/s, and almost no prompt caching (0–30%, often 0). Qwen's hybrid attention made Ollama throw away its cache, so every turn re-read the whole conversation — up to 60% of model time in some hours was just re-reading the prompt. Long tool calls also left the stream silent long enough to trip Claude Code's 5-minute idle watchdog, which aborted and retried requests; a small proxy (keepalive.py) that sends ping events fixed that.`,
 `<b>Sep 27 04:13, llama-server:</b> Ollama could not run two requests at once for this model, so the harness switched to the llama.cpp server it ships with, with two real slots and host-RAM context checkpoints. Cache reuse jumped to <b>95–98%</b>: a turn now only reads what was added since the last one. 96K per slot made Claude Code compact every few minutes, so at 06:28 the KV cache went to 8-bit to fit <b>2 × 128K</b>, the model was split across both GPUs (tensor parallel) and multi-token prediction (speculative decoding) turned on.`,
 `<b>Speed plateaus:</b> ~35–40 tok/s per request while both agents work, ~65–70 tok/s when only one does (Sep 27 23:00 → Sep 28 12:00, when the second agent had nothing it was allowed to start). Section 5 explains why two agents add only ~20%.`));

// 2. time split
const TS=D.time_split,tcols=[['Prefill (reading the prompt)','prefill_s',col(0)],['Generation (writing tokens)','gen_s',col(1)],['Tools running','tool_s',col(2)],['Harness + overhead','other_s',col(6)]];
h+=card('2 · Where an agent\'s wall-clock time goes','Summed over all runs of each era; each agent counted on its own clock (two agents = two clocks).',
 TS.map(e=>`<div style="margin:10px 0 2px"><b>${esc(e.era)}</b> <span class="note">${e.runs} runs · ${hrs(e.wall_s)}</span></div><div class="bar100">`+
 tcols.map(c=>`<div style="flex:${e[c[1]]};background:${c[2]}" data-t="${esc(c[0])}: ${hrs(e[c[1]])} (${pc(e[c[1]],e.wall_s)})"></div>`).join('')+'</div>').join('')+
 legend(tcols.map(c=>({n:c[0],c:c[2]})))+'<table><tr><th>Era</th>'+tcols.map(c=>`<th class="num">${esc(c[0].split(' (')[0])}</th>`).join('')+'</tr>'+
 TS.map(e=>`<tr><td>${esc(e.era)}</td>`+tcols.map(c=>`<td class="num">${pc(e[c[1]],e.wall_s)}</td>`).join('')+'</tr>').join('')+'</table>'+
 P(`<b>Reading it:</b> with the cache working, an agent spends about <b>${pc(TS[TS.length-1].gen_s,TS[TS.length-1].wall_s)} of its time waiting for the model to write</b>, ~${pc(TS[TS.length-1].prefill_s,TS[TS.length-1].wall_s)} for it to read, and only ~${pc(TS[TS.length-1].tool_s,TS[TS.length-1].wall_s)} actually running tools (tests, git, scripts). On Ollama, prompt reading was ${pc(TS[0].prefill_s,TS[0].wall_s)} and overhead ${pc(TS[0].other_s,TS[0].wall_s)} (retries, aborted streams, restarts).`,
 `<b>Implication:</b> the agent is <b>generation-bound</b>. Faster tools or a faster sandbox would barely matter; what matters is how many tokens the model writes per task (section 3) and how fast the GPUs write them. Prefix caching greatly reduces prefill work; it does not remove context occupancy, compaction costs, or the risk of losing relevant evidence. These observations are not a controlled before/after experiment.`));

// 3. generated tokens
const GK=D.gen_kinds,gmax=Math.max(...GK.map(g=>g.tokens)),gtot=GK.reduce((a,g)=>a+g.tokens,0);
h+=card('3 · What the model spends its output on',`Every generated token by kind (${k(gtot)} total; tokens estimated from characters at ${T.chars_per_token} chars/token, calibrated against the server's counts). Minutes = GPU generation time at the median speed.`,
 GK.filter(g=>g.tokens>1000).map(g=>`<div class="hb" style="grid-template-columns:minmax(0,1fr) 260px 150px" data-t="${esc(g.kind)}: ${k(g.tokens)} tokens"><span class="l">${esc(g.kind)}</span><span><div class="b" style="width:${100*g.tokens/gmax}%;background:${g.kind==='thinking'?col(5):g.kind.startsWith('call')?col(1):col(6)}"></div></span><span class="num">${k(g.tokens)} · ${pc(g.tokens,gtot)} · ${mins(g.gen_min)}</span></div>`).join('')+
 P(`<b>Thinking is ${pc((GK.find(g=>g.kind==='thinking')||{}).tokens||0,gtot)} of everything the model writes</b> — about ${mins((GK.find(g=>g.kind==='thinking')||{}).gen_min||0)} of pure GPU time. The tool calls themselves are next: Bash commands, then the file contents of Write and the old/new text of Edit. The replies a human would read are a few percent.`,
 `<b>Implication:</b> the cheapest speedup is less thinking per turn (reasoning budget, or a checklist step small enough that there is little to deliberate), not faster tools. Edit is a cheap way to change a file compared with Write, which regenerates it whole; Write's per-call cost is ${k((D.tools.find(t=>t.name==='Write')||{}).gen_tok/Math.max(1,(D.tools.find(t=>t.name==='Write')||{}).calls))} tokens vs ${k((D.tools.find(t=>t.name==='Edit')||{}).gen_tok/Math.max(1,(D.tools.find(t=>t.name==='Edit')||{}).calls))} for Edit.`));

h+=contextAudit(D.audit);
h+=card('Runs to inspect for large tool output','Sorted by oversized-result count, then result characters; compare like tasks and eras before drawing performance conclusions. <a href="study/context_audit.csv">Download context audit CSV</a>',
 '<table><tr><th>Task / worker</th><th>Repeat chars</th><th>Large results</th><th>Errors</th></tr>'+[...D.audit_runs].sort((a,b)=>b.large_results-a.large_results||b.result_chars-a.result_chars).slice(0,15).map(r=>`<tr><td>${r.report?`<a href="${esc(r.report)}">${esc(r.title||r.run)}</a>`:esc(r.title||r.run)} · ${esc(r.tag)}</td><td>${k(r.repeat_chars)}</td><td>${r.large_results}</td><td>${r.errors}</td></tr>`).join('')+'</table>');

// 4. tools
const trow=t=>`<tr><td>${esc(t.name)}</td><td class="num">${t.calls}</td><td class="num">${k(t.gen_tok)}</td><td class="num">${mins(t.exec_s/60)}</td><td class="num">${f1(t.exec_s/Math.max(1,t.calls))} s</td><td class="num">${k(t.result_tok)}</td></tr>`;
const thead='<table><tr><th>Tool</th><th class="num">Calls</th><th class="num">Tokens to write the call</th><th class="num">Time running</th><th class="num">Per call</th><th class="num">Result tokens into context</th></tr>';
h+=card('4 · Tools: cost to call, time to run, context they add','Sorted by time running.','<div style="overflow-x:auto">'+thead+D.tools.map(trow).join('')+'</table></div>')+
 card('Bash commands by what they do','Classified by the leading command (a loop by its body; sleep-polling as waiting).','<div style="overflow-x:auto">'+thead.replace('Tool','Kind')+D.bash.map(trow).join('')+'</table></div>'+
 '<details><summary>Examples per kind</summary><div class="scroll">'+Object.entries(D.bash_examples).map(([k2,v])=>`<div style="margin-top:6px"><b>${esc(k2)}</b>${v.map(c=>`<div class="mono note" style="margin:1px 0">${esc(c)}</div>`).join('')}</div>`).join('')+'</div></details>')+
 card('What the tool data says','',P(
 `<b>Bash is the agent's hands:</b> about two thirds of all tool calls. By count the agent mostly <i>looks</i> (cat/sed/head, grep/ls/find, git) — and those looks are where the context goes: file reads and searches put the most result tokens into the conversation, which later turns must carry (the "Context at the end" card of each run report shows this per run).`,
 `<b>By time</b> the tools that matter are the slow ones: running test suites and the project's own scripts (GitHub sync), and <b>waiting on background jobs</b> — the agent starting a long test in the background and polling it with sleep. Those waits are cheap in tokens but block the agent; with two agents the other one keeps the GPU busy meanwhile.`,
 `<b>Inline python</b> (python - &lt;&lt;EOF) is the agent's way of querying SQLite or patching files programmatically: few calls, but many generated tokens each, like Write.`));

// 5. context
const CT=D.ctx_time,CM=D.ctx_mix,gsum=Object.values(D.growth).reduce((a,b)=>a+b,0);
h+=card('5 · Context over a run, across all runs','Prompt size by minutes into the run (128K-window era): the median run and the 10th/90th percentiles. The population shrinks with time (most runs end within 10 min).',
 '<div id="ctxt"></div>'+legend([{n:'90th percentile',c:col(1)},{n:'median',c:col(0)},{n:'10th percentile',c:col(2)}]))+
 '<div class="grid2">'+card('What the context is made of, by turn','Average tokens per category at each turn (runs that got that far). Resets after compaction pull the later averages down.',
 '<div id="mix"></div>'+legend(D.cats.map((c,i)=>({n:c.name,c:col(i)}))))+
 card('What made the context grow','All growth over all runs, by category',
 '<div class="bar100">'+D.cats.filter(c=>D.growth[c.key]).map((c,i)=>`<div style="flex:${D.growth[c.key]};background:${col(D.cats.indexOf(c))}" data-t="${esc(c.name)}: ${k(D.growth[c.key])} (${pc(D.growth[c.key],gsum)})"></div>`).join('')+'</div>'+
 '<table>'+D.cats.filter(c=>D.growth[c.key]).sort((a,b)=>D.growth[b.key]-D.growth[a.key]).map(c=>`<tr><td><span class="sw" style="background:${col(D.cats.indexOf(c))}"></span>${esc(c.name)}</td><td class="num">${k(D.growth[c.key])}</td><td class="num">${pc(D.growth[c.key],gsum)}</td></tr>`).join('')+'</table>'+
 '<div class="note" style="margin-top:10px">Peak context per run, % of the 128K window</div><div id="peak"></div>')+'</div>'+
 card('How compaction works, and what the context data means','',P(
 `A run starts at ~${k(CM.base[0])} tokens before the agent does anything: Claude Code's system prompt, the ${23} tool definitions, CLAUDE.md, the task prompt and NOTES.md. Each turn then appends the model's thinking, its tool call, and the tool's result; nothing is removed.`,
 `<b>Compaction:</b> Claude Code watches the prompt size. The harness sets <span class="mono">CLAUDE_CODE_AUTO_COMPACT_WINDOW = NUM_CTX − CTX_MARGIN</span> (131072 − 8192); Claude Code compacts when the conversation gets within its own buffer (~33K) of that, i.e. around 90K tokens here. It then makes one extra model call that summarises the conversation so far, replaces the history with that summary, and continues. The report shows it as a cliff in the context chart. It costs a long generation, and details the summary drops are gone — which is why the loop keeps runs short: one task (or one checklist step) per run, and a fresh session each time with NOTES.md as the hand-off. ${DI.compactions['0']||0} of ${T.runs} runs never compacted.`,
 `<b>Implication:</b> thinking (${pc(D.growth.think||0,gsum)}) and tool output (Bash ${pc(D.growth.bash||0,gsum)}, Read ${pc(D.growth.read||0,gsum)}) fill the window; the model's own thinking stays in context until compaction. Peaks sit at 20–60% of 128K, so the bigger window mostly buys fewer compactions rather than being used up.`));

// 6. one vs two
const sys=O.system;
h+=card('6 · One agent or two?','% of requests at each generation speed (5 tok/s buckets), split by whether another agent\'s request was generating at the same time (llama-server era).',
 '<div id="o2"></div>'+legend([{n:`alone (${O.alone_n} requests, median ${f1(O.alone_med)} tok/s)`,c:col(0)},{n:`sharing the GPUs (${O.shared_n}, median ${f1(O.shared_med)} tok/s)`,c:col(1)}])+
 '<table style="margin-top:10px"><tr><th>Agents with a run going</th><th class="num">10-min periods</th><th class="num">Tokens generated / min (all agents)</th><th class="num">Tasks done / hour</th><th class="num">Checklist steps done / hour</th></tr>'+
 Object.entries(sys).map(([n,v])=>`<tr><td>${n}</td><td class="num">${v.buckets}</td><td class="num">${k(v.tok_per_min)}</td><td class="num">${v.done_per_hour}</td><td class="num">${v.steps_per_hour}</td></tr>`).join('')+'</table>'+
 P(`<b>Per request, sharing halves the speed</b>: ${f1(O.alone_med)} → ${f1(O.shared_med)} tok/s (prompt reading ${k(O.pp_alone_med)} → ${k(O.pp_shared_med)} tok/s). Two requests at ~${f1(O.shared_med)} each is ~${f1(2*O.shared_med)} tok/s in total, only a little above one alone. Measured over whole periods, two agents produced <b>${sys[2]&&sys[1]?(sys[2].tok_per_min/sys[1].tok_per_min).toFixed(2):'–'}×</b> the tokens of one.`,
 `<b>Why so little:</b> generating a token for one request mostly means streaming all 27B weights from VRAM; a second request can share that pass, which in an earlier benchmark (no speculative decoding) gave 1.78× aggregate. Multi-token prediction already fills that spare capacity for a single request — it drafts several tokens per pass — so the two gains overlap. A second agent also waits on tools less visibly: while one runs tests, the other has the GPU to itself.`,
 `<b>Tasks per hour is not a fair comparison here</b>: the one-agent hours were mostly short issue-checklist steps (the second agent had nothing it was allowed to start), the two-agent hours mostly longer pipeline-building tasks. The fair unit is tokens per minute. <b>Implication:</b> a second agent is worth ~20% more throughput and, more importantly, keeps the GPU busy while the other waits on tests; it does not double speed on this hardware. More useful work per hour comes from fewer tokens per task.`));

// 7. GPU
const gid=G.ids.map((g,i)=>({n:'GPU '+g,c:col(i)}));
h+='<div class="grid3">'+card('7 · GPU utilization','% per GPU, 5-minute means','<div id="gu"></div>'+legend(gid))+
 card('VRAM','GB per GPU, of '+G.cap_gb+' GB','<div id="gv"></div>'+legend(gid))+card('Power','W per GPU, 5-minute means (each card is capped at 250 W)','<div id="gp"></div>'+legend(gid))+'</div>'+
 '<div class="grid2">'+card('Utilization distribution','Share of 5 s samples (both GPUs)','<div id="guh"></div>')+card('Power distribution','Both GPUs together, 5 s samples','<div id="gph"></div>')+'</div>'+
 card('What the hardware says','',P(
 `<b>The GPUs are saturated whenever an agent is working</b>: most samples sit at 90–100% utilization, drawing ~${Math.round(G.power_busy_med)} W for the pair (the 2 × 250 W cap) — ${G.kwh} kWh over ${G.hours_sampled} sampled hours. The low-power samples are the gaps between runs and the tool-running stretches.`,
 `<b>The cards run at their power cap</b>: both are limited to 250 W (their default is 350 W), and busy samples sit right at it. Decoding one token mostly streams weights from memory, which is less power-hungry than prefill; raising the cap would speed up prompt reading and speculative decoding more than plain generation, at ~40% more power. Worth measuring before assuming.`,
 `<b>VRAM is nearly full on purpose</b>: ~20–22.6 GB of 24 GB per card holds half the model's weights each (tensor split), the 8-bit KV cache for two 128K slots, and compute buffers. That is the budget that decided 2 × 128K over 2 × 96K at 16-bit, and it is why a third agent (a third slot) is not possible without shrinking the window.`));

// 8. distributions
const hb=(id,x,v,fmt)=>chart(document.getElementById(id),{x,bars:true,H:150,series:[{n:'runs',c:col(0),v}],xfmt:fmt,yfmt:v=>Math.round(v)});
h+='<div class="grid3">'+card('8 · Run length','Runs per wall-clock bucket (minutes)','<div id="dw"></div>')+card('Turns per run','Model calls per run','<div id="dt"></div>')+
 card('Results','What became of each run\'s task','<table>'+DI.results.map(r=>`<tr><td>${esc(r[0])}</td><td class="num">${r[1]}</td></tr>`).join('')+'</table>')+'</div>'+
 card('Sources and method','',P(`Runs are framed by the harness's start/end lines in logs/*.jsonl (Sep 26 sessions predate them and count as one run per session). Model requests come from llama.cpp's own timings in the journal, attributed to agents by the keepalive ledger (before it existed, by run windows). "Sharing" = another agent's request was generating during this one's generation. Tool time = tool call to tool result in the transcript. Harness + overhead = wall time minus the three measured parts (retries, merges, reports, gaps). Rebuild with <span class="mono">.venv/bin/python lib/study.py</span>.`));
app.innerHTML=h;
document.querySelectorAll('[data-t]').forEach(n=>{n.addEventListener('mousemove',e=>showTip(e,esc(n.dataset.t)));n.addEventListener('mouseleave',hideTip)});
chart(document.getElementById('thr'),{x:OT.t,stacked:true,H:240,xfmt:dt,yfmt:k,marks:eraMarks(OT.t),tipx:i=>`<b>${dt(OT.t[i])}</b> · tokens/min`,
 series:OT.tags.map((t,i)=>({n:t,c:col(i),v:OT.gen_per_tag[i]}))});
chart(document.getElementById('reuse'),{x:OT.t,H:170,xfmt:dt,ymax:100,yfmt:v=>Math.round(v)+'%',marks:eraMarks(OT.t),series:[{n:'reused',c:col(2),v:OT.reuse}]});
chart(document.getElementById('tpsh'),{x:OT.t,H:170,xfmt:dt,yfmt:v=>Math.round(v),marks:eraMarks(OT.t),series:[{n:'tok/s',c:col(1),v:OT.tps}]});
chart(document.getElementById('ctxt'),{x:CT.min,H:240,limit:131072,xlabel:'min into run',xfmt:v=>v+'m',tipx:i=>`<b>${CT.min[i]} min in</b> · ${CT.n[i]} runs`,
 series:[{n:'90th percentile',c:col(1),v:CT.p90},{n:'median',c:col(0),v:CT.p50},{n:'10th percentile',c:col(2),v:CT.p10}]});
chart(document.getElementById('mix'),{x:CM.turn,stacked:true,H:240,xlabel:'turn',tipx:i=>`<b>turn ${CM.turn[i]}</b> · ${CM.n[i]} runs`,series:D.cats.map((c,i)=>({n:c.name,c:col(i),v:CM[c.key]}))});
hb('peak',DI.peak_edges,DI.peak,v=>v+'%');
chart(document.getElementById('o2'),{x:O.edges,H:200,xfmt:v=>v,xlabel:'tok/s',tipx:i=>`<b>${O.edges[i]}–${O.edges[i]+5} tok/s</b>`,
 yfmt:v=>Math.round(v)+'%',series:[{n:'alone',c:col(0),v:O.alone_hist.map(v=>100*v/Math.max(1,O.alone_n))},{n:'sharing',c:col(1),v:O.shared_hist.map(v=>100*v/Math.max(1,O.shared_n))}]});
const gx={x:G.t,H:170,xfmt:ds,tipx:i=>`<b>${dt(G.t[i])}</b>`};
chart(document.getElementById('gu'),{...gx,ymax:100,yfmt:v=>Math.round(v)+'%',series:G.ids.map((g,i)=>({n:'GPU '+g,c:col(i),v:G.util[i]}))});
chart(document.getElementById('gv'),{...gx,ymax:G.cap_gb,yfmt:v=>f1(v),series:G.ids.map((g,i)=>({n:'GPU '+g,c:col(i),v:G.vram[i]}))});
chart(document.getElementById('gp'),{...gx,yfmt:v=>Math.round(v)+'W',series:G.ids.map((g,i)=>({n:'GPU '+g,c:col(i),v:G.power[i]}))});
const us=G.util_hist.reduce((a,b)=>a+b,0),ps=G.power_hist.reduce((a,b)=>a+b,0);
chart(document.getElementById('guh'),{x:G.util_edges,bars:true,H:150,xfmt:v=>v+'%',yfmt:v=>Math.round(v)+'%',series:[{n:'samples',c:col(0),v:G.util_hist.map(v=>100*v/us)}]});
chart(document.getElementById('gph'),{x:G.power_edges,bars:true,H:150,xfmt:v=>v+'W',yfmt:v=>Math.round(v)+'%',series:[{n:'samples',c:col(3),v:G.power_hist.map(v=>100*v/ps)}]});
hb('dw',DI.wall_edges,DI.wall,v=>v+'m');hb('dt',DI.turn_edges,DI.turns,v=>v);
"""

if __name__ == "__main__":
    main()
