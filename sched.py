#!/usr/bin/env python3
"""Task scheduler for ./agent workers: picks a TODO.md task, claims it, and builds the prompt.

  sched.py next    STATE WORKER DIR [--ref REF]   claim the next task; prints JSON (exit codes below)
  sched.py release STATE WORKER                   drop WORKER's claim
  sched.py status  STATE DIR [--ref REF]          workers, claims and what each open task waits on
  sched.py note    STATE AKEY TEXT                 remember why an attempt at a task failed (next prompt)
  sched.py lastrun LOG                            the log's last run if it never ended (killed, crashed):
                                                  JSON {task, src, parent, key, sid}; exit 1 if it ended

An interrupted run leaves run/<project>/workers/WORKER.resume (its claim plus a short handoff from the
killed session). WORKER's next `next` takes that task again first, with the handoff in the prompt.

STATE is run/<project>/, DIR the directory holding the task files (the project, or a worktree).
With --ref, task files are read from that git ref (the integration branch) instead of DIR.

Exit codes of `next`: 0 task claimed, 10 no open tasks, 11 all runnable tasks are claimed or wait on
claimed ones (retry later), 12 the remaining tasks wait on tasks that were given up.

Dependencies (TODO.md or checklist lines):
  (id: NAME)        names a task
  (after: A, B)     runs once tasks A and B are checked (or gone); "(after: -)" = no dependencies
  no (after: ...)   runs once every open task above it is checked: the file is worked top-down,
                    exactly like a single agent
A milestone line ending in "(checklist: PATH)" is claimed as a whole; its steps run one at a time.
"""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LINE = re.compile(r"^\s*- \[( |x|X)\] (.*)$")
ID = re.compile(r"\(id: *([\w.-]+)\)")
AFTER = re.compile(r"\(after: *([^)]*)\)")
CHECKLIST = re.compile(r"\(checklist: ([^) ]+)\)")


TITLE = re.compile(r"^\*\*(.+?)\*\*\s*(?:—\s*)?(.*)$")
TAGS = re.compile(r"\s*\((?:id|after|checklist): *[^)]*\)")


