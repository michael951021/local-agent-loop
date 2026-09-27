#!/usr/bin/env python3
"""Context + GPU report for one agent run, as a self-contained HTML page.

  ctxreport.py LOG.jsonl [--session SID] [--trigger end|compact|manual]
  ctxreport.py --session SID [--trigger ...]        find the log that holds SID
  ctxreport.py --index                              only rebuild reports/index.html

Writes reports/<project>/<start>-<sid8>-<trigger>.html and refreshes reports/index.html.
Run automatically by pretty.py at the end of every run and on every compaction.

Context attribution: each API call reports the real prompt size (usage.input_tokens). The growth
between two calls is split across what was added in between (tool results, tool inputs, thinking,
replies) in proportion to their character counts, so totals are exact and splits are estimates.
"""
import argparse
import html
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS, REPORTS = ROOT / "logs", ROOT / "reports"

# Fixed category order = fixed colour slot (a category keeps its colour in every report).
CATS = [
    ("base", "System + tools + task"),
    ("read", "Read results"),
    ("bash", "Bash output"),
    ("edit", "Write / Edit"),
    ("search", "Grep / Glob / web"),
    ("think", "Thinking"),
    ("say", "Replies + tool calls"),
    ("other", "Hooks, reminders, summaries"),
]
TOOL_CAT = {"Read": "read", "Bash": "bash", "BashOutput": "bash", "Write": "edit", "Edit": "edit",
            "MultiEdit": "edit", "NotebookEdit": "edit", "Grep": "search", "Glob": "search",
            "WebFetch": "search", "WebSearch": "search"}


def tool_cat(name):
    return TOOL_CAT.get(name) or ("search" if name.startswith("mcp__websearch") else "other")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


def num_ctx():
    m = re.search(r"^NUM_CTX=(\d+)", (ROOT / "config.env").read_text(), re.M)
    return int(m.group(1)) if m else 131072


def short(s, n=70):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def text_of(content):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content or [])


# ── stream log ────────────────────────────────────────────────────────────────

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


def sessions_in(log):
    seen = []
    for line in open(log, errors="replace"):
        if '"subtype":"init"' in line:
            sid = json.loads(line).get("session_id")
            if sid and sid not in seen:
                seen.append(sid)
    return seen


def analyse(events):
    """Walk the main thread and attribute context growth per API call."""
    calls, items, pending = [], [], []   # pending: [cat, label, chars] added since the last call
    tools_by_id, tool_stats = {}, defaultdict(lambda: {"calls": 0, "tokens": 0.0})
    comp = {c: 0.0 for c, _ in CATS}
    compactions, retries, result, last_mid, t_first, t_last = [], 0, None, None, None, None

    def settle(total, t):
        nonlocal comp
        prev = calls[-1]["total"] if calls else 0
        delta, chars = total - prev, sum(p[2] for p in pending)
        if not calls:
            comp["base"] = total
        elif delta < 0 or (compactions and compactions[-1]["turn"] is None):
            # Compaction: everything but the base collapses into the summary.
            if compactions and compactions[-1]["turn"] is None:
                compactions[-1]["turn"] = len(calls)
            else:
                compactions.append({"turn": len(calls), "pre": prev, "trigger": "detected"})
            comp = {c: 0.0 for c, _ in CATS}
            comp["base"] = min(calls[0]["total"], total)
            comp["other"] = total - comp["base"]
        elif chars:
            for cat, label, n, tool in pending:
                tok = delta * n / chars
                comp[cat] += tok
                items.append({"cat": cat, "label": label, "tokens": tok, "turn": len(calls)})
                if tool:
                    tool_stats[tool]["tokens"] += tok
        else:
            comp["other"] += delta
        pending.clear()
        calls.append({"turn": len(calls), "t": t, "total": total, "delta": delta,
                      "comp": {c: round(v) for c, v in comp.items()}})

    for d in events:
        if d.get("parent_tool_use_id"):
            continue   # sub-agent context is separate from the main thread
        kind, sub = d.get("type"), d.get("subtype")
        t = ts(d.get("timestamp"))
        if t:
            t_first, t_last = t_first or t, t
        if kind == "system" and sub == "compact_boundary":
            meta = d.get("compact_metadata") or {}
            compactions.append({"turn": None, "pre": meta.get("pre_tokens"), "trigger": meta.get("trigger", "auto")})
        elif kind == "system" and sub == "api_retry":
            retries += 1
        elif kind == "system" and sub == "hook_response":
            pending.append(["other", f"hook: {d.get('hook_name')}", len(d.get("output") or ""), None])
        elif kind == "result":
            result = d
        elif kind == "assistant":
            m = d["message"]
            if m.get("id") != last_mid:
                last_mid = m.get("id")
                settle((m.get("usage") or {}).get("input_tokens", 0), t)
            for b in m.get("content", []):
                if b["type"] == "thinking":
                    pending.append(["think", "thinking", len(b.get("thinking", "")), None])
                elif b["type"] == "text":
                    pending.append(["say", "reply: " + short(b["text"], 50), len(b["text"]), None])
                elif b["type"] == "tool_use":
                    name, inp = b["name"], b.get("input", {})
                    arg = inp.get("file_path") or inp.get("command") or inp.get("pattern") or inp.get("url") or ""
                    tools_by_id[b["id"]] = (name, arg)
                    tool_stats[name]["calls"] += 1
                    cat = "edit" if tool_cat(name) == "edit" else "say"
                    pending.append([cat, f"{name} call {short(arg, 50)}", len(json.dumps(inp)), name])
        elif kind == "user":
            content = d["message"].get("content")
            for b in content if isinstance(content, list) else [{"type": "text", "text": content}]:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    name, arg = tools_by_id.get(b.get("tool_use_id"), ("?", ""))
                    pending.append([tool_cat(name), f"{name} {short(arg, 60)}", len(text_of(b.get("content"))), name])
                elif b.get("type") == "text":
                    pending.append(["other", "note: " + short(b["text"], 50), len(b.get("text") or ""), None])

    # Merge repeat items (e.g. the same file read twice) for the top-consumer list.
    merged = defaultdict(lambda: {"tokens": 0.0, "turns": []})
    for it in items:
        key = (it["cat"], "thinking (all turns)" if it["cat"] == "think" else it["label"])
        merged[key]["tokens"] += it["tokens"]
        merged[key]["turns"].append(it["turn"])
    top = sorted(({"cat": k[0], "label": k[1], "tokens": round(v["tokens"]), "turns": v["turns"]}
                  for k, v in merged.items()), key=lambda x: -x["tokens"])[:12]
    per_turn_top = {}
    for it in items:
        cur = per_turn_top.get(it["turn"])
        if not cur or it["tokens"] > cur["tokens"]:
            per_turn_top[it["turn"]] = it
    for c in calls:
        it = per_turn_top.get(c["turn"])
        c["added"] = f'{it["label"]} ({it["tokens"] / 1000:.1f}k)' if it else ""
    tools = sorted(({"name": k, **v, "tokens": round(v["tokens"])} for k, v in tool_stats.items()),
                   key=lambda x: -x["tokens"])
    return {"calls": calls, "top": top, "tools": tools, "compactions": [c for c in compactions if c["turn"] is not None],
            "retries": retries, "result": result, "t0": t_first, "t1": t_last}


