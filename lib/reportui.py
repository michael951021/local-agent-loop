"""Shared look for the report pages: palette tokens (light + dark), and a small SVG chart library
(stacked area / lines / bars with crosshair tooltips, and agent timeline lanes). No CDN, no deps."""
import html
import json

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

LIB = r"""const k=v=>v==null?'–':Math.abs(v)>=1000?(v/1000).toFixed(Math.abs(v)>=1e5?0:1)+'k':String(Math.round(v));
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
function hover(n,h){n.addEventListener('mousemove',e=>showTip(e,h));n.addEventListener('mouseleave',hideTip)}
// Timeline, one lane per agent: runs are pale bars (click opens the run's report), model requests are
// solid segments whose faded head is prompt reading (prefill). lanes=[{name,c,runs:[{a,b,t,href}],reqs:[{a,b,p,t}]}]
function lanes(host,{lanes,x1,xfmt,H0=30}){
 const draw=()=>{host.innerHTML='';if(!lanes.length){host.innerHTML='<div class="note">no data</div>';return}
  const W=host.clientWidth||600,L=Math.min(130,W*.28),R=8,T=4,B=22,pw=W-L-R,H=T+B+lanes.length*H0;
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,height:H},host),X=v=>L+Math.max(0,Math.min(1,v/(x1||1)))*pw;
  for(const t of ticks(x1,6)){if(X(t)>W-R-20)continue;el('line',{x1:X(t),x2:X(t),y1:T,y2:H-B,stroke:'var(--grid)'},svg);
   el('text',{x:X(t),y:H-6,'text-anchor':'middle'},svg).textContent=xfmt(t)}
  lanes.forEach((ln,i)=>{const y=T+i*H0;
   el('text',{x:L-8,y:y+H0/2+4,'text-anchor':'end',style:'fill:var(--ink2)'},svg).textContent=ln.name;
   for(const r of ln.runs){const n=el('rect',{x:X(r.a),y:y+3,width:Math.max(2,X(r.b)-X(r.a)),height:H0-6,rx:4,fill:ln.c,
     'fill-opacity':.14,stroke:ln.c,'stroke-opacity':.45},svg);hover(n,r.t);
     if(r.href){n.style.cursor='pointer';n.addEventListener('click',()=>location.href=r.href)}}
   for(const q of ln.reqs){const xa=X(q.a),xp=X(Math.min(q.b,q.a+q.p)),xb=X(q.b),g=el('g',{},svg);
     el('rect',{x:xa,y:y+H0/2-5,width:Math.max(1,xp-xa),height:10,fill:ln.c,'fill-opacity':.35},g);
     el('rect',{x:xp,y:y+H0/2-5,width:Math.max(1,xb-xp),height:10,fill:ln.c},g);
     el('rect',{x:xa-2,y:y+2,width:Math.max(5,xb-xa+4),height:H0-4,fill:'transparent'},g);hover(g,q.t)}})};
 draw();new ResizeObserver(()=>draw()).observe(host)}
"""


def page(title, data, body_js):
    """A self-contained HTML page: data is embedded as JSON and rendered by body_js."""
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>"
            "<main id=\"app\"></main><div id=\"tip\"></div>"
            f"<script id=\"data\" type=\"application/json\">{json.dumps(data, separators=(',', ':')).replace('</', '<\\/')}</script>"
            "<script>const D=JSON.parse(document.getElementById('data').textContent),app=document.getElementById('app'),"
            "tip=document.getElementById('tip'),col=i=>`var(--c${i+1})`;\n" + LIB + "\n" + body_js + "</script></body></html>")