def split_title(text):
    """(title, spec) of a task line: `**Title** — spec (tags)`. Older lines without a title get a short
    head (up to the first colon, else ~70 characters) as title."""
    text = TAGS.sub("", text or "").strip()
    m = TITLE.match(text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    head = text.split(": ", 1)[0]
    if len(head) > 70:
        head = text[:70].rsplit(" ", 1)[0] + "…"
    return head, text


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def key(text):
    return hashlib.sha1(text.strip().encode()).hexdigest()[:12]


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


class Files:
    """Reads task files from a directory or from a git ref."""

    def __init__(self, d, ref=None):
        self.d, self.ref = Path(d), ref

    def read(self, path):
        if self.ref:
            r = subprocess.run(["git", "-C", str(self.d), "show", f"{self.ref}:{path}"],
                               capture_output=True, text=True)
            return r.stdout if r.returncode == 0 else None
        p = self.d / path
        return p.read_text() if p.is_file() else None


def tasks(text):
    """[(line_no, done, text)] for every checkbox line."""
    out = []
    for i, line in enumerate((text or "").splitlines(), 1):
        m = LINE.match(line)
        if m:
            out.append((i, m.group(1) != " ", m.group(2)))
    return out


class State:
    def __init__(self, d):
        self.d = Path(d)
        (self.d / "workers").mkdir(parents=True, exist_ok=True)

    @contextmanager
    def lock(self):
        with open(self.d / "lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def load(self, name, default):
        try:
            return json.loads((self.d / name).read_text())
        except (OSError, ValueError):
            return default

    def save(self, name, data):
        dst = self.d / name
        tmp = dst.with_name(f".{dst.name}.tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(dst)

    def claims(self):
        """{claim key: claim} for workers that are still alive."""
        out = {}
        for f in (self.d / "workers").glob("*.json"):
            c = self.load(f"workers/{f.name}", {})
            if c.get("key") and alive(c.get("pid")):
                out[c["key"]] = c
        return out


def plan(files, claims, failed):
    """Classify each open TODO.md line: [(entry, state, reason)], state in ready / claimed / waiting /
    failed / blocked-failed."""
    todo = tasks(files.read("TODO.md"))
    all_ids = {m.group(1) for _, _, t in todo for m in [ID.search(t)] if m}
    opened = [(ln, t) for ln, done, t in todo if not done]
    tid = lambda t: (ID.search(t) or [None, None])[1]
    out, state_of = [], {}
    for i, (ln, text) in enumerate(opened):
        k = key(text)
        m = AFTER.search(text)
        if m:
            deps = [d for d in re.split(r"[,\s]+", m.group(1).strip()) if d and d != "-"]
            unknown = [d for d in deps if d not in all_ids]
            blockers = [t for _, t in opened if tid(t) in deps and t != text]
        else:
            unknown, blockers = [], [t for _, t in opened[:i]]
        entry = {"line": ln, "text": text, "key": k, "unknown": unknown}
        dead = lambda t: state_of.get(key(t)) in ("failed", "blocked-failed")
        if k in failed:
            state, why = "failed", failed[k]
        elif k in claims:
            state, why = "claimed", claims[k]["worker"]
        elif blockers:
            state = "blocked-failed" if all(dead(t) for t in blockers) else "waiting"
            why = next((t for t in blockers if not dead(t)), blockers[0])
        else:
            state, why = "ready", ""
        state_of[k] = state
        out.append((entry, state, why))
    return out


def where(files, job):
    """The breadcrumb (goal → sections → task, each with its why) for a job, as plain dicts. A checklist
    step gets the TODO.md path to its milestone, then the checklist's own sections."""
    try:
        from plan import Plan   # pydantic (harness .venv)
    except ImportError:
        return []
    todo = Plan.parse(files.read("TODO.md"))
    if not job["parent"]:
        return [s.model_dump() for s in todo.path(job["line"])]
    ms = next((ln for ln, _, t in tasks(files.read("TODO.md")) if t == job["parent"]), None)
    head = [s.model_copy(update={"kind": "milestone"}) if s.kind == "task" else s for s in (todo.path(ms) if ms else [])]
    steps = Plan.parse(files.read(job["src"])).path(job["line"])
    steps = [s for s in steps if s.kind != "goal"]   # the checklist's title line is the milestone itself
    return [s.model_dump() for s in head] + [dict(s.model_dump(), kind="section" if s.kind == "task" else s.kind)
                                             for s in steps[:-1]] + [s.model_dump() for s in steps[-1:]]


def resolve(files, entry):
    """Expand a milestone to its current checklist step."""
    text, src, parent = entry["text"], "TODO.md", None
    m = CHECKLIST.search(text)
    cl = m.group(1) if m else None
    if cl:
        body = files.read(cl)
        steps = [(ln, t) for ln, done, t in tasks(body) if not done]
        if steps:
            # the checklist's `# ` title names its instance (repo, issue): templates reuse step texts,
            # so retry counts and debriefs must not carry over from one issue's checklist to the next
            inst = next((ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")), "")
            return {"task": steps[0][1], "src": cl, "inst": inst, "line": steps[0][0], "parent": text, "cl": cl,
                    "next": [t for _, t in steps[1:3]]}
    return {"task": text, "src": src, "line": entry["line"], "parent": parent, "cl": cl, "next": None}


def prompt(files, job, attempts, step_abandon, others, resume=None, ref=None):
    p = (ROOT / "prompts" / "loop.md").read_text().rstrip()
    if job.get("path"):
        from plan import Step, render
        p += ("\n\n## Where this fits\nEach level says how it serves the one above. Aim the work at your task's "
              "why, not only at its checkbox; if the task as written would not serve it, say so in NOTES.md.\n"
              + render([Step(**s) for s in job["path"]]))
    p += f"\n\n## Your task ({job['src']} line {job['line']})\n{job['task']}"
    if job["cl"] and not job["parent"]:
        p += (f"\n\nThis is a milestone line and {job['cl']} does not exist or has no open steps. Do what the "
              "line says for that case. Check the milestone only when the line tells you to; after creating a "
              "checklist, leave it unchecked.")
    elif job["parent"]:
        p += (f"\n\nThis is one step of the checklist {job['src']}, under this TODO.md milestone (leave the "
              f"milestone unchecked unless the step says to check it):\n{job['parent']}\n"
              f"Read the Rules and On failure sections at the top of {job['src']} first.")
    nxt = job["next"] if job["next"] is not None else job.get("todo_next", [])
    if nxt:
        p += "\n\n## Coming next (for context only, do not do these)\n" + "\n".join(f"- [ ] {t}" for t in nxt)
    if others:
        p += ("\n\n## Other agents working in parallel (separate git worktrees; do not do their tasks, and "
              "avoid editing the files they will need)\n" + "\n".join(f"- {w}: {t}" for w, t in others))
    if ref:   # parallel agents: take in the others' work now, while this change is still in context
        p += (f"\n\n## Before your final message\nAfter your commit, run `git merge {ref}` to take in what other "
              "agents merged meanwhile. If it conflicts, resolve it so both sides' changes survive, re-run the "
              "tests, and commit the merge. (Doing it now saves a separate merge-fix run later.)")
    if resume:
        p += ("\n\n## Continue an interrupted run\nA previous run on this task was stopped before it finished. "
              + (f"Its work is already committed on your branch (`git log {ref}..HEAD` and `git diff {ref}...HEAD` "
                 "show it)" if ref else "Its work is already committed (see `git log -3` and `git show`)")
              + "; it may be incomplete or untested. Its handoff note:\n"
              + "\n".join(f"> {l}" for l in (resume.get("handoff") or "(none: the run could not write one)").splitlines())
              + "\nCheck what is there, then finish the task.")
    elif attempts > 1:
        p += (f"\n\nThis is attempt {attempts} at this task; earlier attempts did not finish it. Read NOTES.md "
              "for what was tried and why it failed, and try a different approach. Record this attempt's "
              "outcome in NOTES.md.")

    if job.get("debrief") and not resume:
        p += ("\n\nAn earlier attempt at this task ended without finishing it. Its own account (why, and what "
              "would have helped):\n" + "\n".join(f"> {l}" for l in job["debrief"].splitlines()))
    if step_abandon:
        p += (f"\n\nThis step has now failed {attempts - 1} times. Do not attempt it again: follow the On "
              f"failure section of {job['src']} (use reason STEP_FAILED if no other reason fits), then stop.")
    return p


def cmd_next(st, worker, d, ref):
    files = Files(d, ref)
    max_task, max_step = env_int("MAX_TASK_ATTEMPTS", 0), env_int("MAX_STEP_ATTEMPTS", 0)
    with st.lock():
        claims = {k: c for k, c in st.claims().items() if c["worker"] != worker}
        failed = st.load("failed.json", {})
        attempts = st.load("attempts.json", {})
        rows = plan(files, claims, failed)
        if not rows:
            return 10, {"reason": "no open tasks"}
        todo_open = [e["text"] for e, _, _ in rows]
        rpath = st.d / "workers" / f"{worker}.resume"
        resume = st.load(f"workers/{worker}.resume", None)
        rpath.unlink(missing_ok=True)
        if resume:   # the interrupted task goes first, if it is still open and nobody else took it
            rows = sorted(rows, key=lambda r: r[0]["key"] != resume.get("key"))
            if rows[0][0]["key"] != resume.get("key") or rows[0][1] not in ("ready", "waiting"):
                resume = None
        for entry, state, why in rows:
            if state != "ready" and not (resume and entry["key"] == resume["key"]):
                continue
            job = resolve(files, entry)
            akey = key(job["src"] + "\0" + job.get("inst", "") + "\0" + job["task"])
            n = attempts.get(akey, 0) + 1
            give_up = (max_task and n > max_task) or (job["parent"] and max_step and n > max_step + 2)
            if give_up:
                failed[entry["key"]] = f"gave up after {n - 1} attempts: {job['task'][:120]}"
                st.save("failed.json", failed)
                print(f"✖ gave up after {n - 1} attempts on: {job['task']}", file=sys.stderr)
                continue
            attempts[akey] = n
            st.save("attempts.json", attempts)
            job["debrief"] = st.load("debriefs.json", {}).get(akey)
            job["path"] = where(files, job)
            if job["next"] is None:
                i = todo_open.index(entry["text"])
                job["todo_next"] = todo_open[i + 1:i + 3]
            others = [(c["worker"], c["task"]) for c in claims.values()]
            job.update(title=split_title(job["task"])[0], key=entry["key"], akey=akey, attempts=n, open=len(rows), unknown=entry["unknown"],
                       step_abandon=bool(job["parent"] and max_step and n > max_step),
                       resumed=bool(resume and entry["key"] == resume["key"]),
                       prompt=prompt(files, job, n, bool(job["parent"] and max_step and n > max_step), others,
                                     resume if resume and entry["key"] == resume["key"] else None, ref))
            st.save(f"workers/{worker}.json", {"worker": worker, "pid": int(os.environ.get("AGENT_PID") or os.getppid()), "key": entry["key"],
                                               "task": job["task"], "since": time.time()})
            return 0, job
        if claims:   # something is running: its merge may unblock the rest
            waiting = [(e["text"], why) for e, s, why in rows if s in ("claimed", "waiting")]
            return 11, {"reason": "waiting", "open": len(rows),
                        "claimed": [(c["worker"], c["task"]) for c in claims.values()], "first_wait": waiting[:1]}
        return 12, {"reason": "blocked by given-up tasks", "failed": list(failed.values())}


def cmd_lastrun(log):
    """The last run in LOG if it has a harness start but no end (the worker was killed mid-run)."""
    run, sid = None, None
    try:
        lines = open(log, errors="replace").read().splitlines()
    except OSError:
        return 1, {}
    for line in lines:
        if line.startswith('{"type":"harness"'):
            ev = json.loads(line)
            run, sid = (ev, None) if ev.get("event") == "start" else (None, None)
        elif run and sid is None and '"subtype":"init"' in line:
            sid = json.loads(line).get("session_id")
    if not run:
        return 1, {}
    todo_line = run.get("parent") or run.get("task") or ""
    return 0, {"task": run.get("task"), "src": run.get("src"), "parent": run.get("parent"),
               "key": key(todo_line), "sid": sid, "rid": run.get("run")}


def cmd_status(st, d, ref):
    files = Files(d, ref)
    claims = st.claims()
    failed = st.load("failed.json", {})
    for f in sorted((st.d / "workers").glob("*.json")):
        c = st.load(f"workers/{f.name}", {})
        up = alive(c.get("pid"))
        mins = (time.time() - c.get("since", time.time())) / 60
        print(f"{c.get('worker', f.stem):6} {'running' if up else 'gone':8} {mins:5.0f} min  {split_title(c.get('task', ''))[0][:100]}")
    for f in sorted((st.d / "workers").glob("*.resume")):
        r = st.load(f"workers/{f.name}", {})
        print(f"{f.stem:6} {'paused':8}            {split_title(r.get('task', ''))[0][:100]}\n{'':25}handoff: {(r.get('handoff') or '-')[:300]}")
    rows = plan(files, claims, failed)
    print(f"\n{len(rows)} open tasks")
    for entry, state, why in rows[:15]:
        extra = f"  ← {split_title(why)[0][:50]}" if why and state not in ("ready", "claimed") else f" ({why})" if why else ""
        print(f"  {state:14} L{entry['line']:<4} {split_title(entry['text'])[0][:70]}{extra}")


def main():
    a = sys.argv[1:]
    ref = None
    if "--ref" in a:
        i = a.index("--ref")
        ref = a[i + 1]
        del a[i:i + 2]
    if a and a[0] == "lastrun":
        code, out = cmd_lastrun(a[1])
        print(json.dumps(out))
        sys.exit(code)
    cmd, st = a[0], State(a[1])
    if cmd == "next":
        code, out = cmd_next(st, a[2], a[3], ref)
        print(json.dumps(out))
        sys.exit(code)
    if cmd == "note":
        with st.lock():
            notes = st.load("debriefs.json", {})
            if a[3].strip():
                notes[a[2]] = a[3].strip()
                st.save("debriefs.json", notes)
        return
    if cmd == "release":
        with st.lock():
            (st.d / "workers" / f"{a[2]}.json").unlink(missing_ok=True)
    elif cmd == "status":
        cmd_status(st, a[2], ref)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