# ── Ollama journal ────────────────────────────────────────────────────────────

RX = {
    "start": re.compile(r"task (\d+) \| new prompt, .*task\.n_tokens = (\d+)"),
    "full": re.compile(r"task (\d+) \| forcing full prompt re-processing"),
    "prefill": re.compile(r"task (\d+) \| prompt eval time =\s*([\d.]+) ms /\s*(\d+) tokens"),
    "gen": re.compile(r"task (\d+) \|\s+eval time =\s*([\d.]+) ms /\s*(\d+) tokens"),
    "progress": re.compile(r"task (\d+) \| n_gen =\s*(\d+), tg =\s*([\d.]+)"),
    "pp": re.compile(r"task (\d+) \| prompt processing, n_tokens =\s*(\d+), progress = [\d.]+, t =\s*([\d.]+) s"),
    "end": re.compile(r"task (\d+) \| stop processing: n_tokens = (\d+)"),
    "cancel": re.compile(r"cancel task, id_task = (\d+)"),
}


def ollama_requests(t0, t1):
    try:
        out = subprocess.run(["journalctl", "-u", "ollama", "--no-pager", "-o", "short-unix",
                              "--since", f"@{int(t0) - 30}", "--until", f"@{int(t1) + 30}"],
                             capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    reqs = {}
    for line in out.splitlines():
        if "task" not in line:
            continue
        try:
            t = float(line.split(" ", 1)[0])
        except ValueError:
            continue
        for key, rx in RX.items():
            m = rx.search(line)
            if not m:
                continue
            tid = m.group(1)
            if key == "start":
                reqs[tid] = {"t": t, "prompt": int(m.group(2)), "processed": None, "prefill_s": None,
                             "gen": 0, "gen_s": 0.0, "tps": None, "full": False, "cancelled": False, "end": None}
                continue
            r = reqs.get(tid)
            if not r:
                break
            if key == "full":
                r["full"] = True
            elif key == "prefill":
                r["prefill_s"], r["processed"] = float(m.group(2)) / 1000, int(m.group(3))
            elif key == "gen":
                r["gen_s"], r["gen"] = float(m.group(2)) / 1000, int(m.group(3))
                r["tps"] = r["gen"] / r["gen_s"] if r["gen_s"] else None
            elif key == "progress":
                r["gen"], r["tps"] = int(m.group(2)), float(m.group(3))
            elif key == "pp":
                r["pp_last"] = (t, float(m.group(3)))
            elif key == "end":
                r["end"] = t
            elif key == "cancel":
                r["cancelled"] = True
            break
    res = []
    for r in reqs.values():
        if not (t0 - 5 <= r["t"] <= t1 + 5):
            continue
        end = r["end"] or r["t"]
        if r["prefill_s"] is None:   # cancelled before the summary line: estimate from progress lines
            r["prefill_s"] = r.get("pp_last", (0, 0.0))[1]
            r["gen_s"] = max(0.0, end - r["t"] - r["prefill_s"])
        r["processed"] = r["processed"] if r["processed"] is not None else r["prompt"]
        r["reused"] = max(0, r["prompt"] - r["processed"])
        r["wall_s"] = round(end - r["t"], 1)
        r.pop("pp_last", None)
        res.append(r)
    return sorted(res, key=lambda r: r["t"])


# ── GPU samples ───────────────────────────────────────────────────────────────

def gpu_samples(t0, t1):
    rows = defaultdict(dict)
    days = {time.strftime("%Y%m%d", time.localtime(x)) for x in (t0, t1)}
    for day in sorted(days):
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
    return [{"t": t, "g": g} for t, g in sorted(rows.items())]


# ── project info ──────────────────────────────────────────────────────────────

def project_info(project, t0, t1):
    pdir = ROOT / "projects" / project
    if not (pdir / ".git").exists():
        return None, []
    git = lambda *a: subprocess.run(["git", "-C", str(pdir), *a], capture_output=True, text=True).stdout
    commits = git("log", f"--since=@{int(t0)}", f"--until=@{int(t1) + 120}", "--format=%h %s").splitlines()
    task = None
    diff = git("log", "-p", f"--since=@{int(t0)}", f"--until=@{int(t1) + 120}", "--format=", "--", "*.md")
    m = re.search(r"^\+\s*- \[x\] (.+)$", diff, re.M)
    if m:
        task = m.group(1)
    elif (pdir / "TODO.md").exists():
        m = re.search(r"^\s*- \[ \] (.+)$", (pdir / "TODO.md").read_text(), re.M)
        task = m.group(1) if m else None
    return task, commits


# ── report ────────────────────────────────────────────────────────────────────

def build(log, sid, trigger):
    log = Path(log)
    project = re.sub(r"-\d{8}-\d{6}\.jsonl$", "", log.name)
    a = analyse(read_session(log, sid))
    if not a["calls"]:
        sys.exit(f"no API calls for session {sid} in {log}")
    res = a["result"] or {}
    t0 = a["t0"]
    t1 = max(a["t1"], t0 + (res.get("duration_ms") or 0) / 1000) if res else time.time()
    reqs = ollama_requests(t0, t1)
    gpu = gpu_samples(t0, t1)
    task, commits = project_info(project, t0, t1)
    window = num_ctx()
    peak = max(c["total"] for c in a["calls"])
    last = a["calls"][-1]["comp"]
    # Share of context growth by category, over the whole run (compactions included).
    added, prev = defaultdict(float), None
    for c in a["calls"]:
        if prev is not None:
            for k in c["comp"]:
                d = c["comp"][k] - prev[k]
                if d > 0 and not (k == "other" and c["delta"] < 0):
                    added[k] += d
        prev = c["comp"]
    growth = dict(added)

    wall = t1 - t0
    pre_s = sum(r["prefill_s"] for r in reqs)
    gen_s = sum(r["gen_s"] for r in reqs)
    prompt_tok = sum(r["prompt"] for r in reqs)
    reused = sum(r["reused"] for r in reqs)
    gen_tps = sorted(r["tps"] for r in reqs if r["tps"])
    pp_tps = sorted(r["processed"] / r["prefill_s"] for r in reqs if r["prefill_s"] and r["processed"] > 1000)
    med = lambda xs: xs[len(xs) // 2] if xs else None
    gpu_ids = sorted({i for s in gpu for i in s["g"]})
    util = [v[0] for s in gpu for v in s["g"].values()]
    vram_total = [sum(v[1] for v in s["g"].values()) for s in gpu]
    vram_cap = sum(v[2] for v in gpu[0]["g"].values()) if gpu else 0
    power = [sum(v[3] for v in s["g"].values()) for s in gpu]
    energy_wh = sum(power) * 5 / 3600 if power else 0

    start = datetime.fromtimestamp(t0)
    data = {
        "title": task or f"session {sid[:8]}",
        "project": project, "sid": sid, "trigger": trigger, "log": log.name,
        "start": start.strftime("%Y-%m-%d %H:%M"), "window": window,
        "cats": [{"key": k, "name": n} for k, n in CATS],
        "calls": [{"turn": c["turn"], "min": round((c["t"] - t0) / 60, 2) if c["t"] else None, "total": c["total"],
                   "delta": c["delta"], "comp": c["comp"], "added": c["added"]} for c in a["calls"]],
        "final": last, "growth": growth, "top": a["top"], "tools": a["tools"],
        "compactions": a["compactions"], "commits": commits,
        "reqs": [{"min": round((r["t"] - t0) / 60, 2), **{k: r[k] for k in
                  ("prompt", "reused", "processed", "prefill_s", "gen", "gen_s", "tps", "full", "cancelled", "wall_s")}}
                 for r in reqs],
        "gpu": {"ids": gpu_ids, "t": [round((s["t"] - t0) / 60, 2) for s in gpu],
                "util": [[s["g"].get(i, [None])[0] for s in gpu] for i in gpu_ids],
                "vram": [[round(s["g"][i][1], 2) if i in s["g"] else None for s in gpu] for i in gpu_ids],
                "power": [[s["g"][i][3] if i in s["g"] else None for s in gpu] for i in gpu_ids],
                "temp": [[s["g"][i][4] if i in s["g"] else None for s in gpu] for i in gpu_ids],
                "cap_gb": round(vram_cap, 1)},
        "stats": {
            "wall_min": round(wall / 60, 1), "turns": len(a["calls"]),
            "outcome": ("error" if res.get("is_error") else res.get("subtype")) if res else "running / killed",
            "peak": peak, "peak_pct": round(100 * peak / window, 1), "end_ctx": a["calls"][-1]["total"],
            "think_pct": round(100 * growth.get("think", 0) / max(1, sum(growth.values())), 1),
            "retries": a["retries"], "compactions": len(a["compactions"]),
            "prefill_s": round(pre_s), "gen_s": round(gen_s), "other_s": round(max(0, wall - pre_s - gen_s)),
            "gen_tok": sum(r["gen"] for r in reqs), "gen_tps": med(gen_tps), "pp_tps": med(pp_tps),
            "reuse_pct": round(100 * reused / prompt_tok, 1) if prompt_tok else None,
            "full_reprocess": sum(r["full"] for r in reqs), "requests": len(reqs),
            "cancelled": sum(r["cancelled"] for r in reqs),
            "util_avg": round(sum(util) / len(util)) if util else None,
            "vram_peak": round(max(vram_total), 1) if vram_total else None, "vram_cap": round(vram_cap),
            "power_avg": round(sum(power) / len(power)) if power else None, "energy_wh": round(energy_wh),
        },
    }
    out_dir = REPORTS / project
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{start.strftime('%Y%m%d-%H%M')}-{sid[:8]}-{trigger}"
    page = TEMPLATE.replace("__TITLE__", html.escape(data["title"][:80])).replace(
        "__DATA__", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))
    (out_dir / f"{name}.html").write_text(page)
    summary = {k: data[k] for k in ("title", "project", "start", "trigger")} | {
        k: data["stats"][k] for k in ("wall_min", "turns", "peak_pct", "think_pct", "gen_tps", "util_avg", "outcome", "compactions", "retries")}
    (out_dir / f"{name}.json").write_text(json.dumps(summary))
    build_index()
    return out_dir / f"{name}.html"


def build_index():
    rows = []
    for f in sorted(REPORTS.glob("*/*.json"), reverse=True):
        s = json.loads(f.read_text())
        rows.append(
            f'<tr><td class="num">{s["start"]}</td><td>{html.escape(s["project"])}</td>'
            f'<td><a href="{f.parent.name}/{f.stem}.html">{html.escape(short(s["title"], 90))}</a></td>'
            f'<td>{s["trigger"]}</td><td class="num">{s["wall_min"]}</td><td class="num">{s["turns"]}</td>'
            f'<td class="num">{s["peak_pct"]}%</td><td class="num">{s["think_pct"]}%</td>'
            f'<td class="num">{s["gen_tps"] and round(s["gen_tps"], 1)}</td><td class="num">{s["util_avg"]}</td>'
            f'<td>{html.escape(str(s["outcome"]))}{" · " + str(s["compactions"]) + " compact" if s["compactions"] else ""}'
            f'{" · " + str(s["retries"]) + " retries" if s["retries"] else ""}</td></tr>')
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "index.html").write_text(INDEX.replace("__ROWS__", "\n".join(rows)))


