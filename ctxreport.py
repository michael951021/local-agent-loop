#!/usr/bin/env python3
"""Context + GPU report for one agent run, as a self-contained HTML page.

  ctxreport.py LOG.jsonl [--run RID | --session SID | --all] [--trigger end|compact|manual]
  ctxreport.py --session SID [--trigger ...]        find the log that holds SID
  ctxreport.py --index                              only rebuild reports/index.html and fleet.html

Writes reports/<project>/<start>-[<agent>-]<sid8>-<trigger>.html, then refreshes reports/index.html
and reports/fleet.html. ./agent runs it at the end of every run (--run); pretty.py on each compaction.
Data comes from telemetry.py, the page look from reportui.py.

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

import reportui
from telemetry import (LOGS, ROOT, agent_tag, attribute, clean_task, commits, config, fair_share, gpu_samples,
                       guess_task, harness_runs, ledger, log_runs, ollama_requests, read_session)

REPORTS = ROOT / "reports"

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


def short(s, n=70):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def text_of(content):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content or [])


# ── context attribution ──────────────────────────────────────────────────────

def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


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


# ── report ────────────────────────────────────────────────────────────────────

def report_links():
    """{run id: 'project/file.html'} for every end report, so timelines can link to other runs."""
    links = {}
    for f in REPORTS.glob("*/*.json"):
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if s.get("run") and (s.get("trigger") == "end" or s["run"] not in links):
            links[s["run"]] = f"{f.parent.name}/{f.stem}.html"
    return links


def lane_order(tag, mine):
    return (tag != mine, tag == "other clients", tag)


def timeline(all_reqs, runs, t0, t1, mine, prefix="../"):
    """Per-agent lanes of runs and requests, in minutes from t0."""
    links = report_links()
    by = defaultdict(lambda: {"runs": [], "reqs": []})
    span = (t1 - t0) / 60
    for r in all_reqs:
        a, b = (max(r["t"], t0) - t0) / 60, (min(r["end"], t1) - t0) / 60
        if b <= 0 or a >= span:
            continue
        by[r["tag"]]["reqs"].append({
            "a": round(a, 3), "b": round(max(b, a + 0.01), 3), "p": round(r["prefill_s"] / 60, 3),
            "t": f"<b>{html.escape(r['tag'])}</b> · slot {r['slot']}<div class='note'>{r['prompt'] / 1000:.1f}k prompt · "
                 f"{r['prefill_s']:.0f}s reading · {r['gen']} tokens in {r['gen_s']:.0f}s"
                 f"{' · cancelled' if r['cancelled'] else ''}</div>"})
    for run in runs:
        st, en = run["start"], run["end"]
        b = en["t"] if en else t1
        if b < t0 or st["t"] > t1:
            continue
        href = links.get(st["run"])
        by[agent_tag(st)]["runs"].append({
            "a": round((max(st["t"], t0) - t0) / 60, 3), "b": round((min(b, t1) - t0) / 60, 3),
            "href": prefix + href if href else None,
            "t": f"<b>{html.escape(agent_tag(st))}</b> · {html.escape(clean_task(st.get('task'))[:140] or 'run')}"
                 f"<div class='note'>{(b - st['t']) / 60:.0f} min{' · ' + en['merged'] if en and en.get('merged') not in (None, 'n/a') else ''}"
                 f"{' · click for its report' if href else ''}</div>"})
    return [{"tag": t, **v} for t, v in sorted(by.items(), key=lambda kv: lane_order(kv[0], mine))]


def build(log, sid, trigger, run=None):
    log = Path(log)
    start, end = (run or {}).get("start"), (run or {}).get("end")
    project = start["project"] if start else re.sub(r"(-w\d+)?-\d{8}-\d{6}\.jsonl$", "", log.name)
    worker = (start or {}).get("worker") or "main"
    tag = agent_tag(start) or project
    a = analyse(read_session(log, sid))
    if not a["calls"]:
        sys.exit(f"no API calls for session {sid} in {log}")
    res = a["result"] or {}
    t0 = a["t0"]
    t1 = end["t"] if end else (max(a["t1"], t0 + (res.get("duration_ms") or 0) / 1000) if res else time.time())

    # Model requests: with the keepalive ledger each one is attributed to its agent; without it
    # (runs from before the ledger existed) every request in the time window counts as this run's.
    all_reqs = attribute(ollama_requests(t0, t1), ledger(t0, t1))
    for r in all_reqs:
        if r["tag"] == "untagged":   # proxy started by an older ./agent: single agent, so it is us
            r["tag"] = tag
    tags = {r["tag"] for r in all_reqs}
    reqs = [r for r in all_reqs if r["tag"] == tag] if tags - {"other clients"} else all_reqs
    share = None
    if len(tags) > 1:
        sec, busy, overlap = fair_share(all_reqs, t0, t1)
        runs = [r for r in harness_runs(t0 - 86400) if (r["end"] or {}).get("t", t1) >= t0 and r["start"]["t"] <= t1]
        share = {"mine_pct": round(100 * sec.get(tag, 0) / busy, 1) if busy else 0,
                 "agents": len(tags - {"other clients"}) or 1,
                 "overlap_pct": round(100 * overlap / busy, 1) if busy else 0,
                 "rows": [{"tag": t, "n": sum(r["tag"] == t for r in all_reqs), "s": round(v),
                           "pct": round(100 * v / busy, 1) if busy else 0}
                          for t, v in sorted(sec.items(), key=lambda kv: lane_order(kv[0], tag))],
                 "lanes": timeline(all_reqs, runs, t0, t1, tag)}

    gpu = gpu_samples(t0, t1)
    if start and start.get("task"):
        task = ("Merge fix: " if start.get("merge_fix") else "") + clean_task(start["task"])
        cs = commits(project, start.get("base"), (end or {}).get("head"))
    else:
        task, cs = guess_task(project, t0, t1)
    # The window each request really had (Ollama logs it), else what the harness was configured with.
    window = max((r["n_ctx"] for r in reqs), default=None) or (start or {}).get("num_ctx") or config("NUM_CTX", 131072)
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

    begun = datetime.fromtimestamp(t0)
    data = {
        "title": task or f"session {sid[:8]}",
        "project": project, "worker": worker, "tag": tag, "sid": sid, "trigger": trigger, "log": log.name,
        "branch": (start or {}).get("branch"), "merged": (end or {}).get("merged"),
        "start": begun.strftime("%Y-%m-%d %H:%M"), "window": window,
        "cats": [{"key": k, "name": n} for k, n in CATS],
        "calls": [{"turn": c["turn"], "min": round((c["t"] - t0) / 60, 2) if c["t"] else None, "total": c["total"],
                   "delta": c["delta"], "comp": c["comp"], "added": c["added"]} for c in a["calls"]],
        "final": last, "growth": growth, "top": a["top"], "tools": a["tools"],
        "compactions": a["compactions"], "commits": cs, "share": share,
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
            "real_peak": max((r["prompt"] for r in reqs), default=None),
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
    name = f"{begun.strftime('%Y%m%d-%H%M')}-{'' if worker == 'main' else worker + '-'}{sid[:8]}-{trigger}"
    (out_dir / f"{name}.html").write_text(reportui.page(data["title"][:80], data, TEMPLATE_JS))
    summary = {k: data[k] for k in ("title", "project", "worker", "tag", "start", "trigger", "merged")} | {
        "run": (start or {}).get("run"), "share_pct": share and share["mine_pct"]} | {
        k: data["stats"][k] for k in ("wall_min", "turns", "peak_pct", "think_pct", "gen_tps", "util_avg", "outcome",
                                      "compactions", "retries")}
    (out_dir / f"{name}.json").write_text(json.dumps(summary))
    build_index()
    return out_dir / f"{name}.html"


def build_index():
    rows = []
    for f in sorted(REPORTS.glob("*/*.json"), key=lambda p: p.name[:13], reverse=True):
        s = json.loads(f.read_text())
        who = s.get("tag") or s["project"]
        merged = {"conflict": " · merge conflict", "parked": " · parked"}.get(s.get("merged"), "")
        rows.append(
            f'<tr><td class="num">{s["start"]}</td><td>{html.escape(who)}</td>'
            f'<td><a href="{f.parent.name}/{f.stem}.html">{html.escape(short(s["title"], 90))}</a></td>'
            f'<td>{s["trigger"]}</td><td class="num">{s["wall_min"]}</td><td class="num">{s["turns"]}</td>'
            f'<td class="num">{s["peak_pct"]}%</td><td class="num">{s["think_pct"]}%</td>'
            f'<td class="num">{s["gen_tps"] and round(s["gen_tps"], 1)}</td>'
            f'<td class="num">{s["share_pct"] if s.get("share_pct") is not None else "–"}</td>'
            f'<td>{html.escape(str(s["outcome"]))}{merged}{" · " + str(s["compactions"]) + " compact" if s["compactions"] else ""}'
            f'{" · " + str(s["retries"]) + " retries" if s["retries"] else ""}</td></tr>')
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "index.html").write_text(INDEX.replace("__ROWS__", "\n".join(rows)))
    try:
        import fleet
        fleet.build()
    except Exception as e:   # the fleet page must never cost us the run report
        print(f"fleet page: {e!r}", file=sys.stderr)


def find_log(sid):
    for f in sorted(LOGS.glob("*.jsonl"), key=lambda p: -p.stat().st_mtime):
        if subprocess.run(["grep", "-qF", sid, str(f)]).returncode == 0:
            return f
    sys.exit(f"session {sid} not found in {LOGS}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", nargs="?")
    ap.add_argument("--run", help="harness run id (from ./agent)")
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
    runs = [r for r in log_runs(log) if r["sessions"]]
    if args.run:
        runs = [r for r in runs if r["start"] and r["start"]["run"] == args.run]
    elif args.session:
        runs = [r for r in runs if args.session in r["sessions"]]
    elif not args.all:
        runs = runs[-1:]
    if not runs:
        sys.exit(f"nothing to report in {log}")
    for r in runs:
        print(build(log, args.session or r["sessions"][0], args.trigger, r))


INDEX = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Agent run reports</title>
<style>""" + reportui.CSS + """</style></head><body><main><h1>Agent run reports</h1>
<div class="sub">One row per report — written at the end of each run and at every compaction. Newest first.
 · <a href="fleet.html"><b>Fleet view</b></a>: every agent on one timeline, and how they share the GPUs</div>
<section class="card" style="overflow-x:auto"><table><tr><th>Start</th><th>Agent</th><th>Task</th><th>Trigger</th>
<th class="num">Min</th><th class="num">Turns</th><th class="num">Peak ctx</th><th class="num">Thinking</th>
<th class="num">Gen t/s</th><th class="num">GPU share %</th><th>Outcome</th></tr>
__ROWS__</table></section></main></body></html>"""


