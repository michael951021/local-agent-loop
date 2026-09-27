#!/usr/bin/env python3
"""The code a run changed, as a page: reports/<project>/diffs/<run id>.html.

  diffpage.py RUN_ID            (re)build one run's page
  diffpage.py --all [--force]   every run in the logs (past ones too); --force rebuilds existing pages
  diffpage.py --live            the runs still going (their branch so far); reportsrv refreshes these

A run's change is base..head (recorded by ./agent before it merges the branch). Runs whose range holds a
merge (merge-conflict fixes) are shown commit by commit, merges as --remerge-diff, i.e. only how the
conflicts were resolved, not the other agents' work that came in with the merge. A run still going is
shown up to its branch's current commit. ctxreport.py calls build() at the end of every run.
"""
import html
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import reportui
from telemetry import ROOT, harness_runs, result, task_title

REPORTS = ROOT / "reports"
BOOKKEEPING = {"TODO.md", "NOTES.md", "TASKLOG.md"}   # collapsed: the task list and the agent's notes
MAX_LINES = 1500        # per file; the rest is summarised
BIG = 2000              # files with more changed lines than this start collapsed
SECRET = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})")


def git(pdir, *args):
    return subprocess.run(["git", "-C", str(pdir), *args], capture_output=True, text=True, errors="replace").stdout


def exists(pdir, rev):
    return bool(rev) and subprocess.run(["git", "-C", str(pdir), "cat-file", "-e", f"{rev}^{{commit}}"],
                                        capture_output=True).returncode == 0


def parse(diff):
    """Unified diff → [{path, old, status, add, del, binary, hunks: [[(kind, old_no, new_no, text)]]}]."""
    files, f, o, n = [], None, 0, 0
    for line in diff.splitlines():
        if line.startswith("diff --git ") or line.startswith("diff --cc ") or line.startswith("diff --combined "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            f = {"path": m.group(2) if m else line.split(" ", 2)[-1], "old": m.group(1) if m else None,
                 "status": "modified", "add": 0, "del": 0, "binary": False, "hunks": []}
            files.append(f)
        elif f is None:
            continue
        elif line.startswith("new file"):
            f["status"] = "added"
        elif line.startswith("deleted file"):
            f["status"] = "deleted"
        elif line.startswith("rename to "):
            f["status"] = "renamed"
        elif line.startswith("Binary files"):
            f["binary"] = True
        elif line.startswith("@@"):
            m = re.match(r"@@+ -(\d+)(?:,\d+)? .*?\+(\d+)", line)
            o, n = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            f["hunks"].append([("hunk", None, None, line)])
        elif f["hunks"] and line[:1] in "+- \\":
            k = line[:1]
            if k == "+":
                f["add"] += 1
                f["hunks"][-1].append(("add", None, n, line[1:])); n += 1
            elif k == "-":
                f["del"] += 1
                f["hunks"][-1].append(("del", o, None, line[1:])); o += 1
            elif k == " ":
                f["hunks"][-1].append(("ctx", o, n, line[1:])); o += 1; n += 1
            else:
                f["hunks"][-1].append(("meta", None, None, line))
    return files


def render_file(f, anchor):
    stat = f'<span class="plus">+{f["add"]}</span> <span class="minus">−{f["del"]}</span>'
    tag = "" if f["status"] == "modified" else f' <span class="st">{f["status"]}</span>'
    name = html.escape(f["path"])
    if f["status"] == "renamed" and f["old"]:
        name = f'{html.escape(f["old"])} → {name}'
    changed = f["add"] + f["del"]
    open_ = "" if f["path"] in BOOKKEEPING or changed > BIG else " open"
    if f["binary"]:
        body = '<div class="note">binary file</div>'
    else:
        rows, shown = [], 0
        for h in f["hunks"]:
            for kind, o, n, text in h:
                if shown >= MAX_LINES:
                    break
                shown += 1
                text = html.escape(SECRET.sub("[redacted]", text))
                if kind == "hunk":
                    rows.append(f'<tr class="hunk"><td></td><td></td><td>{text}</td></tr>')
                else:
                    rows.append(f'<tr class="{kind}"><td>{o or ""}</td><td>{n or ""}</td><td>{text or " "}</td></tr>')
        total = sum(len(h) for h in f["hunks"])
        more = f'<div class="note">… {total - shown} more lines not shown</div>' if total > shown else ""
        body = f'<div class="dwrap"><table class="diff">{"".join(rows)}</table></div>{more}'
    return (f'<details class="file" id="{anchor}"{open_}><summary><span class="fn mono">{name}</span>{tag}'
            f'<span class="fstat">{stat}</span></summary>{body}</details>')


def worktree_diff(wt, base):
    """base → the worktree as it is now, uncommitted and new files included. Uses a throwaway index so
    the agent's own index is never touched."""
    with tempfile.TemporaryDirectory() as d:
        env = {**__import__("os").environ, "GIT_INDEX_FILE": f"{d}/index"}
        run = lambda *a: subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, errors="replace",
                                        env=env).stdout
        run("read-tree", "HEAD")
        run("add", "-A")
        return parse(run("diff", "--cached", "-M", "--no-color", base))