def find_log(sid):
    for f in sorted(LOGS.glob("*.jsonl"), key=lambda p: -p.stat().st_mtime):
        if subprocess.run(["grep", "-qF", sid, str(f)]).returncode == 0:
            return f
    sys.exit(f"session {sid} not found in {LOGS}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", nargs="?")
    ap.add_argument("--session")
    ap.add_argument("--trigger", default="manual")
    ap.add_argument("--all", action="store_true", help="one report per session in LOG")
    ap.add_argument("--index", action="store_true")
    args = ap.parse_args()
    if args.index:
        return build_index()
    log = args.log or (find_log(args.session) if args.session else None)
    if not log:
        ap.error("give LOG or --session")
    sids = sessions_in(log) if args.all else [args.session or sessions_in(log)[-1]]
    for sid in sids:
        print(build(log, sid, args.trigger))


CSS = """
:root{color-scheme:light;--bg:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--ring:rgba(11,11,11,.10);--crit:#d03b3b;--warn:#b27a00;
--c1:#2a78d6;--c2:#eb6834;--c3:#1baf7a;--c4:#eda100;--c5:#e87ba4;--c6:#008300;--c7:#4a3aa7;--c8:#e34948}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#0d0d0d;--surface:#1a1a19;
--ink:#fff;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);--warn:#fab219;
--c1:#3987e5;--c2:#d95926;--c3:#199e70;--c4:#c98500;--c5:#d55181;--c6:#008300;--c7:#9085e9;--c8:#e66767}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--grid:#2c2c2a;
--axis:#383835;--ring:rgba(255,255,255,.10);--warn:#fab219;--c1:#3987e5;--c2:#d95926;--c3:#199e70;--c4:#c98500;
--c5:#d55181;--c6:#008300;--c7:#9085e9;--c8:#e66767}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:19px;margin:0 0 4px;font-weight:600}h2{font-size:14px;margin:0 0 2px;font-weight:600}
.sub{color:var(--ink2);font-size:12.5px}.note{color:var(--muted);font-size:12px;margin:2px 0 10px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(128px,1fr));gap:10px;margin:18px 0}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:10px 12px}
.tile .k{color:var(--ink2);font-size:12px}.tile .v{font-size:22px;font-weight:600;margin-top:2px}
.tile .s{color:var(--muted);font-size:11.5px}.bad{color:var(--crit)}.warn{color:var(--warn)}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:14px 16px;margin:12px 0}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px}.grid2 .card{margin:0}
.grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}.grid3 .card{margin:0}
svg{display:block;width:100%;overflow:visible}svg text{fill:var(--muted);font-size:11px}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;margin:6px 0 2px;font-size:12px;color:var(--ink2)}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.bar100{display:flex;gap:2px;height:22px;margin:8px 0}.bar100 div{border-radius:4px;min-width:2px}
table{border-collapse:collapse;width:100%;font-size:12.5px}td,th{padding:4px 8px;text-align:left;border-bottom:1px solid var(--grid)}
th{color:var(--ink2);font-weight:500}.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.hb{display:grid;grid-template-columns:minmax(0,1fr) 150px 52px;gap:8px;align-items:center;font-size:12.5px;padding:3px 0}
.hb .l{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink)}.hb .b{height:12px;border-radius:0 4px 4px 0}
details{margin-top:8px}summary{cursor:pointer;color:var(--ink2);font-size:12.5px}
.scroll{max-height:360px;overflow:auto}.mono{font-family:ui-monospace,Menlo,monospace;font-size:12px}
#tip{position:fixed;pointer-events:none;background:var(--surface);color:var(--ink);border:1px solid var(--ring);
border-radius:8px;padding:7px 9px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.15);display:none;z-index:9;max-width:340px}
#tip .r{display:flex;justify-content:space-between;gap:14px}#tip b{font-weight:600}
a{color:var(--c1)}ul.commits{margin:4px 0 0;padding-left:18px}
"""

TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<style>""" + CSS + """</style></head><body><main id="app"></main><div id="tip"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent),S=D.stats,app=document.getElementById('app'),tip=document.getElementById('tip');
const col=i=>`var(--c${i+1})`,catIdx=Object.fromEntries(D.cats.map((c,i)=>[c.key,i])),catCol=k=>col(catIdx[k]);
const k=v=>v==null?'–':Math.abs(v)>=1000?(v/1000).toFixed(Math.abs(v)>=1e5?0:1)+'k':String(Math.round(v));
const f1=v=>v==null?'–':(+v).toFixed(1),esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const mins=m=>m==null?'–':m<60?m.toFixed(1)+' min':Math.floor(m/60)+'h '+Math.round(m%60)+'m';
function showTip(e,h){tip.innerHTML=h;tip.style.display='block';const w=tip.offsetWidth,x=e.clientX+14;
 tip.style.left=(x+w>innerWidth?e.clientX-w-14:x)+'px';tip.style.top=Math.min(e.clientY+12,innerHeight-tip.offsetHeight-8)+'px'}
function hideTip(){tip.style.display='none'}
const NS='http://www.w3.org/2000/svg';
function el(t,a,p){const n=document.createElementNS(NS,t);for(const x in a)n.setAttribute(x,a[x]);if(p)p.appendChild(n);return n}
function ticks(max,n=4){if(max<=0)return[0];const raw=max/n,m=Math.pow(10,Math.floor(Math.log10(raw))),s=[1,2,2.5,5,10].map(x=>x*m).find(x=>x>=raw);
 const r=[];for(let v=0;v<=max*1.0001;v+=s)r.push(+v.toFixed(6));return r}