TEMPLATE_JS = r"""const S=D.stats,catIdx=Object.fromEntries(D.cats.map((c,i)=>[c.key,i])),catCol=k=>col(catIdx[k]);
const tot=Object.values(D.final).reduce((a,b)=>a+b,0)||1,gsum=Object.values(D.growth).reduce((a,b)=>a+b,0)||1;
const pk=S.peak_pct,pkc=pk>=85?'bad':pk>=65?'warn':'';
const who=D.worker&&D.worker!=='main'?`${esc(D.project)} / <b>${esc(D.worker)}</b>`:esc(D.project);
const merge={yes:'merged',conflict:'<span class="warn">merge conflict</span>',parked:'<span class="bad">parked</span>',nothing:'no changes'}[D.merged]||'';
let h=`<h1>${esc(D.title)}</h1><div class="sub">${who} · ${D.start} · ${mins(S.wall_min)} · session <span class="mono">${D.sid.slice(0,8)}</span>${D.branch?' · '+esc(D.branch):''}${merge?' · '+merge:''} · report on <b>${D.trigger}</b> · <a href="../index.html">all reports</a> · <a href="../fleet.html">fleet</a></div>`;
h+='<div class="tiles">'+[
 tile('Peak context',k(S.peak),`${pk}% of ${k(D.window)} window${S.real_peak?` · Ollama saw ${k(S.real_peak)}`:''}`,pkc),
 tile('Thinking share',S.think_pct+'%','of all context growth'),
 tile('Turns',S.turns,`${S.compactions} compaction${S.compactions==1?'':'s'} · ${S.retries} retr${S.retries==1?'y':'ies'}`,S.retries?'warn':''),
 tile('Outcome',esc(S.outcome),S.cancelled?`${S.cancelled} request${S.cancelled>1?'s':''} cancelled`:'',S.outcome==='success'?'':'bad'),
 tile('Generation',S.gen_tps?f1(S.gen_tps)+' t/s':'–',`${k(S.gen_tok)} tokens out · median`),
 tile('Prefill',S.pp_tps?k(S.pp_tps)+' t/s':'–',`cache reuse ${S.reuse_pct??'–'}% · ${S.full_reprocess} full reprocess`,S.full_reprocess>S.requests/3?'warn':''),
 D.share?tile('GPU share',D.share.mine_pct+'%',`of busy GPU time · ${D.share.agents} agent${D.share.agents>1?'s':''} · overlap ${D.share.overlap_pct}%`):'',
 tile('GPU util',S.util_avg!=null?S.util_avg+'%':'–','average, all GPUs'),
 tile('VRAM peak',S.vram_peak!=null?f1(S.vram_peak)+' GB':'–',`of ${S.vram_cap||'–'} GB · ${S.power_avg??'–'} W avg · ${S.energy_wh} Wh`),
].join('')+'</div>';

// time split
const tw=[['Prefill (reading prompt)',S.prefill_s,col(0)],['Generation',S.gen_s,col(1)],['Tools + idle',S.other_s,col(2)]],tws=tw.reduce((a,b)=>a+b[1],0)||1;
h+=card('Where the wall-clock time went',`${mins(S.wall_min)} total over ${S.requests} model requests`,
 '<div class="bar100">'+tw.map(t=>`<div style="flex:${t[1]};background:${t[2]}" data-t="${esc(t[0])}: ${mins(t[1]/60)} (${Math.round(100*t[1]/tws)}%)"></div>`).join('')+'</div>'+
 legend(tw.map(t=>({n:`${t[0]} ${Math.round(100*t[1]/tws)}% · ${mins(t[1]/60)}`,c:t[2]}))));

if(D.share)h+=card('Shared GPU during this run',`Every agent's model requests on the same Ollama; this run is <b>${esc(D.tag)}</b>. Solid = generating, faded = reading the prompt. While requests overlap, each is charged an equal part of the GPU time.`,
 '<div id="lanes"></div><table style="margin-top:8px"><tr><th>Agent</th><th class="num">Requests</th><th class="num">GPU time</th><th class="num">Share</th></tr>'+
 D.share.rows.map(r=>`<tr><td><span class="sw" style="background:${r.tag===D.tag?col(0):'var(--muted)'}"></span>${esc(r.tag)}</td><td class="num">${r.n}</td><td class="num">${mins(r.s/60)}</td><td class="num">${r.pct}%</td></tr>`).join('')+'</table>');
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
 +card('Model requests: prompt cache','Prompt tokens reused from Ollama\'s cache vs re-processed from scratch',
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
if(D.share)lanes(document.getElementById('lanes'),{x1:S.wall_min,xfmt:v=>Math.round(v)+'m',
 lanes:D.share.lanes.map(l=>({name:l.tag,c:l.tag===D.tag?col(0):'var(--muted)',runs:l.runs,reqs:l.reqs}))});

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
"""

if __name__ == "__main__":
    main()
