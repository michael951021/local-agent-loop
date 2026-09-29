#!/usr/bin/env python3
"""The standup board: tickets as To Do / In Progress / Done, on top of the plan hierarchy.

  standup.py [PROJECT ...]        (default: every project with a TODO.md; ctxreport --index calls it)

Writes reports/standup.html. The plan is the backbone: the `# Goal` is the big goal, each top-level `##`
section is an Initiative with its own colour, deeper `###`/`####` sections are plan levels, and every
`- [ ]` line is a ticket that carries its initiative's colour and shows its full breadcrumb.

Status comes from the live loop, not a second copy: a checked line is Done (with its diff and post-mortem
from the run report), a claimed line is In Progress (which agent, current step, elapsed), everything else is
To Do (ready, or waiting on another ticket). A ticket's terse spec is state/tickets/<id>.md, written by the
agent when it picks the ticket up.
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plantree  # noqa: E402
import reportui  # noqa: E402
import sched  # noqa: E402
from plan import Plan  # noqa: E402
from telemetry import ROOT  # noqa: E402

REPORTS = ROOT / "reports"
RUN = ROOT / "run"
TASKLINE = re.compile(r"^\s*- \[( |x|X)\] (.*)$")
HEAD = re.compile(r"^#+\s*(.+?)\s*$")


def read_spec(proj_dir, tid):
    """The ticket's expanded spec (state/tickets/<tid>.md), as {issue, done_when, plan}, or None."""
    f = proj_dir / "state" / "tickets" / f"{tid}.md"
    if not f.is_file():
        return None
    secs, cur, buf = {}, None, []
    for line in f.read_text().splitlines():
        h = HEAD.match(line)
        if h:
            if cur:
                secs[cur] = "\n".join(buf).strip()
            cur, buf = h.group(1).lower(), []
        elif cur is not None:
            buf.append(line)
    if cur:
        secs[cur] = "\n".join(buf).strip()
    g = lambda *names: next((secs[n] for n in names if secs.get(n)), "")
    spec = {"issue": g("issue"), "done_when": g("done when", "done-when", "done", "completion", "tests"),
            "plan": g("plan", "solution plan", "solution")}
    return spec if any(spec.values()) else {"issue": "\n".join(f.read_text().splitlines()[:15]).strip(), "done_when": "", "plan": ""}


def latest_reports(project):
    """{ticket title: newest end-report summary}, so a Done ticket shows its diff and post-mortem."""
    out = {}
    d = REPORTS / project
    if not d.exists():
        return out
    for f in d.glob("*.json"):
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if s.get("trigger") != "end" or not s.get("title"):
            continue
        t = s["title"]
        if t not in out or s.get("start", "") >= out[t].get("start", ""):
            s["_page"] = f"{project}/{f.stem}.html"
            out[t] = s
    return out


def node(sec, icol):
    """A section as a plan-tree node: ticket counts, the tids beneath it, children."""
    tasks = [{"tid": sched.ticket_id(t.text), "title": t.title, "done": t.done} for t in sec.tasks]
    kids = [node(c, icol) for c in sec.children]
    tids = [t["tid"] for t in tasks] + [x for k in kids for x in k["tids"]]
    total = len(tasks) + sum(k["total"] for k in kids)
    done = sum(t["done"] for t in tasks) + sum(k["done"] for k in kids)
    return {"title": sec.title, "why": sec.why, "color": icol.get(sec.title),
            "total": total, "done": done, "tids": tids, "children": kids}