function legend(items){return '<div class="legend">'+items.map(i=>`<span><span class="sw" style="background:${i.c}"></span>${esc(i.n)}</span>`).join('')+'</div>'}
function card(h,note,body,cls='card'){return `<section class="${cls}"><h2>${h}</h2>${note?`<div class="note">${note}</div>`:''}${body}</section>`}

// Generic chart: x values, series [{n,c,v:[]}], stacked or lines, with crosshair tooltip.
function chart(host,{x,series,stacked=false,H=200,yfmt=k,xfmt=v=>v,xlabel='',limit=null,marks=[],bars=false,tipx=null,ymax=null}){
 const draw=()=>{host.innerHTML='';const W=host.clientWidth||600,L=44,R=8,T=10,B=26,pw=W-L-R,ph=H-T-B;
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,height:H},host),n=x.length;if(!n){host.innerHTML='<div class="note">no data</div>';return}
  const cum=series.map(()=>new Array(n).fill(0));series.forEach((s,j)=>s.v.forEach((v,i)=>{cum[j][i]=(stacked&&j?cum[j-1][i]:0)+(v||0)}));
  let top=ymax??Math.max(1,...(stacked?cum[cum.length-1]:series.flatMap(s=>s.v.filter(v=>v!=null))));if(limit)top=Math.max(top,limit);top*=1.04;
  const x0=Math.min(...x),x1=Math.max(...x),bw=bars?Math.max(2,pw/n-2):0;
  const X=i=>bars?L+(i+.5)*pw/n:L+(x1>x0?(x[i]-x0)/(x1-x0):.5)*pw,Y=v=>T+ph-(v/top)*ph;
  for(const t of ticks(top/1.04)){el('line',{x1:L,x2:W-R,y1:Y(t),y2:Y(t),stroke:'var(--grid)'},svg);el('text',{x:L-6,y:Y(t)+4,'text-anchor':'end'},svg).textContent=yfmt(t)}
  el('line',{x1:L,x2:W-R,y1:T+ph,y2:T+ph,stroke:'var(--axis)'},svg);
  const xt=bars?ticks(n-1,6).filter(v=>Number.isInteger(v)&&v<n):ticks(x1-x0,6).map(v=>v+x0).filter(v=>v<=x1);
  for(const t of xt){const px=bars?X(t):L+(x1>x0?(t-x0)/(x1-x0):.5)*pw;if(xlabel&&px>W-R-40)continue;el('text',{x:px,y:H-8,'text-anchor':'middle'},svg).textContent=xfmt(bars?x[t]:t)}
  if(xlabel)el('text',{x:W-R,y:H-8,'text-anchor':'end'},svg).textContent=xlabel;
  if(bars){series.forEach((s,j)=>s.v.forEach((v,i)=>{if(!v)return;const y0=stacked&&j?cum[j-1][i]:0,h=Math.max(0,Y(y0)-Y(y0+v)-(j?2:0));
    el('rect',{x:X(i)-bw/2,y:Y(y0+v),width:bw,height:h,rx:Math.min(2,bw/2),fill:s.c},svg)}))}
  else if(stacked){for(let j=series.length-1;j>=0;j--){let d='';for(let i=0;i<n;i++)d+=(i?'L':'M')+X(i)+','+Y(cum[j][i]);for(let i=n-1;i>=0;i--)d+='L'+X(i)+','+Y(j?cum[j-1][i]:0);
    el('path',{d:d+'Z',fill:series[j].c,stroke:'var(--surface)','stroke-width':1,'stroke-linejoin':'round'},svg)}}
  else series.forEach(s=>{let d='',pen=false;s.v.forEach((v,i)=>{if(v==null){pen=false;return}d+=(pen?'L':'M')+X(i)+','+Y(v);pen=true});
    el('path',{d,fill:'none',stroke:s.c,'stroke-width':2,'stroke-linejoin':'round'},svg)});
  if(limit){el('line',{x1:L,x2:W-R,y1:Y(limit),y2:Y(limit),stroke:'var(--crit)','stroke-dasharray':'4 3'},svg);
    el('text',{x:W-R,y:Y(limit)-5,'text-anchor':'end',style:'fill:var(--crit)'},svg).textContent='window '+k(limit)}
  for(const m of marks){const px=X(m.i);el('line',{x1:px,x2:px,y1:T,y2:T+ph,stroke:'var(--ink2)'},svg);el('text',{x:px+4,y:T+10,style:'fill:var(--ink2)'},svg).textContent=m.label}
  const cross=el('line',{y1:T,y2:T+ph,stroke:'var(--ink2)','stroke-width':1,visibility:'hidden'},svg);
  const hit=el('rect',{x:L,y:T,width:pw,height:ph,fill:'transparent'},svg);
  hit.addEventListener('mousemove',e=>{const r=svg.getBoundingClientRect(),mx=(e.clientX-r.left)*W/r.width;let bi=0,bd=1e9;
   for(let i=0;i<n;i++){const d=Math.abs(X(i)-mx);if(d<bd){bd=d;bi=i}}cross.setAttribute('x1',X(bi));cross.setAttribute('x2',X(bi));cross.setAttribute('visibility','visible');
   const rows=[...series].reverse().filter(s=>s.v[bi]!=null).map(s=>`<div class="r"><span><span class="sw" style="background:${s.c}"></span>${esc(s.n)}</span><b>${yfmt(s.v[bi])}</b></div>`).join('');
   showTip(e,(tipx?tipx(bi):`<b>${xfmt(x[bi])}</b>`)+rows+(stacked?`<div class="r"><span>total</span><b>${yfmt(cum[cum.length-1][bi])}</b></div>`:''))});
  hit.addEventListener('mouseleave',()=>{cross.setAttribute('visibility','hidden');hideTip()})};
 draw();new ResizeObserver(()=>draw()).observe(host)}

