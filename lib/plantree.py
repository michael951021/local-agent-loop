#!/usr/bin/env python3
"""The plan hierarchy with the time spent in each part: reports/plan.html.

  plantree.py [PROJECT]     (default: every project with reports; ctxreport.py --index calls it)

Every run report records where its task sat in the plan (goal → sections → milestone → checklist sections →
task). Summing the runs' wall-clock time up that chain gives time per section; the current TODO.md adds the
parts nobody has worked on yet (zero time, shown as open).
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reportui  # noqa: E402
from telemetry import ROOT  # noqa: E402

REPORTS = ROOT / "reports"


def node(title, kind="section", why=None):
    return {"title": title, "kind": kind, "why": why, "min": 0.0, "runs": 0, "done": 0, "open": None, "children": {}}


def add_path(root, path, run=None):
    cur = root
    for step in path:
        if step.get("kind") == "goal":
            if cur is root:
                root["title"], root["why"] = step["title"], root["why"] or step.get("why")
            continue
        kid = cur["children"].get(step["title"])
        if kid is None:
            kid = cur["children"][step["title"]] = node(step["title"], step.get("kind", "section"), step.get("why"))
        kid["why"] = kid["why"] or step.get("why")
        if run:
            kid["min"] += run["min"]
            kid["runs"] += 1
            kid["done"] += run["done"]
        cur = kid
    if run and cur is not root:
        cur["last"] = max(cur.get("last", ""), run["start"])


def from_todo(root, project):
    """Current TODO.md: every section and task, marked open/done, so unworked parts show too."""
    try:
        from plan import Plan
        text = (ROOT / "projects" / project / "TODO.md").read_text()
    except (ImportError, OSError):
        return
    plan = Plan.parse(text)

    def go(sec, trail):
        here = trail + ([{"kind": "section", "title": sec.title, "why": sec.why}] if sec is not plan.root else [])
        for t in sec.tasks:
            kind = "milestone" if "(checklist:" in t.text else "task"
            add_path(root, here + [{"kind": kind, "title": t.title, "why": t.why}])
            leaf = root
            for s in here + [{"title": t.title}]:
                leaf = leaf["children"][s["title"]]
            leaf["open"] = not t.done
        for c in sec.children:
            go(c, here)
    root["title"], root["why"] = plan.root.title, plan.root.why
    go(plan.root, [])


def finish(n):
    """Children to sorted lists; a section's open flag = any open task below it."""
    kids = [finish(c) for c in n["children"].values()]
    n["children"] = sorted(kids, key=lambda c: -c["min"])
    if kids:
        opens = [c["open"] for c in kids if c["open"] is not None]
        n["open"] = any(opens) if opens else n["open"]
    n["min"] = round(n["min"], 1)
    if not n["children"] and n["kind"] == "section":
        n["kind"] = "task"
    return n


def build(project):
    root = node(project, "goal")
    from_todo(root, project)
    total = 0.0
    for f in sorted((REPORTS / project).glob("*.json")):
        s = json.loads(f.read_text())
        if s.get("trigger") != "end" or not s.get("wall_min"):
            continue
        run = {"min": s["wall_min"], "done": int((s.get("result") or {}).get("label", "").startswith("✔")),
               "start": s.get("start", "")}
        path = s.get("path") or [{"kind": "section", "title": "(not placed in the plan)"}, {"kind": "task", "title": s["title"]}]
        title = s["title"].removeprefix("Merge fix: ")   # merge fixes count toward the task they finished
        if path and path[-1].get("kind") == "task" and path[-1]["title"] != title:
            path = path[:-1] + [dict(path[-1], title=title)]
        add_path(root, path, run)
        total += s["wall_min"]
    root["min"] = total
    root["runs"] = sum(c["runs"] for c in root["children"].values())
    return finish(root)


def main(argv=None):
    projects = (argv if argv is not None else []) or sorted(p.name for p in REPORTS.iterdir() if p.is_dir() and (ROOT / "projects" / p.name / "TODO.md").exists()
                                      and not p.name.startswith(("smoke", "zz")))
    trees = [build(p) for p in projects]
    (REPORTS / "plan.html").write_text(reportui.page("Plan and time", {"trees": trees}, JS))
    print(REPORTS / "plan.html")