def sections(pdir, base, head):
    """[(heading or None, files)] for base..head."""
    revs = git(pdir, "rev-list", "--reverse", "--first-parent", f"{base}..{head}").split()
    merges = {r for r in git(pdir, "rev-list", "--merges", "--first-parent", f"{base}..{head}").split()}
    if not merges:
        return [(None, parse(git(pdir, "diff", "-M", "--no-color", base, head)))]
    out = []
    for r in revs:
        subj = git(pdir, "log", "-1", "--format=%h %s", r).strip()
        if r in merges:
            d = git(pdir, "show", "--remerge-diff", "--format=", "--no-color", r)
            files = parse(d)
            out.append((f"{subj} — conflict resolution" if files else f"{subj} — merged cleanly, nothing to resolve", files))
        else:
            out.append((subj, parse(git(pdir, "show", "-M", "--format=", "--no-color", r))))
    return out


def build(start, end, run_page=None):
    """Write the page for one run; returns {'path', 'files', 'add', 'del'} or None when there is nothing to show."""
    project, run = start.get("project"), start.get("run")
    pdir = ROOT / "projects" / (project or "")
    base, head = start.get("base"), (end or {}).get("head")
    live = False
    if not head and start.get("branch"):     # still running (or killed before it ended): its branch so far
        head, live = git(pdir, "rev-parse", "-q", "--verify", f"refs/heads/{start['branch']}").strip(), True
    if not (run and exists(pdir, base) and exists(pdir, head)):
        return None
    wt = Path(start.get("workdir") or "/nonexistent")
    if live and (wt / ".git").exists():
        secs = [(None, worktree_diff(wt, base))]
    else:
        secs = sections(pdir, base, head) if base != head else []
    files = [f for _, fs in secs for f in fs]
    add, dele = sum(f["add"] for f in files), sum(f["del"] for f in files)
    commits = git(pdir, "log", "--first-parent", "--format=%h%x09%s%x09%b%x1e", f"{base}..{head}").split("\x1e")
    commits = [c.strip().split("\t", 2) for c in commits if c.strip()]

    title, spec = task_title(start.get("task") or "", project) if start.get("task") else ("(no task)", None)
    if start.get("merge_fix"):
        title = "Merge fix: " + title
    label, cls, why = result(start, end) if end else ("… running", "", "")
    # File list: code first, then the bookkeeping files.
    order = sorted({f["path"]: f for f in files}.values(), key=lambda f: (f["path"] in BOOKKEEPING, f["path"]))
    ids = {id(f): f"f{i}" for i, f in enumerate(files)}
    top = max((f["add"] + f["del"] for f in order), default=1) or 1
    flist = "".join(
        f'<tr><td class="mono"><a href="#{ids[id(f)]}">{html.escape(f["path"])}</a>'
        f'{"" if f["status"] == "modified" else " <span class=st>" + f["status"] + "</span>"}</td>'
        f'<td class="num"><span class="plus">+{f["add"]}</span></td><td class="num"><span class="minus">−{f["del"]}</span></td>'
        f'<td style="width:160px"><div class="ab"><i class="a" style="width:{100 * f["add"] / top:.1f}%"></i>'
        f'<i class="d" style="width:{100 * f["del"] / top:.1f}%"></i></div></td></tr>' for f in order)
    clist = "".join(f'<li><span class="mono">{html.escape(h)}</span> {html.escape(s)}'
                    + (f'<div class="note pre">{html.escape(b.strip())}</div>' if b.strip() else "") + "</li>"
                    for h, s, *rest in commits for b in [rest[0] if rest else ""])
    body = []
    for heading, fs in secs:
        if heading:
            body.append(f'<h2 class="sec mono">{html.escape(heading)}</h2>')
        fs = sorted(fs, key=lambda f: (f["path"] in BOOKKEEPING, f["path"]))
        body.extend(render_file(f, ids[id(f)]) for f in fs)
    back = f'<a href="../{Path(run_page).name}">← run report</a> · ' if run_page else ""
    page = PAGE.format(
        title=html.escape(title[:80]), h1=html.escape(title), css=reportui.CSS + CSS,
        spec=f'<div class="sub">{html.escape(spec)}</div>' if spec else "",
        nav=f'{back}<a href="../../index.html">all runs</a>',
        meta=" · ".join(html.escape(x) for x in (
            f'{start.get("worker") or "main"}', f'{base[:9]}..{head[:9]}', start.get("branch") or "",
            "in progress: its work so far, uncommitted changes included" if live else "") if x),
        result=f'<span class="{cls}"><b>{html.escape(label)}</b></span> <span class="note">{html.escape(why)}</span>',
        stats=f'{len(files)} file{"s" if len(files) != 1 else ""} · <span class="plus">+{add}</span> '
              f'<span class="minus">−{dele}</span> · {len(commits)} commit{"s" if len(commits) != 1 else ""}',
        files=f'<table>{flist}</table>' if flist else '<div class="note">no file changes</div>',
        commits=f'<ul class="commits">{clist}</ul>' if clist else '<div class="note">no commits</div>',
        body="\n".join(body))
    out = REPORTS / project / "diffs" / f"{run}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    return {"path": f"{project}/diffs/{run}.html", "files": len(files), "add": add, "del": dele, "live": live}