function tile(kk,v,s='',cls=''){return `<div class="tile"><div class="k">${kk}</div><div class="v ${cls}">${v}</div><div class="s">${s}</div></div>`}
const tot=Object.values(D.final).reduce((a,b)=>a+b,0)||1,gsum=Object.values(D.growth).reduce((a,b)=>a+b,0)||1;
const pk=S.peak_pct,pkc=pk>=85?'bad':pk>=65?'warn':'';
let h=`<h1>${esc(D.title)}</h1><div class="sub">${esc(D.project)} · ${D.start} · ${mins(S.wall_min)} · session <span class="mono">${D.sid.slice(0,8)}</span> · report on <b>${D.trigger}</b> · ${esc(D.log)}</div>`;
h+='<div class="tiles">'+[
 tile('Peak context',k(S.peak),`${pk}% of ${k(D.window)} window`,pkc),
 tile('Thinking share',S.think_pct+'%','of all context growth'),
 tile('Turns',S.turns,`${S.compactions} compaction${S.compactions==1?'':'s'} · ${S.retries} retr${S.retries==1?'y':'ies'}`,S.retries?'warn':''),
 tile('Outcome',esc(S.outcome),S.cancelled?`${S.cancelled} request${S.cancelled>1?'s':''} cancelled`:'',S.outcome==='success'?'':'bad'),
 tile('Generation',S.gen_tps?f1(S.gen_tps)+' t/s':'–',`${k(S.gen_tok)} tokens out · median`),
 tile('Prefill',S.pp_tps?k(S.pp_tps)+' t/s':'–',`cache reuse ${S.reuse_pct??'–'}% · ${S.full_reprocess} full reprocess`,S.full_reprocess>S.requests/3?'warn':''),
 tile('GPU util',S.util_avg!=null?S.util_avg+'%':'–','average, all GPUs'),
 tile('VRAM peak',S.vram_peak!=null?f1(S.vram_peak)+' GB':'–',`of ${S.vram_cap||'–'} GB · ${S.power_avg??'–'} W avg · ${S.energy_wh} Wh`),
].join('')+'</div>';