def build_project(project):
    proj_dir = ROOT / "projects" / project
    todo_text = (proj_dir / "TODO.md").read_text()
    plan = Plan.parse(todo_text)
    icol = plan.initiative_colors()
    inits = [{"title": s.title, "why": s.why, "color": icol[s.title]} for s in plan.initiatives()]

    claims, failed = {}, {}
    st_dir = RUN / project
    if st_dir.exists():
        st = sched.State(st_dir)
        claims = st.claims()
        failed = st.load("failed.json", {})
    files = sched.Files(proj_dir)
    open_state = {e["text"]: (state, why) for e, state, why in sched.plan(files, claims, failed)}
    reps = latest_reports(project)

    tickets = []
    for i, line in enumerate(todo_text.splitlines(), 1):
        m = TASKLINE.match(line)
        if not m:
            continue
        done, text = m.group(1) != " ", m.group(2)
        title, spec = sched.split_title(text)
        tid = sched.ticket_id(text)
        path = [s.model_dump() for s in plan.path(i)]
        color = next((icol[s["title"]] for s in path if s["title"] in icol), None)
        crumb = [s for s in path if s["kind"] != "goal"]
        t = {"tid": tid, "title": title, "spec": spec, "crumb": crumb, "color": color,
             "milestone": "(checklist:" in text}
        expanded = read_spec(proj_dir, tid)
        if done:
            r = reps.get(title, {})
            t.update(status="done", result=r.get("result"), note=r.get("note"), summary=r.get("summary"),
                     diff=r.get("diff"), report=r.get("_page"), duration=r.get("wall_min"),
                     when=r.get("start"), attempts=(r.get("attempt") or {}).get("n", 1))
        elif sched.key(text) in claims:
            c = claims[sched.key(text)]
            worker = c.get("worker")
            if worker and worker != "main":   # an in-progress spec lives on the agent's worktree, not merged yet
                expanded = read_spec(ROOT / "work" / project / worker, tid) or expanded
            step = sched.split_title(c.get("task", ""))[0]
            t.update(status="doing", worker=worker, step=step if c.get("task") != text else "",
                     elapsed=round((time.time() - c.get("since", time.time())) / 60))
        else:
            state, why = open_state.get(text, ("ready", ""))
            t.update(status="todo", substate=state,
                     blocker=sched.split_title(why)[0] if why and state in ("waiting", "blocked-failed") else "")
        t["expanded"] = expanded
        tickets.append(t)

    root = plan.root
    tree = {"title": root.title, "why": root.why, "color": None, "total": sum(n["total"] for n in [node(c, icol) for c in root.children]),
            "done": 0, "tids": [], "children": [node(c, icol) for c in root.children]}
    tree["done"] = sum(c["done"] for c in tree["children"])
    tree["tids"] = [x for c in tree["children"] for x in c["tids"]]
    return {"project": project, "goal": root.title, "why": root.why,
            "initiatives": inits, "tickets": tickets, "tree": tree}


def main(argv=None):
    projects = (argv if argv is not None else []) or sorted(
        p.name for p in (ROOT / "projects").iterdir()
        if p.is_dir() and (p / "TODO.md").exists() and not p.name.startswith(("smoke", "zz")))
    data = []
    for p in projects:
        try:
            data.append(build_project(p))
        except Exception as e:   # one bad project must not sink the board
            print(f"standup {p}: {e!r}", file=sys.stderr)
    # newest activity first, so the loop you are running shows on top
    data.sort(key=lambda d: max([t.get("when") or "" for t in d["tickets"]] + [""]), reverse=True)
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "standup.html").write_text(reportui.page("Standup", {"projects": data}, JS))
    print(REPORTS / "standup.html")