def live_runs(runs=None):
    """Runs with no end that are their agent's latest (an older endless run was killed)."""
    runs = runs if runs is not None else harness_runs(0)
    latest = {}
    for r in runs:
        latest[(r["start"].get("project"), r["start"].get("worker"))] = r
    return [r for r in latest.values() if not r["end"]]


def run_pages():
    """{run id: its report page path} (end report preferred), from the reports' summary files."""
    pages = {}
    for f in REPORTS.glob("*/*.json"):
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if s.get("run") and (s.get("trigger") == "end" or s["run"] not in pages):
            pages[s["run"]] = f.with_suffix(".html")
    return pages


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    force = "--force" in args
    pages = run_pages()
    runs = harness_runs(0)
    if "--live" in args:
        runs, force = live_runs(runs), True
    elif "--all" not in args:
        runs = [r for r in runs if r["start"].get("run") in args]
    n = 0
    for r in runs:
        s, e = r["start"], r["end"]
        out = REPORTS / (s.get("project") or "") / "diffs" / f"{s.get('run')}.html"
        if out.exists() and e and not force:
            continue
        info = build(s, e, pages.get(s.get("run")))
        if info:
            n += 1
            print(f'{info["path"]}: {info["files"]} files +{info["add"]} −{info["del"]}')
    print(f"{n} diff pages written")
    if "--live" not in args:   # reportsrv rebuilds the index itself
        import ctxreport
        ctxreport.build_index()


CSS = """
.plus{color:var(--c3);font-variant-numeric:tabular-nums}.minus{color:var(--crit);font-variant-numeric:tabular-nums}
.st{font-size:11px;color:var(--ink2);border:1px solid var(--ring);border-radius:5px;padding:0 5px;margin-left:6px}
.ab{display:flex;height:8px;gap:1px}.ab i{display:block;height:8px;border-radius:2px}.ab .a{background:var(--c3)}.ab .d{background:var(--crit)}
details.file{background:var(--surface);border:1px solid var(--ring);border-radius:10px;margin:10px 0;overflow:hidden}
details.file>summary{padding:8px 12px;display:flex;gap:10px;align-items:center;position:sticky;top:0;background:var(--surface);
 border-bottom:1px solid var(--grid);color:var(--ink)}
.fn{font-weight:600;overflow-wrap:anywhere}.fstat{margin-left:auto;white-space:nowrap;font-size:12px}
.dwrap{overflow-x:auto}table.diff{font:12px/1.5 ui-monospace,Menlo,monospace;border-collapse:collapse;width:100%}
table.diff td{border:0;padding:0 8px;white-space:pre;vertical-align:top}
table.diff td:nth-child(-n+2){color:var(--muted);text-align:right;user-select:none;width:1%;padding:0 6px}
table.diff tr.add{background:color-mix(in srgb,var(--c3) 16%,transparent)}
table.diff tr.del{background:color-mix(in srgb,var(--crit) 14%,transparent)}
table.diff tr.hunk td{color:var(--c1);background:color-mix(in srgb,var(--c1) 8%,transparent);padding:3px 8px}
table.diff tr.meta td{color:var(--muted)}
h2.sec{margin:22px 0 4px;font-size:13px}.pre{white-space:pre-wrap;margin:2px 0 6px}
ul.commits li{margin:3px 0}
"""

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · changes</title>
<style>{css}</style></head><body><main>
<div class="note">{nav}</div>
<h1>{h1}</h1>{spec}
<div class="sub" style="margin-top:6px">{result}</div>
<div class="sub">{meta}</div>
<div class="grid2" style="margin-top:14px">
<section class="card"><h2>Files</h2><div class="note">{stats}</div>{files}</section>
<section class="card"><h2>Commits</h2>{commits}</section></div>
<div class="note" style="margin-top:14px">TODO.md, NOTES.md and TASKLOG.md start collapsed ·
<a href="#" onclick="document.querySelectorAll('details.file').forEach(d=>d.open=true);return false">expand all</a> ·
<a href="#" onclick="document.querySelectorAll('details.file').forEach(d=>d.open=false);return false">collapse all</a></div>
{body}
</main></body></html>"""

if __name__ == "__main__":
    main()