// time split
const tw=[['Prefill (reading prompt)',S.prefill_s,col(0)],['Generation',S.gen_s,col(1)],['Tools + idle',S.other_s,col(2)]],tws=tw.reduce((a,b)=>a+b[1],0)||1;
h+=card('Where the wall-clock time went',`${mins(S.wall_min)} total over ${S.requests} model requests`,
 '<div class="bar100">'+tw.map(t=>`<div style="flex:${t[1]};background:${t[2]}" data-t="${esc(t[0])}: ${mins(t[1]/60)} (${Math.round(100*t[1]/tws)}%)"></div>`).join('')+'</div>'+
 legend(tw.map(t=>({n:`${t[0]} ${Math.round(100*t[1]/tws)}% · ${mins(t[1]/60)}`,c:t[2]}))));

h+=card('Context over the run','Measured prompt size per API call; split by what was added since the previous call (char-proportional). Hover for the breakdown.',
 '<div id="ctx"></div>'+legend(D.cats.map((c,i)=>({n:c.name,c:col(i)}))));

h+='<div class="grid2">'+card('Context at the end',`${k(tot)} tokens — what the model is carrying now`,
 '<div class="bar100">'+D.cats.filter(c=>D.final[c.key]>0).map(c=>`<div style="flex:${D.final[c.key]};background:${catCol(c.key)}" data-t="${esc(c.name)}: ${k(D.final[c.key])} (${(100*D.final[c.key]/tot).toFixed(1)}%)"></div>`).join('')+'</div>'+
 '<table><tr><th>Category</th><th class="num">Now</th><th class="num">%</th><th class="num">Added over run</th><th class="num">%</th></tr>'+
 D.cats.map(c=>`<tr><td><span class="sw" style="background:${catCol(c.key)}"></span>${esc(c.name)}</td><td class="num">${k(D.final[c.key])}</td><td class="num">${(100*D.final[c.key]/tot).toFixed(1)}</td><td class="num">${k(D.growth[c.key]||0)}</td><td class="num">${(100*(D.growth[c.key]||0)/gsum).toFixed(1)}</td></tr>`).join('')+'</table>')
 +card('Biggest single contributors','Merged across repeats; turns where each landed',
 (()=>{const mx=Math.max(1,...D.top.map(t=>t.tokens));return D.top.map(t=>`<div class="hb" data-t="${esc(t.label)} — ${k(t.tokens)} tokens · turn${t.turns.length>1?'s':''} ${t.turns.slice(0,12).join(', ')}${t.turns.length>12?'…':''}">
 <span class="l">${esc(t.label)}</span><span><div class="b" style="width:${100*t.tokens/mx}%;background:${catCol(t.cat)}"></div></span><span class="num">${k(t.tokens)}</span></div>`).join('')})())+'</div>';

h+='<div class="grid2">'+card('Model requests: time per request','Seconds spent reading the prompt vs generating; cancelled requests are the tall bars that never finish',
 '<div id="rtime"></div>'+legend([{n:'Prefill',c:col(0)},{n:'Generation',c:col(1)}]))
 +card('Model requests: prompt cache','Prompt tokens reused from Ollama\\'s cache vs re-processed from scratch',
 '<div id="rcache"></div>'+legend([{n:'Reused',c:col(2)},{n:'Re-processed',c:col(7)}]))+'</div>';

const G=D.gpu,gid=G.ids.map((g,i)=>({n:'GPU '+g,c:col(i)}));
h+='<div class="grid3">'+card('GPU utilization','% per GPU, 5 s samples','<div id="gu"></div>'+legend(gid))
 +card('VRAM','GB per GPU, of '+(G.ids.length?f1(G.cap_gb/G.ids.length):'–')+' GB each','<div id="gv"></div>'+legend(gid))
 +card('Power','W per GPU','<div id="gp"></div>'+legend(gid))+'</div>';

h+='<div class="grid2">'+card('Tools','Result tokens are what each tool added to the context',
 '<table><tr><th>Tool</th><th class="num">Calls</th><th class="num">Tokens</th><th class="num">Per call</th></tr>'+
 D.tools.map(t=>`<tr><td>${esc(t.name)}</td><td class="num">${t.calls}</td><td class="num">${k(t.tokens)}</td><td class="num">${k(t.calls?t.tokens/t.calls:0)}</td></tr>`).join('')+'</table>')
 +card('Commits during the run',D.commits.length?'':'none','<ul class="commits mono">'+D.commits.map(c=>`<li>${esc(c)}</li>`).join('')+'</ul>')+'</div>';