JS = r"""
const P=D.projects; const icol=i=>i==null?'var(--muted)':`var(--c${i%8+1})`;
let cur=0, filt=null;   // filt: {label, tids:Set} or null

function esc2(s){return esc(s==null?'':s)}
const STATE={ready:'ready',waiting:'waiting',claimed:'in progress','blocked-failed':'blocked',failed:'given up'};

function crumbHtml(t){return (t.crumb||[]).filter(s=>s.kind!=='task').map(s=>`<span title="${esc2(s.why)}">${esc2(s.title)}</span>`).join(' <span style="color:var(--axis)">›</span> ')}

function tcard(t){
 const c=icol(t.color), sub=t.status==='todo'?(t.substate==='waiting'?`waiting · ${esc2(t.blocker)}`:t.substate==='failed'||t.substate==='blocked-failed'?STATE[t.substate]:'ready')
   :t.status==='doing'?`${esc2(t.worker)}${t.step?' · '+esc2(t.step):''} · ${t.elapsed} min`
   :(t.result?esc2(t.result.label):'done')+(t.attempts>1?` · attempt ${t.attempts}`:'');
 const dim=t.status==='todo'&&t.substate!=='ready';
 return `<div class="tk" data-tid="${esc2(t.tid)}" style="border-left:3px solid ${c};${dim?'opacity:.6':''}">
   <div class="cr">${crumbHtml(t)||'&nbsp;'}</div>
   <div class="tt">${esc2(t.title)}</div>
   <div class="ts" style="color:${t.status==='done'&&t.result?`var(--${t.result.cls||'muted'})`:'var(--muted)'}">${sub}</div></div>`}

function board(){
 const p=P[cur]; let tks=p.tickets;
 if(filt) tks=tks.filter(t=>filt.tids.has(t.tid));
 const cols=[['To Do','todo'],['In Progress','doing'],['Done','done']];
 return `<div class="cols">`+cols.map(([name,st])=>{
   let list=tks.filter(t=>t.status===st);
   if(st==='done') list=[...list].reverse();
   return `<div class="col"><div class="ch">${name} <span class="n">${list.length}</span></div>${list.map(tcard).join('')||'<div class="empty">–</div>'}</div>`}).join('')+`</div>`}

// initiative-coloured icicle of the plan; click a block filters the board to its tickets
function icicle(host,tree){
 let focus=tree; const par=new Map(); (function w(n){(n.children||[]).forEach(c=>{par.set(c,n);w(c)})})(tree);
 const initColor=n=>{let x=n;while(par.get(x)&&par.get(x)!==tree)x=par.get(x);return x.color};
 const draw=()=>{host.innerHTML='';const W=host.clientWidth||900,RH=30;
  const dep=(function d(n){return 1+Math.max(0,...(n.children||[]).filter(c=>c.total>0).map(d))})(focus);
  const H=Math.min(dep,6)*RH+2,svg=el('svg',{viewBox:`0 0 ${W} ${H}`,height:H},host);
  const lay=(n,x0,x1,d)=>{if(d>=6||x1-x0<1)return;const w=x1-x0,y=d*RH,g=el('g',{},svg);
   const c=n===tree?'var(--muted)':icol(initColor(n));
   el('rect',{x:x0+.5,y:y+.5,width:Math.max(0,w-1),height:RH-2,rx:3,fill:c,'fill-opacity':n===tree?.25:Math.max(.2,.85-d*.16)},g);
   if(w>52){const tx=el('text',{x:x0+6,y:y+RH/2+4,style:`fill:${d?'#fff':'var(--ink)'};font-size:11px`},g);
    let s=n.title+(w>150?`  ·  ${n.done}/${n.total}`:'');const mx=Math.floor((w-10)/6.2);tx.textContent=s.length>mx?s.slice(0,mx-1)+'…':s}
   hover(g,`<b>${esc2(n.title)}</b><div>${n.done}/${n.total} tickets done</div>${n.why?`<div class="note">${esc2(n.why)}</div>`:''}`);
   g.style.cursor='pointer';g.onclick=()=>{if(d===0){focus=par.get(focus)||tree;filt=null}else{focus=n;filt={label:n.title,tids:new Set(n.tids)}}render()};
   const kids=(n.children||[]).filter(c=>c.total>0);let x=x0;const tot=kids.reduce((a,c)=>a+c.total,0)||1;
   for(const c of kids){const cw=w*c.total/Math.max(tot,n.total||tot);lay(c,x,x+cw,d+1);x+=cw}};
  lay(focus,0,W,0)};
 draw();new ResizeObserver(draw).observe(host)}

function drawer(t){
 if(!t){document.getElementById('dw').className='';return}
 const e=t.expanded, dw=document.getElementById('dw');
 const sec=(h,v)=>v?`<h3>${h}</h3><div class="dv">${esc2(v)}</div>`:'';
 let h=`<button id="dx">✕</button><div class="cr">${crumbHtml(t)}</div><h2 style="border-left:3px solid ${icol(t.color)};padding-left:8px">${esc2(t.title)}</h2>`;
 h+=`<div class="note">${esc2(t.spec)}</div>`;
 if(t.status==='doing')h+=`<div class="pill">In progress · ${esc2(t.worker)}${t.step?' · '+esc2(t.step):''} · ${t.elapsed} min</div>`;
 if(t.status==='todo')h+=`<div class="pill">${t.substate==='waiting'?'Waiting on '+esc2(t.blocker):STATE[t.substate]||'ready'}</div>`;
 if(e){h+=sec('Issue',e.issue)+sec('Done when',e.done_when)+sec('Plan',e.plan)}
 else h+=`<div class="note" style="margin-top:10px">No spec yet — the agent writes state/tickets/${esc2(t.tid)}.md when it picks this up.</div>`;
 if(t.status==='done'){h+='<hr>';
  if(t.result)h+=`<div class="pill" style="color:var(--${t.result.cls||'muted'})">${esc2(t.result.label)}</div> <span class="note">${esc2(t.result.why)}</span>`;
  h+=sec('Post-mortem',t.summary)+sec("Agent's note",t.note);
  const bits=[];
  if(t.diff&&t.diff.files)bits.push(`<a href="${esc2(t.diff.path)}">${t.diff.files} file${t.diff.files==1?'':'s'} · +${t.diff.add}/−${t.diff.del}</a>`);
  if(t.report)bits.push(`<a href="${esc2(t.report)}">run report</a>`);
  if(t.when)bits.push(`${esc2(t.when)}${t.duration!=null?' · '+mins(t.duration):''}`);
  if(bits.length)h+=`<div class="dv" style="margin-top:8px">${bits.join(' · ')}</div>`}
 dw.innerHTML=h;dw.className='open';document.getElementById('dx').onclick=()=>drawer(null)}

function render(){
 const p=P[cur];
 let h=`<h1>Standup</h1><div class="sub">Tickets on the plan hierarchy: the goal, its colour-coded initiatives, and every ticket beneath one. Click a ticket for its spec, diff and post-mortem; click a block in the map to filter. · <a href="plan.html">plan &amp; time</a> · <a href="index.html">all reports</a></div>`;
 if(P.length>1)h+=`<div class="tabs">${P.map((q,i)=>`<button class="${i===cur?'on':''}" onclick="cur=${i};filt=null;render()">${esc2(q.project)}</button>`).join('')}</div>`;
 h+=`<div class="goal"><b>${esc2(p.goal)}</b>${p.why?`<div class="note">${esc2(p.why)}</div>`:''}</div>`;
 h+=`<div class="lg">${p.initiatives.map(it=>`<span class="ic" title="${esc2(it.why)}"><span class="sw" style="background:${icol(it.color)}"></span>${esc2(it.title)}</span>`).join('')}</div>`;
 h+=`<section class="card"><div class="note" style="margin:0 0 6px">Plan map — width = ticket count, colour = initiative. Click to filter the board; click the top bar to zoom out.${filt?` <b>Filtered: ${esc2(filt.label)}</b> <a href="#" onclick="filt=null;render();return false">clear</a>`:''}</div><div id="ice"></div></section>`;
 h+=board();
 app.innerHTML=h;
 icicle(document.getElementById('ice'),p.tree);
 document.querySelectorAll('.tk').forEach(el=>el.onclick=()=>{const t=p.tickets.find(x=>x.tid===el.dataset.tid);drawer(t)});
}
document.head.insertAdjacentHTML('beforeend',`<style>
.tabs{display:flex;gap:6px;margin:12px 0}.tabs button{font:inherit;background:var(--surface);border:1px solid var(--ring);color:var(--ink2);border-radius:7px;padding:4px 12px;cursor:pointer}.tabs button.on{color:var(--ink);border-color:var(--axis);font-weight:600}
.goal{margin:14px 0 6px;font-size:15px}.lg{display:flex;flex-wrap:wrap;gap:4px 14px;margin:8px 0 4px;font-size:12px;color:var(--ink2)}.ic{display:inline-flex;align-items:center}
.cols{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:8px;align-items:start}
@media(max-width:800px){.cols{grid-template-columns:1fr}}
.col{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:10px}
.ch{font-weight:600;font-size:13px;margin:2px 4px 8px}.ch .n{color:var(--muted);font-weight:400}
.tk{background:var(--bg);border:1px solid var(--ring);border-radius:9px;padding:8px 10px;margin-bottom:7px;cursor:pointer}
.tk:hover{border-color:var(--axis)}.tk .cr{font-size:10.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tk .tt{font-weight:600;font-size:13px;margin:2px 0}.tk .ts{font-size:11.5px}
.empty{color:var(--muted);font-size:12px;padding:6px 4px}
#dw{position:fixed;top:0;right:0;height:100%;width:min(440px,92vw);background:var(--surface);border-left:1px solid var(--ring);box-shadow:-8px 0 32px rgba(0,0,0,.18);padding:22px;overflow:auto;transform:translateX(100%);transition:transform .18s;z-index:20}
#dw.open{transform:none}#dw h2{font-size:17px;margin:6px 0 4px}#dw h3{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:14px 0 3px}
#dw .cr{font-size:11px;color:var(--muted)}#dw .dv{white-space:pre-wrap;font-size:13px;line-height:1.5}
#dw .pill{display:inline-block;font-size:12px;margin:8px 8px 0 0}#dw hr{border:none;border-top:1px solid var(--grid);margin:16px 0}
#dx{position:absolute;top:14px;right:14px;background:none;border:none;color:var(--muted);font-size:16px;cursor:pointer}
</style>`);
app.insertAdjacentHTML('afterend','<div id="dw"></div>');
render();
"""

if __name__ == "__main__":
    main(sys.argv[1:])