JS = r"""
const hm=m=>{m=Math.round(m);return m<60?m+' min':Math.floor(m/60)+'h '+String(m%60).padStart(2,'0')+'m'};
const kcol={milestone:col(1),task:col(2),section:col(0),goal:col(6),checklist:col(4)};
let h=`<h1>Plan and time</h1><div class="sub">The plan hierarchy (goal → sections → milestones → checklist sections → tasks) with the wall-clock time agents spent under each part, from every run report. Width = time. Click a block to zoom in, click the top bar to zoom out. Grey = not worked on yet. · <a href="standup.html">standup board</a> · <a href="index.html">all reports</a> · <a href="study.html">study</a></div>`;
D.trees.forEach((t,i)=>{h+=card(esc(t.title),`${hm(t.min)} over ${t.runs} runs${t.why?' · '+esc(t.why):''}`,`<div id="ice${i}"></div>`+
 legend([{n:'section',c:col(0)},{n:'milestone',c:col(1)},{n:'task',c:col(2)},{n:'not worked on',c:'var(--grid)'}]))+
 card('The tree','Every level with its time, runs and tasks done; ✓ = no open tasks left under it in TODO.md.',`<div id="tree${i}" class="mono" style="font-size:12.5px"></div>`)});
app.innerHTML=h;
function icicle(host,root){
 let focus=root;const parents=new Map();(function walk(n){(n.children||[]).forEach(c=>{parents.set(c,n);walk(c)})})(root);
 const draw=()=>{host.innerHTML='';const W=host.clientWidth||900,RH=34;
  const depth=(function d(n){return 1+Math.max(0,...(n.children||[]).filter(c=>c.min>0).map(d))})(focus);
  const H=Math.min(depth,7)*RH+4,svg=el('svg',{viewBox:`0 0 ${W} ${H}`,height:H},host);
  function lay(n,x0,x1,dep){if(dep>=7||x1-x0<1)return;const g=el('g',{},svg),w=x1-x0,y=dep*RH;
   const worked=n.min>0;el('rect',{x:x0+.5,y:y+.5,width:Math.max(0,w-1),height:RH-2,rx:3,fill:worked?(kcol[n.kind]||col(0)):'var(--grid)','fill-opacity':worked?(dep?.85:.35):1},g);
   if(w>46){const t=el('text',{x:x0+6,y:y+RH/2+4,style:`fill:${worked&&dep?'#fff':'var(--ink)'};font-size:11.5px`},g);
    let s=(n.open===false?'✓ ':'')+n.title+(w>150?'  ·  '+hm(n.min):'');const max=Math.floor((w-10)/6.4);t.textContent=s.length>max?s.slice(0,Math.max(1,max-1))+'…':s}
   hover(g,`<b>${esc(n.title)}</b> <span class="note">${esc(n.kind)}</span><div>${hm(n.min)} · ${n.runs} run${n.runs==1?'':'s'} · ${n.done} done${n.open===false?' · ✓ complete':n.open?' · open':''}</div>${n.why?`<div class="note">${esc(n.why)}</div>`:''}`);
   g.style.cursor='pointer';g.addEventListener('click',()=>{focus=dep===0?(parents.get(focus)||root):n;hideTip();draw()});
   const kids=(n.children||[]).filter(c=>c.min>0);let x=x0;const tot=kids.reduce((a,c)=>a+c.min,0)||1;
   for(const c of kids){const cw=w*c.min/Math.max(tot,n.min||tot);lay(c,x,x+cw,dep+1);x+=cw}}
  lay(focus,0,W,0)};
 draw();new ResizeObserver(()=>draw()).observe(host)}
function tree(host,root){
 const mx=Math.max(1,...(root.children||[]).map(c=>c.min));
 const row=(n,d)=>{const kids=n.children||[],id='n'+Math.random().toString(36).slice(2);
  const bar=`<span style="display:inline-block;width:${Math.max(n.min?2:0,160*n.min/mx)}px;height:9px;border-radius:0 3px 3px 0;background:${n.min?(kcol[n.kind]||col(0)):'transparent'};vertical-align:middle"></span>`;
  const line=`<div style="display:grid;grid-template-columns:minmax(0,1fr) 170px 150px;gap:8px;padding:3px 0;border-bottom:1px solid var(--grid)" title="${esc(n.why||'')}">
   <span style="padding-left:${d*16}px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:${n.min?'var(--ink)':'var(--muted)'}">${kids.length?'▸ ':'  '}${n.open===false?'<b style="color:var(--c3)">✓</b> ':''}${esc(n.title)}</span>
   <span>${bar}</span><span class="num">${n.min?hm(n.min)+' · '+n.runs+' run'+(n.runs==1?'':'s'):'–'}</span></div>`;
  return kids.length?`<details ${d<1?'open':''}><summary style="list-style:none;cursor:pointer">${line}</summary>${kids.map(c=>row(c,d+1)).join('')}</details>`:line};
 host.innerHTML=(root.children||[]).map(c=>row(c,0)).join('')}
D.trees.forEach((t,i)=>{icicle(document.getElementById('ice'+i),t);tree(document.getElementById('tree'+i),t)});
"""

if __name__ == "__main__":
    main(sys.argv[1:])