h+=card('Per-turn data','','<details><summary>Turn-by-turn table ('+D.calls.length+' rows)</summary><div class="scroll"><table><tr><th class="num">Turn</th><th class="num">Min</th><th class="num">Context</th><th class="num">Δ</th><th>Largest addition</th></tr>'+
 D.calls.map(c=>`<tr><td class="num">${c.turn}</td><td class="num">${f1(c.min)}</td><td class="num">${k(c.total)}</td><td class="num">${c.delta>=0?'+':''}${k(c.delta)}</td><td>${esc(c.added)}</td></tr>`).join('')+'</table></div></details>'+
 '<details><summary>Model requests ('+D.reqs.length+' rows)</summary><div class="scroll"><table><tr><th class="num">Min</th><th class="num">Prompt</th><th class="num">Reused</th><th class="num">Prefill s</th><th class="num">Gen tok</th><th class="num">Gen s</th><th class="num">t/s</th><th>Flags</th></tr>'+
 D.reqs.map(r=>`<tr><td class="num">${f1(r.min)}</td><td class="num">${k(r.prompt)}</td><td class="num">${k(r.reused)}</td><td class="num">${f1(r.prefill_s)}</td><td class="num">${r.gen}</td><td class="num">${f1(r.gen_s)}</td><td class="num">${f1(r.tps)}</td><td>${[r.full?'full reprocess':'',r.cancelled?'cancelled':''].filter(Boolean).join(', ')}</td></tr>`).join('')+'</table></div></details>');
app.innerHTML=h;

document.querySelectorAll('[data-t]').forEach(n=>{n.addEventListener('mousemove',e=>showTip(e,esc(n.dataset.t)));n.addEventListener('mouseleave',hideTip)});
const C=D.calls;
chart(document.getElementById('ctx'),{x:C.map(c=>c.turn),stacked:true,H:260,limit:D.window,xlabel:'turn',
 series:D.cats.map((c,i)=>({n:c.name,c:col(i),v:C.map(t=>t.comp[c.key])})),
 marks:D.compactions.map(m=>({i:m.turn,label:'compacted'})),
 tipx:i=>`<b>Turn ${C[i].turn}</b> · ${mins(C[i].min)} · ${Math.round(100*C[i].total/D.window)}% full<div class="note">${esc(C[i].added||'')}</div>`});
const R=D.reqs,rt=i=>`<b>Request ${i+1}</b> · ${mins(R[i].min)}${R[i].cancelled?' · <span class="bad">cancelled</span>':''}${R[i].full?' · full reprocess':''}<div class="note">${k(R[i].prompt)} prompt · ${R[i].gen} tokens out · ${f1(R[i].tps)} t/s</div>`;
chart(document.getElementById('rtime'),{x:R.map((r,i)=>i+1),bars:true,stacked:true,yfmt:v=>Math.round(v)+'s',xlabel:'request',tipx:rt,
 series:[{n:'Prefill',c:col(0),v:R.map(r=>r.prefill_s)},{n:'Generation',c:col(1),v:R.map(r=>r.gen_s)}]});
chart(document.getElementById('rcache'),{x:R.map((r,i)=>i+1),bars:true,stacked:true,xlabel:'request',tipx:rt,
 series:[{n:'Reused',c:col(2),v:R.map(r=>r.reused)},{n:'Re-processed',c:col(7),v:R.map(r=>r.processed)}]});
const gx={x:G.t,xfmt:v=>Math.round(v)+'m',H:170,tipx:i=>`<b>${mins(G.t[i])}</b>`};
chart(document.getElementById('gu'),{...gx,ymax:100,yfmt:v=>Math.round(v)+'%',series:G.ids.map((g,i)=>({n:'GPU '+g,c:col(i),v:G.util[i]}))});
chart(document.getElementById('gv'),{...gx,ymax:G.ids.length?G.cap_gb/G.ids.length:null,yfmt:v=>f1(v),series:G.ids.map((g,i)=>({n:'GPU '+g,c:col(i),v:G.vram[i]}))});
chart(document.getElementById('gp'),{...gx,yfmt:v=>Math.round(v)+'W',series:G.ids.map((g,i)=>({n:'GPU '+g,c:col(i),v:G.power[i]}))});
</script></body></html>"""

INDEX = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Agent run reports</title>
<style>""" + CSS + """</style></head><body><main><h1>Agent run reports</h1>
<div class="sub">One row per report — written at the end of each run and at every compaction. Newest first.</div>
<section class="card" style="overflow-x:auto"><table><tr><th>Start</th><th>Project</th><th>Task</th><th>Trigger</th>
<th class="num">Min</th><th class="num">Turns</th><th class="num">Peak ctx</th><th class="num">Thinking</th>
<th class="num">Gen t/s</th><th class="num">GPU %</th><th>Outcome</th></tr>
__ROWS__</table></section></main></body></html>"""

if __name__ == "__main__":
    main()
