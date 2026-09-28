#!/usr/bin/env python3
"""Fleet view: every agent on one timeline and how they share the GPUs -> reports/fleet.html.

  fleet.py [--hours N]     (default 24; rebuilt automatically after every run report)

Agents are identified by the keepalive ledger tag (project, or project/wK for worktree agents).
GPU time is split fairly: while n requests run at once, each is charged 1/n of the time.
"""
import argparse
import html
import json
import os
import time
from collections import defaultdict
from pathlib import Path

import reportui
from telemetry import (ROOT, result, task_title, task_state, agent_tag, attribute, clean_task, fair_share, gpu_samples, harness_runs, ledger,
                       ollama_requests, vram_budget)

REPORTS = ROOT / "reports"
BUCKET = 60


def live_agents():
    out = []
    for f in (ROOT / "run").glob("*/workers/*.pid"):
        try:
            os.kill(int(f.read_text()), 0)
            out.append(f"{f.parent.parent.name}{'' if f.stem == 'main' else '/' + f.stem}")
        except (OSError, ValueError):
            pass
    return sorted(out)


def build(hours=24):
    now = time.time()
    t0 = now - hours * 3600
    runs, rows = harness_runs(t0), ledger(t0, now)
    first = min([r["start"]["t"] for r in runs] + [e["t0"] for e in rows], default=None)
    REPORTS.mkdir(exist_ok=True)
    if first is None:
        (REPORTS / "fleet.html").write_text(reportui.page("Agent fleet", {}, "app.innerHTML='<h1>Agent fleet</h1>"
                                            "<div class=\"note\">No runs from the multi-agent harness yet.</div>'"))
        return
    t0 = max(t0, first - 120)
    reqs = attribute(ollama_requests(t0, now), rows)
    sec, busy, overlap = fair_share(reqs, t0, now)
    agents = sorted(({r["tag"] for r in reqs} | {agent_tag(r["start"]) for r in runs}) - {"other clients"})
    tags = agents + (["other clients"] if any(r["tag"] == "other clients" for r in reqs) else [])

    # Requests in flight per agent, per minute (0..slots).
    n = int((now - t0) // BUCKET) + 1
    occ = {t: [0.0] * n for t in tags}
    for r in reqs:
        a, b = max(r["t"], t0), min(r["end"], now)
        i = int((a - t0) // BUCKET)
        while a < b and i < n:
            edge = t0 + (i + 1) * BUCKET
            occ[r["tag"]][i] += (min(b, edge) - a) / BUCKET
            a, i = edge, i + 1

    links = {}
    for f in REPORTS.glob("*/*.json"):
        s = json.loads(f.read_text())
        if s.get("run"):
            links[s["run"]] = f"{f.parent.name}/{f.stem}.html"
    lanes = defaultdict(lambda: {"runs": [], "reqs": []})
    for r in reqs:
        lanes[r["tag"]]["reqs"].append({
            "a": round((r["t"] - t0) / 60, 3), "b": round(max((r["end"] - t0) / 60, (r["t"] - t0) / 60 + .01), 3),
            "p": round(r["prefill_s"] / 60, 3),
            "t": f"<b>{html.escape(r['tag'])}</b> · slot {r['slot']} · {time.strftime('%H:%M', time.localtime(r['t']))}"
                 f"<div class='note'>{r['prompt'] / 1000:.1f}k prompt · {r['prefill_s']:.0f}s reading · "
                 f"{r['gen']} tokens in {r['gen_s']:.0f}s{' · cancelled' if r['cancelled'] else ''}</div>"})
    per = {t: {"runs": 0, "done": 0, "open": 0, "interrupted": 0, "fixes": 0, "merged": 0, "conflicts": 0, "parked": 0, "run_s": 0.0} for t in tags}
    for run in runs:
        st, en = run["start"], run["end"]
        tag = agent_tag(st)
        b = en["t"] if en else now
        p = per.setdefault(tag, {"runs": 0, "done": 0, "open": 0, "interrupted": 0, "fixes": 0, "merged": 0, "conflicts": 0, "parked": 0, "run_s": 0.0})
        p["runs"] += 1
        p["run_s"] += b - st["t"]
        if en:
            m, ts = en.get("merged"), task_state(st, en)
            p["done"] += ts == "done"
            p["open"] += ts == "open"
            p["interrupted"] += ts == "interrupted"
            p["fixes"] += ts == "merge-fix"
            p["merged"] += m == "yes" or (m == "n/a" and en.get("head") != st.get("base"))
            p["conflicts"] += m == "conflict"
            p["parked"] += m == "parked"
        href = links.get(st["run"])
        lanes[tag]["runs"].append({
            "a": round((st["t"] - t0) / 60, 3), "b": round((b - t0) / 60, 3), "href": href,
            "t": f"<b>{html.escape(tag)}</b> · {html.escape(task_title(st.get('task'), st.get('project'))[0] or 'run')}"
                 f"<div class='note'>{time.strftime('%H:%M', time.localtime(st['t']))} · {(b - st['t']) / 60:.0f} min"
                 f" · {result(st, en)[0]}{' · click for its report' if href else ''}</div>"
                 + (f"<div class='note'>{html.escape(en['note'])}</div>" if en and en.get("note") else "")})

    table = []
    for t in tags:
        mine = [r for r in reqs if r["tag"] == t]
        tps = sorted(r["tps"] for r in mine if r["tps"])
        prompts = sorted(r["prompt"] for r in mine)
        p = per.get(t, {})
        table.append({"tag": t, "runs": p.get("runs", 0), "done": p.get("done", 0), "open": p.get("open", 0),
                      "interrupted": p.get("interrupted", 0), "fixes": p.get("fixes", 0), "merged": p.get("merged", 0), "conflicts": p.get("conflicts", 0),
                      "parked": p.get("parked", 0), "run_s": round(p.get("run_s", 0)), "requests": len(mine),
                      "prefill_s": round(sum(r["prefill_s"] for r in mine)), "gen_s": round(sum(r["gen_s"] for r in mine)),
                      "gen_tok": sum(r["gen"] for r in mine), "tps": tps[len(tps) // 2] if tps else None,
                      "prompt": prompts[len(prompts) // 2] if prompts else None,
                      "share_s": round(sec.get(t, 0)), "share_pct": round(100 * sec.get(t, 0) / busy, 1) if busy else 0})

    gpu = gpu_samples(t0, now, step=BUCKET)
    gids = sorted({i for s in gpu for i in s["g"]})
    power = [sum(v[3] for v in s["g"].values()) for s in gpu]
    util = [v[0] for s in gpu for v in s["g"].values()]
    cap = sum(v[2] for v in gpu[-1]["g"].values()) if gpu else 0
    gen_tok = sum(r["gen"] for r in reqs)
    data = {
        "t0": t0, "span_min": round((now - t0) / 60, 1), "hours": hours, "tags": tags, "live": live_agents(),
        "lanes": [{"tag": t, **lanes[t]} for t in tags], "table": table,
        "occ": {"x": [round(i * BUCKET / 60, 2) for i in range(n)], "v": {t: [round(v, 3) for v in occ[t]] for t in tags}},
        "gpu": {"ids": gids, "t": [round((s["t"] - t0) / 60, 2) for s in gpu],
                "util": [[round(s["g"][i][0]) if i in s["g"] else None for s in gpu] for i in gids],
                "vram": [[round(s["g"][i][1], 2) if i in s["g"] else None for s in gpu] for i in gids],
                "power": [[round(s["g"][i][3]) if i in s["g"] else None for s in gpu] for i in gids], "cap_gb": round(cap, 1)},
        "vram": vram_budget(), "slots": max((r["slot"] for r in reqs), default=0) + 1,
        "stats": {"agents": len(agents), "runs": len(runs), "finished": sum(1 for r in runs if r["end"]),
                  "tasks_done": sum(p["done"] for p in per.values()), "not_done": sum(p["open"] for p in per.values()),
                  "fixes": sum(p["fixes"] for p in per.values()),
                  "merged": sum(p["merged"] for p in per.values()), "conflicts": sum(p["conflicts"] for p in per.values()),
                  "requests": len(reqs), "gen_tok": gen_tok, "agg_tps": round(gen_tok / busy, 1) if busy else None,
                  "busy_pct": round(100 * busy / (now - t0), 1),
                  "reuse_pct": round(100 * sum(r["reused"] for r in reqs) / max(1, sum(r["prompt"] for r in reqs)), 1),
                  "servers": sorted({r["server"] for r in reqs}), "overlap_pct": round(100 * overlap / busy, 1) if busy else 0,
                  "util_avg": round(sum(util) / len(util)) if util else None,
                  "energy_kwh": round(sum(power) * BUCKET / 3600 / 1000, 2) if power else None},
        "built": time.strftime("%Y-%m-%d %H:%M"),
    }
    (REPORTS / "fleet.html").write_text(reportui.page("Agent fleet", data, JS))


JS = r"""const S=D.stats,T=D.tags,agentCol=t=>t==='other clients'?'var(--muted)':col(T.indexOf(t));
const clock=v=>{const d=new Date((D.t0+v*60)*1000);return d.getHours().toString().padStart(2,'0')+':'+d.getMinutes().toString().padStart(2,'0')};
const hrs=s=>s>=3600?(s/3600).toFixed(1)+' h':Math.round(s/60)+' min';
let h=`<h1>Agent fleet</h1><div class="sub">Last ${mins(D.span_min)} · built ${D.built} · ${D.live.length?'running now: <b>'+D.live.map(esc).join(', ')+'</b>':'no agents running'} · <a href="index.html">all reports</a></div>`;
h+='<div class="tiles">'+[
 tile('Agents',S.agents,`${D.slots} model slot${D.slots>1?'s':''} in use · ${S.servers.map(esc).join(' + ')||'–'}`),
 tile('Tasks done',S.tasks_done,`in ${S.runs} runs · ${S.not_done} not finished · ${S.fixes} merge-fix runs`,S.not_done?'warn':''),
 tile('Merge conflicts',S.conflicts,S.conflicts?'resolved by the agent on its next run':'none',S.conflicts?'warn':''),
 tile('GPU busy',S.busy_pct+'%',`of the time · 2+ requests at once ${S.overlap_pct}% of busy time`),
 tile('Throughput',S.agg_tps!=null?f1(S.agg_tps)+' t/s':'–',`${k(S.gen_tok)} tokens generated, all agents`),
 tile('Prompt cache',S.reuse_pct+'%',`of prompt tokens reused, not re-read`,S.reuse_pct<50?'warn':''),
 tile('GPU util',S.util_avg!=null?S.util_avg+'%':'–',S.energy_kwh!=null?`average · ${S.energy_kwh} kWh`:'average'),
].join('')+'</div>';
h+=card('Timeline','One lane per agent. Pale bars are runs (click one for its report); solid segments are model requests, with the faded head spent reading the prompt.',
 '<div id="lanes"></div>'+legend(T.map(t=>({n:t,c:agentCol(t)}))));
h+=card('Requests in flight','Model requests running at once, per agent, averaged per minute. At the slot count the GPUs are shared by every request.',
 '<div id="occ"></div>'+legend(T.map(t=>({n:t,c:agentCol(t)}))));
const V=D.vram;
if(V){const gib=m=>m/1024,cap=D.gpu.cap_gb||gib(V.weights+V.kv+V.recurrent+V.compute),free=Math.max(0,cap-gib(V.weights+V.kv+V.recurrent+V.compute)),
 parts=[['Model weights',gib(V.weights),col(0)],[`KV cache · ${V.slots} slot${V.slots>1?'s':''} × ${k(V.ctx_slot)} tokens`,gib(V.kv),col(1)],['Recurrent state',gib(V.recurrent),col(2)],['Compute buffers',gib(V.compute),col(3)],['Free',free,'var(--grid)']];
 h+=card('How the model uses VRAM',`From the model server's last load, across all GPUs (${f1(cap)} GB). Each slot owns its own KV cache; weights are shared.`,
 '<div class="bar100">'+parts.map(p=>`<div style="flex:${p[1]};background:${p[2]}" data-t="${esc(p[0])}: ${f1(p[1])} GB"></div>`).join('')+'</div>'+legend(parts.map(p=>({n:`${p[0]} ${f1(p[1])} GB`,c:p[2]}))))}
const gid=D.gpu.ids.map((g,i)=>({n:'GPU '+g,c:col(i)}));
h+='<div class="grid3">'+card('GPU utilization','% per GPU, 1-minute average','<div id="gu"></div>'+legend(gid))
 +card('VRAM','GB per GPU','<div id="gv"></div>'+legend(gid))+card('Power','W per GPU','<div id="gp"></div>'+legend(gid))+'</div>';
h+=card('Per agent','GPU share charges overlapping requests equally. Done = runs that finished their task; not finished = ended with the task unchecked (see the note on its lane).',
 '<div style="overflow-x:auto"><table><tr><th>Agent</th><th class="num">Runs</th><th class="num">Tasks done</th><th class="num">Not finished</th><th class="num">Interrupted</th><th class="num">Merge conflicts</th><th class="num">Run time</th><th class="num">Requests</th><th class="num">Median prompt</th><th class="num">Reading</th><th class="num">Generating</th><th class="num">Tokens out</th><th class="num">Median t/s</th><th class="num">GPU share</th></tr>'+
 D.table.map(r=>`<tr><td><span class="sw" style="background:${agentCol(r.tag)}"></span>${esc(r.tag)}</td><td class="num">${r.runs}</td><td class="num">${r.done}</td><td class="num">${r.open}</td><td class="num">${r.interrupted}</td><td class="num">${r.conflicts}${r.parked?' · '+r.parked+' parked':''}</td><td class="num">${hrs(r.run_s)}</td><td class="num">${r.requests}</td><td class="num">${k(r.prompt)}</td><td class="num">${hrs(r.prefill_s)}</td><td class="num">${hrs(r.gen_s)}</td><td class="num">${k(r.gen_tok)}</td><td class="num">${f1(r.tps)}</td><td class="num">${r.share_pct}%</td></tr>`).join('')+'</table></div>');
app.innerHTML=h;
document.querySelectorAll('[data-t]').forEach(n=>hover(n,esc(n.dataset.t)));
lanes(document.getElementById('lanes'),{x1:D.span_min,xfmt:clock,lanes:D.lanes.map(l=>({name:l.tag,c:agentCol(l.tag),runs:l.runs,reqs:l.reqs}))});
const occMax=Math.max(D.slots,...D.occ.x.map((_,i)=>T.reduce((a,t)=>a+D.occ.v[t][i],0)));
chart(document.getElementById('occ'),{x:D.occ.x,stacked:true,H:150,ymax:occMax,yfmt:v=>f1(v),xfmt:clock,
 series:T.map(t=>({n:t,c:agentCol(t),v:D.occ.v[t]})),tipx:i=>`<b>${clock(D.occ.x[i])}</b>`});
const gx={x:D.gpu.t,xfmt:clock,H:160,tipx:i=>`<b>${clock(D.gpu.t[i])}</b>`};
chart(document.getElementById('gu'),{...gx,ymax:100,yfmt:v=>Math.round(v)+'%',series:D.gpu.ids.map((g,i)=>({n:'GPU '+g,c:col(i),v:D.gpu.util[i]}))});
chart(document.getElementById('gv'),{...gx,ymax:D.gpu.ids.length?D.gpu.cap_gb/D.gpu.ids.length:null,yfmt:v=>f1(v),series:D.gpu.ids.map((g,i)=>({n:'GPU '+g,c:col(i),v:D.gpu.vram[i]}))});
chart(document.getElementById('gp'),{...gx,yfmt:v=>Math.round(v)+'W',series:D.gpu.ids.map((g,i)=>({n:'GPU '+g,c:col(i),v:D.gpu.power[i]}))});
"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24)
    build(ap.parse_args().hours)
    print(REPORTS / "fleet.html")
