#!/usr/bin/env python3
"""A/B trials: the same task, run by agents that differ in one or more factors, compared on hidden checks
and a blind pairwise judge.

  ./agent ab new    EXP                 scaffold ab/EXP/spec.toml (edit it: tasks, checks, variants)
  ./agent ab run    EXP [--now] [--slots N] [--only GLOB]
                                        run every missing trial; resumable. Default: a trial starts only in a
                                        model slot no loop agent is using (the pool counts trials as busy, so
                                        nothing is oversubscribed). --now: ignore loop agents.
  ./agent ab status EXP                 trials done / running / stale, per variant
  ./agent ab judge  EXP [--samples K]   blind pairwise verdicts by Claude (`claude -p`, both orders, cached)
  ./agent ab report EXP                 reports/ab/EXP.html + one page per trial (also rebuilt after each trial)
  ./agent ab clean  EXP                 remove the trials' worktrees (branches ab/EXP/* keep every diff)
  ./agent ab import polyglot-python EXP [--repeats N]   a public benchmark as an experiment (lib/abbench.py)

Variant `verify = {rounds, runs, feedback}`: after the agent stops, the task's check runs `runs` times in a
fresh checkout of its committed work ("phase 2"); on failure the same session is resumed with the output
(feedback "check") or a bare "not accepted yet" (feedback "generic", the control), up to `rounds` times.
Every trial runs phase 2 once as a shadow, so the rate of false "done"s is measured for every variant.

Reproducibility: the first run pins the base commit, harness revision and model-server config into
ab/EXP/pinned.json. Each trial records the hash of its task + variant; editing either marks that trial
stale (excluded from stats; `run` redoes it). Run order is interleaved by a seeded shuffle, so time-of-day
or server-load drift hits every variant alike. Judge verdicts are cached by prompt hash, so re-running
the report never changes a score. Agent runs themselves are not bit-reproducible (parallel slots, tool
timing): use repeats and read the confidence intervals.
"""
import fnmatch
import hashlib
import html
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diffpage  # noqa: E402
import reportui  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
AB, RUN, LOGS, REPORTS = ROOT / "ab", ROOT / "run" / "ab", ROOT / "logs" / "ab", ROOT / "reports" / "ab"
WORK = ROOT / "work" / "ab"
POLL = 20
GITLOCK = threading.Lock()   # parallel trials add/remove worktrees of one repo
ENV_KNOBS = {"NUM_CTX", "CTX_MARGIN", "MAX_TURNS", "ITER_TIMEOUT", "AGENT_READ_MAX_LINES", "AGENT_READ_MAX_CHARS"}
BOOKKEEPING = sorted(diffpage.BOOKKEEPING)
JUDGE_MODEL = "claude-opus-5-5"
JUDGE_MAX_DIFF = 150_000   # characters per side; longer diffs are cut and the judge is told so

TEMPLATE = '''# A/B experiment. Every task runs once per variant per repeat, from the same pinned commit.
project = "{project}"
base = "HEAD"          # pinned to a commit on the first run
repeats = 3            # per task per variant; see the report's "how many trials" note
seed = 1               # run order shuffle
timeout = 1800         # seconds per trial (ITER_TIMEOUT); a timeout counts as a failed check
max_turns = 150
# Gitignored paths from the project: `shared` are bind-mounted read-only (venvs, browsers), `copy` are
# copied into each trial (databases the task may write). Default shared = the project's .agent-shared.
# shared = [".venv"]
# copy = []

[judge]
rubric = """Correctness: does the change do what the task asks, and would it work as written?
Scope: everything the task asks, nothing unrelated; no disabled or special-cased tests.
Quality: readable, matches the surrounding code, no needless complexity.
Verification: sensible tests for the change."""

[[task]]
id = "example"
prompt = """Describe the task exactly as you would give it to an agent."""
# Acceptance check, run in the trial's tree after the agent finishes; exit 0 = pass.
check = "python -m pytest -q tests"
# Files the agent never sees, copied in only for the check (SWE-bench style hidden tests).
# hidden = "ab/{exp}/hidden/example"

[[variant]]
name = "A"
# Every field is optional; a variant with none is the plain harness.

[[variant]]
name = "B"
# context = "Extra text put before the task prompt."
# template = "ab/{exp}/prompt.md"            # file with {{task}} and optionally {{context}}
# system = "ab/{exp}/system.md"              # replaces prompts/system.md (appended system prompt)
# files = {{ "CLAUDE.md" = "ab/{exp}/CLAUDE.short.md" }}   # overlaid before the run
# remove = ["NOTES.md"]
# env = {{ NUM_CTX = "65536", AGENT_READ_MAX_LINES = "120" }}
# sampling = {{ temperature = 0.6, top_p = 0.95 }}          # merged into every model request
'''


# ── helpers ────────────────────────────────────────────────────────────────────

def sh(*a, cwd=None, check=True, **kw):
    r = subprocess.run(a, cwd=cwd, capture_output=True, text=True, errors="replace", **kw)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(map(str, a))}: {r.stderr.strip()[:400]}")
    return r.stdout


def h(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:12]


def jload(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return default


def jsave(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str))
    tmp.replace(p)


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def config_env():
    out = {}
    for line in (ROOT / "config.env").read_text().splitlines():
        m = re.match(r'^([A-Z_]+)=(?:"\$\{\w+:-(.*)\}"|"(.*)"|(\S*))', line)
        if m:
            out[m.group(1)] = next(g for g in m.groups()[1:] if g is not None)
    return out


def wilson(k, n, z=1.96):
    if not n:
        return None, None
    p, d = k / n, 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    w = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - w), min(1.0, c + w)


def binom_p(k, n):
    """Two-sided exact binomial test against p = 0.5 (sign test / exact McNemar)."""
    if not n:
        return None
    pk = [math.comb(n, i) / 2 ** n for i in range(n + 1)]
    return min(1.0, sum(x for x in pk if x <= pk[k] * (1 + 1e-9)))


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


# ── experiment ─────────────────────────────────────────────────────────────────

class Exp:
    def __init__(self, name):
        self.name, self.d = name, AB / name
        if not (self.d / "spec.toml").exists():
            sys.exit(f"no ab/{name}/spec.toml (./agent ab new {name})")
        self.spec = tomllib.loads((self.d / "spec.toml").read_text())
        s = self.spec
        self.project = s["project"]
        self.pdir = ROOT / "projects" / self.project
        self.tasks = {t["id"]: t for t in s.get("task", [])}
        self.variants = {v["name"]: v for v in s.get("variant", [])}
        if not self.tasks or len(self.variants) < 2:
            sys.exit("spec needs at least one [[task]] and two [[variant]]s")
        for v in self.variants.values():
            bad = set(v.get("env", {})) - ENV_KNOBS
            if bad:
                sys.exit(f"variant {v['name']}: env {sorted(bad)} not in {sorted(ENV_KNOBS)}")
        self.repeats = int(s.get("repeats", 3))
        self.trials_d = self.d / "trials"

    # pinned once, on the first run
    def pinned(self):
        p = self.d / "pinned.json"
        pin = jload(p)
        if not pin:
            cfg = config_env()
            pin = {"base": sh("git", "-C", str(self.pdir), "rev-parse", self.spec.get("base", "HEAD")).strip(),
                   "harness": sh("git", "-C", str(ROOT), "rev-parse", "HEAD").strip(),
                   "model": cfg.get("MODEL"), "backend": cfg.get("BACKEND"), "llama_args": cfg.get("LLAMA_ARGS"),
                   "num_ctx": cfg.get("NUM_CTX"), "num_parallel": cfg.get("NUM_PARALLEL"), "pinned_at": time.time()}
            jsave(p, pin)
        return pin

    def key(self, tid, vname):
        return h({"task": self.tasks[tid], "variant": self.variants[vname],
                  "global": {k: self.spec.get(k) for k in ("timeout", "max_turns", "shared", "copy", "disallowed_tools",
                                                           "verify_runs")}})

    def order(self):
        """Every (task, variant, rep), interleaved: rep-major, tasks in spec order, variants shuffled per cell."""
        rng = random.Random(self.spec.get("seed", 1))
        out = []
        for r in range(1, self.repeats + 1):
            for tid in self.tasks:
                vs = list(self.variants)
                rng.shuffle(vs)
                out += [(tid, v, r) for v in vs]
        return out

    def tid(self, task, variant, rep):
        return f"{task}.{variant}.{rep}"

    def result(self, trial_id):
        return jload(self.trials_d / trial_id / "result.json")

    def state(self, task, variant, rep):
        tid = self.tid(task, variant, rep)
        pid = RUN / self.name / f"{tid}.pid"
        if pid.exists() and alive(pid.read_text().strip()):
            return "running", None
        r = self.result(tid)
        if not r:
            return "todo", None
        return ("done" if r.get("key") == self.key(task, variant) else "stale"), r


# ── running ────────────────────────────────────────────────────────────────────

def loop_busy():
    """Model-server users outside this module: loop workers holding a claim (any project)."""
    n = 0
    for pid in (ROOT / "run").glob("*/workers/*.pid"):
        if alive(pid.read_text().strip() or 0) and pid.with_suffix(".json").exists():
            n += 1
    return n


def trials_busy():
    return sum(1 for p in RUN.glob("*/*.pid") if alive(p.read_text().strip() or 0))


def setup_tree(exp, task, variant, rep, pin):
    """Fresh worktree at the pinned base on branch ab/EXP/ID, the variant's files applied and committed.
    Returns (worktree, setup commit)."""
    tid = exp.tid(task, variant, rep)
    wt, branch = WORK / exp.name / tid, f"ab/{exp.name}/{tid}"
    p = str(exp.pdir)
    GITLOCK.acquire()
    try:
        _fresh_tree(p, wt, branch, pin)
    finally:
        GITLOCK.release()
    return _apply_variant(exp, variant, wt)


def _fresh_tree(p, wt, branch, pin):
    if wt.exists():
        sh("git", "-C", p, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(wt, ignore_errors=True)
    sh("git", "-C", p, "worktree", "prune")
    sh("git", "-C", p, "branch", "-D", branch, check=False)
    wt.parent.mkdir(parents=True, exist_ok=True)
    sh("git", "-C", p, "worktree", "add", "-q", "-b", branch, str(wt), pin["base"])


def _apply_variant(exp, variant, wt):
    v = exp.variants[variant]
    for dst, src in v.get("files", {}).items():
        (wt / dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / src, wt / dst)
    for rm in v.get("remove", []):
        target = wt / rm
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    for c in exp.spec.get("copy", []):
        src = exp.pdir / c
        if src.is_dir():
            shutil.copytree(src, wt / c, dirs_exist_ok=True, symlinks=True)
        elif src.exists():
            shutil.copy2(src, wt / c)
    sh("git", "-C", str(wt), "add", "-A")
    sh("git", "-C", str(wt), "-c", "user.name=ab", "-c", "user.email=ab@sandbox", "commit", "-q", "--allow-empty",
       "-m", f"ab setup: variant {variant}")
    return wt, sh("git", "-C", str(wt), "rev-parse", "HEAD").strip()


def binds(exp, wt):
    shared = exp.spec.get("shared")
    if shared is None:
        f = exp.pdir / ".agent-shared"
        shared = [l.strip() for l in (f.read_text().splitlines() if f.exists() else [])
                  if l.strip() and not l.startswith("#")]
    copied = set(exp.spec.get("copy", []))
    out = [["--bind", str(exp.pdir / ".git"), str(exp.pdir / ".git")]]   # git inside the worktree
    for s in shared:
        if s not in copied and (exp.pdir / s).exists():
            out.append(["--ro-bind", str(exp.pdir / s), f"/work/{s}"])
    return out


def prompt_for(exp, task, variant):
    t, v = exp.tasks[task], exp.variants[variant]
    ctx = v.get("context", "")
    if v.get("template"):
        return (ROOT / v["template"]).read_text().replace("{task}", t["prompt"]).replace("{context}", ctx)
    return (ctx.strip() + "\n\n" + t["prompt"]).strip() if ctx else t["prompt"]


def log_metrics(log):
    """From the stream-json log (every round of the trial): summed result-event numbers, tool calls, the final
    message and the latest session id (what a feedback round resumes)."""
    m = {"tools": 0, "tool_counts": {}, "final": "", "result_events": 0, "turns": 0, "duration_ms": 0,
         "input_tokens": 0, "output_tokens": 0, "session": None}
    try:
        lines = Path(log).read_text(errors="replace").splitlines()
    except OSError:
        return m
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            m["session"] = ev.get("session_id")
        elif ev.get("type") == "assistant":
            for b in ev.get("message", {}).get("content", []) or []:
                if b.get("type") == "tool_use":
                    m["tools"] += 1
                    m["tool_counts"][b.get("name")] = m["tool_counts"].get(b.get("name"), 0) + 1
                elif b.get("type") == "text" and b.get("text", "").strip():
                    m["final"] = b["text"]
        elif ev.get("type") == "result":
            u = ev.get("usage") or {}
            m["result_events"] += 1
            m.update(subtype=ev.get("subtype"), is_error=ev.get("is_error"))
            m["turns"] += ev.get("num_turns") or 0
            m["duration_ms"] += ev.get("duration_ms") or 0
            m["input_tokens"] += sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                             "cache_creation_input_tokens"))
            m["output_tokens"] += u.get("output_tokens") or 0
            if ev.get("result"):
                m["final"] = ev["result"]
    return m


def commit_all(wt, msg):
    if sh("git", "-C", str(wt), "status", "--porcelain").strip():
        sh("git", "-C", str(wt), "add", "-A")
        sh("git", "-C", str(wt), "-c", "user.name=agent", "-c", "user.email=agent@sandbox", "commit", "-q", "-m", msg)
    return sh("git", "-C", str(wt), "rev-parse", "HEAD").strip()


def checked(exp, td, spec, head, cmd, hidden=None, name="check", runs=1):
    """Run CMD (sandboxed) `runs` times in a fresh checkout of HEAD: committed files only, so nothing the agent
    left uncommitted or gitignored counts. HIDDEN files are copied in first. Returns (all passed, output)."""
    fresh = WORK / exp.name / f"{td.name}.{name}"
    with GITLOCK:
        sh("git", "-C", str(exp.pdir), "worktree", "remove", "--force", str(fresh), check=False)
        shutil.rmtree(fresh, ignore_errors=True)
        sh("git", "-C", str(exp.pdir), "worktree", "add", "-q", "--detach", str(fresh), head)
    try:
        if hidden:
            shutil.copytree(ROOT / hidden, fresh, dirs_exist_ok=True)
        cs = td / f"{name}.spec.json"
        jsave(cs, {**spec, "workdir": str(fresh), "check": cmd})
        out, ok = [], True
        for i in range(runs):
            r = subprocess.run([str(ROOT / "agent"), "_abcheck", str(cs)], capture_output=True, text=True, errors="replace")
            out.append(f"── run {i + 1}: exit {r.returncode}\n{r.stdout}{r.stderr}")
            ok = ok and r.returncode == 0
        return ok, "\n".join(out)
    finally:
        with GITLOCK:
            sh("git", "-C", str(exp.pdir), "worktree", "remove", "--force", str(fresh), check=False)
            shutil.rmtree(fresh, ignore_errors=True)


FEEDBACK = {
    "check": ("An independent re-run of the acceptance check failed, so the task is not done yet.\n"
              "Check: `{cmd}`, run {runs}x in a fresh checkout of your committed work (uncommitted or gitignored "
              "files do not exist there).\nOutput (last {n} lines):\n```\n{out}\n```\n"
              "Fix the cause, run the check yourself, and commit. Do not change the tests to make them pass."),
    "generic": ("Your work was not accepted yet. Re-check that the task is completely and correctly done, "
                "fix anything that is not, and commit."),
}


def run_trial(exp, task, variant, rep, pin, contention):
    """One trial. Round 0: the agent works on the task. Then, for every variant, a shadow phase-2 check (the
    task's own check, `runs` times, fresh checkout) records whether the agent's "done" holds up. Variants with
    `verify` get feedback rounds while phase 2 fails (resuming the same session). Grading = the hidden check
    on the final commit; `passed_initial` = the hidden check on round 0's commit (same trial, before feedback)."""
    tid = exp.tid(task, variant, rep)
    td = exp.trials_d / tid
    shutil.rmtree(td, ignore_errors=True)
    td.mkdir(parents=True)
    (RUN / exp.name).mkdir(parents=True, exist_ok=True)
    pidf = RUN / exp.name / f"{tid}.pid"
    t0 = time.time()
    t = exp.tasks[task]
    res = {"id": tid, "task": task, "variant": variant, "rep": rep, "key": exp.key(task, variant), "base": pin["base"],
           "harness": sh("git", "-C", str(ROOT), "rev-parse", "HEAD").strip(), "started": t0, "rounds": 0}
    try:
        wt, setup = setup_tree(exp, task, variant, rep, pin)
        v = exp.variants[variant]
        log = LOGS / exp.name / f"{tid}.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.unlink(missing_ok=True)
        env = {"ITER_TIMEOUT": str(exp.spec.get("timeout", 1800)), "MAX_TURNS": str(exp.spec.get("max_turns", 150)),
               **{k: str(x) for k, x in v.get("env", {}).items()}}
        spec = {"workdir": str(wt), "prompt": prompt_for(exp, task, variant), "log": str(log),
                "run": f"ab-{exp.name}-{tid}", "tag": f"ab/{exp.name}/{tid}", "worker": f"ab-{variant}",
                "binds": binds(exp, wt), "env": env, "sampling": v.get("sampling", {}),
                "system": str(ROOT / v["system"]) if v.get("system") else None,
                "disallowed_tools": exp.spec.get("disallowed_tools", []),
                "check": t.get("check", "true"), "check_timeout": t.get("check_timeout", 900)}
        res.update(setup=setup, prompt=spec["prompt"], env=env, sampling=spec["sampling"], log=str(log))

        def agent_round(rspec, n):
            jsave(td / f"round{n}.spec.json", rspec)
            with open(td / "console.txt", "a") as con:
                con.write(f"\n━━ round {n} ━━\n")
                con.flush()
                p = subprocess.Popen([str(ROOT / "agent"), "_abrun", str(td / f"round{n}.spec.json")],
                                     stdout=con, stderr=subprocess.STDOUT)
                pidf.write_text(str(p.pid))
                return p.wait()

        rc = agent_round(spec, 0)
        res["run_rc"] = rc
        head = commit_all(wt, "ab: leftover changes")
        res["head_initial"] = head
        res["passed_initial"], _ = checked(exp, td, spec, head, spec["check"], t.get("hidden"), "grade0")
        vf = v.get("verify") or {}
        vcmd, runs = t.get("verify_check", spec["check"]), int(vf.get("runs", exp.spec.get("verify_runs", 2)))
        ok, out = checked(exp, td, spec, head, vcmd, None, "phase2", runs)
        res["phase2_first"] = ok
        (td / "phase2.txt").write_text(f"round 0: {'pass' if ok else 'FAIL'}\n{out}\n")
        while vf and not ok and res["rounds"] < int(vf.get("rounds", 2)):
            res["rounds"] += 1
            sid = log_metrics(log)["session"]
            tail = "\n".join(out.splitlines()[-80:])
            msg = FEEDBACK[vf.get("feedback", "check")].format(cmd=vcmd, runs=runs, n=80, out=tail)
            rc = agent_round({**spec, "prompt": msg, "resume": sid}, res["rounds"])
            head = commit_all(wt, f"ab: leftover changes (round {res['rounds']})")
            ok, out = checked(exp, td, spec, head, vcmd, None, "phase2", runs)
            with open(td / "phase2.txt", "a") as f:
                f.write(f"round {res['rounds']}: {'pass' if ok else 'FAIL'}\n{out}\n")
        res["phase2_final"] = ok
        res["wall_s"] = round(time.time() - t0, 1)
        excl = [f":(exclude){b}" for b in BOOKKEEPING]
        stat = sh("git", "-C", str(wt), "diff", "--numstat", setup, head, "--", ".", *excl)
        res.update(head=head, files=len(stat.splitlines()),
                   add=sum(int(a) for a, *_ in (l.split("\t") for l in stat.splitlines()) if a.isdigit()),
                   dele=sum(int(b) for _, b, *_ in (l.split("\t") for l in stat.splitlines()) if b.isdigit()))
        res.update({k: x for k, x in log_metrics(log).items() if k != "session"})
        c0 = time.time()
        ok, out = checked(exp, td, spec, head, spec["check"], t.get("hidden"), "grade")
        (td / "check.txt").write_text(out)
        res.update(passed=ok, check_rc=0 if ok else 1, check_s=round(time.time() - c0, 1))
        res["timed_out"] = rc in (124, 137) or res.get("subtype") == "error_max_turns"
    except Exception as e:   # recorded, not raised: one broken trial must not stop the experiment
        res.update(error=repr(e), passed=False)
    finally:
        pidf.unlink(missing_ok=True)
    c = contention.pop(tid, [])
    res.update(finished=time.time(), others_mean=round(sum(c) / len(c), 2) if c else 0, others_max=max(c, default=0))
    jsave(td / "result.json", res)
    return res


def cmd_run(exp, args):
    now = "--now" in args
    slots = int(args[args.index("--slots") + 1]) if "--slots" in args else int(os.environ.get("NUM_PARALLEL", 2))
    only = args[args.index("--only") + 1] if "--only" in args else None
    pin = exp.pinned()
    todo = [(t, v, r) for t, v, r in exp.order() if exp.state(t, v, r)[0] in ("todo", "stale")
            and (not only or fnmatch.fnmatch(exp.tid(t, v, r), only))]
    print(f"ab {exp.name}: {len(todo)} trials to run on {exp.project}@{pin['base'][:9]}, "
          f"{slots} slots{' (ignoring loop agents)' if now else ', loop agents first'}", flush=True)
    threads, contention, last = {}, {}, None
    while todo or threads:
        for k in [k for k, th in threads.items() if not th.is_alive()]:
            del threads[k]
            r = exp.result(k) or {}
            print(time.strftime("%H:%M:%S"), f"✔ {k}: {'pass' if r.get('passed') else 'FAIL'} "
                  f"{r.get('wall_s', 0) / 60:.1f} min {r.get('error', '')}", flush=True)
            build_report(exp)
        lb = loop_busy()
        for k in threads:
            contention.setdefault(k, []).append(lb + len(threads) - 1)
        free = slots - len(threads) - (0 if now else lb)
        msg = f"running {len(threads)} trial(s), loop agents busy {lb}, {len(todo)} waiting"
        if msg != last:
            print(time.strftime("%H:%M:%S"), msg, flush=True)
            last = msg
        while todo and free > 0:
            t, v, r = todo.pop(0)
            k = exp.tid(t, v, r)
            print(time.strftime("%H:%M:%S"), f"→ {k}", flush=True)
            contention[k] = [lb + len(threads)]   # other agents on the server as it starts
            th = threading.Thread(target=run_trial, args=(exp, t, v, r, pin, contention), daemon=True)
            th.start()
            threads[k] = th
            free -= 1
            time.sleep(2)   # stagger worktree creation
        time.sleep(POLL)
    build_report(exp)
    print(f"done: reports/ab/{exp.name}.html")


def cmd_status(exp):
    pin = jload(exp.d / "pinned.json") or {}
    print(f"{exp.name}: project {exp.project} base {pin.get('base', '(not pinned yet)')[:9]}")
    counts = {}
    for t, v, r in exp.order():
        s, res = exp.state(t, v, r)
        c = counts.setdefault(v, {"done": 0, "running": 0, "todo": 0, "stale": 0, "pass": 0})
        c[s] += 1
        c["pass"] += bool(s == "done" and res.get("passed"))
    for v, c in counts.items():
        print(f"  {v:14} done {c['done']:3}  pass {c['pass']:3}  running {c['running']}  todo {c['todo']:3}  stale {c['stale']}")


def cmd_clean(exp):
    for wt in sorted((WORK / exp.name).glob("*")):
        sh("git", "-C", str(exp.pdir), "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(wt, ignore_errors=True)
    sh("git", "-C", str(exp.pdir), "worktree", "prune")
    print(f"removed worktrees; diffs stay on branches ab/{exp.name}/*")


# ── judging ────────────────────────────────────────────────────────────────────

JUDGE_PROMPT = """You are grading two independent attempts, X and Y, at the same software task in the same repository.
Judge only the code changes below; you are not told who wrote them or how they were produced.

## Task given to both
{task}

## Rubric
{rubric}

## Attempt X — diff
{x}

## Attempt Y — diff
{y}

Compare X and Y against the rubric. A tie is fine when neither is better. Reply with JSON only:
{{"winner": "X" | "Y" | "tie", "score_x": <1-10>, "score_y": <1-10>, "reason": "<two sentences>"}}"""


def judge_diff(exp, r):
    excl = [f":(exclude){b}" for b in BOOKKEEPING]
    d = sh("git", "-C", str(exp.pdir), "diff", "-M", "--no-color", r["setup"], r["head"], "--", ".", *excl)
    if len(d) > JUDGE_MAX_DIFF:
        d = d[:JUDGE_MAX_DIFF] + f"\n[diff truncated: {len(d) - JUDGE_MAX_DIFF} more characters not shown]"
    return d or "(no changes)"


def ask_claude(prompt, model):
    with tempfile.TemporaryDirectory() as tmp:   # empty cwd: no project CLAUDE.md or memory in the judge's context
        r = subprocess.run(["claude", "-p", "--model", model, "--output-format", "json", "--tools", "", "--no-session-persistence"],
                           input=prompt, capture_output=True, text=True, cwd=tmp, timeout=900)
    out = json.loads(r.stdout or "{}")
    text = out.get("result", "")
    m = re.search(r"\{.*\}", text, re.S)
    v = json.loads(m.group(0)) if m else {}
    if v.get("winner") not in ("X", "Y", "tie"):
        raise ValueError(f"unparseable verdict: {text[:200]}")
    return v


def pairs(exp):
    """(task, rep, a, b) for every variant pair whose two trials are done (not stale)."""
    names = list(exp.variants)
    out = []
    for t in exp.tasks:
        for r in range(1, exp.repeats + 1):
            for i, a in enumerate(names):
                for b in names[i + 1:]:
                    if exp.state(t, a, r)[0] == "done" and exp.state(t, b, r)[0] == "done":
                        out.append((t, r, a, b))
    return out


def cmd_judge(exp, args):
    samples = int(args[args.index("--samples") + 1]) if "--samples" in args else 1
    model = args[args.index("--model") + 1] if "--model" in args else JUDGE_MODEL
    rubric = exp.spec.get("judge", {}).get("rubric", "")
    jd = exp.d / "judge"
    n = 0
    for t, rep, a, b in pairs(exp):
        ra, rb = exp.result(exp.tid(t, a, rep)), exp.result(exp.tid(t, b, rep))
        da, db = judge_diff(exp, ra), judge_diff(exp, rb)
        for order, (x, y) in (("ab", (da, db)), ("ba", (db, da))):
            for s in range(samples):
                prompt = JUDGE_PROMPT.format(task=exp.tasks[t]["prompt"], rubric=rubric, x=x, y=y)
                f = jd / f"{t}.{rep}.{a}-{b}.{order}.{s}.json"
                ph = h({"prompt": prompt, "model": model, "sample": s})
                if (jload(f) or {}).get("prompt_hash") == ph:
                    continue
                try:
                    v = ask_claude(prompt, model)
                except Exception as e:
                    print(f"  {f.name}: {e}")
                    continue
                # map X/Y back to variants
                first, second = (a, b) if order == "ab" else (b, a)
                win = {"X": first, "Y": second, "tie": "tie"}[v["winner"]]
                jsave(f, {"task": t, "rep": rep, "a": a, "b": b, "order": order, "sample": s, "model": model,
                          "prompt_hash": ph, "winner": win, "scores": {first: v.get("score_x"), second: v.get("score_y")},
                          "reason": v.get("reason", ""), "t": time.time()})
                n += 1
                print(f"  {t} rep {rep} {a} vs {b} [{order}]: {win}", flush=True)
    print(f"{n} new verdicts")
    build_report(exp)


def verdicts(exp):
    """{(task, rep, a, b): {'a': wins, 'b': wins, 'tie': n, 'n': k, 'score_a': mean, 'score_b': mean, ...}}.
    Both orders are kept: a win needs the pair to win in total across orders (order bias cancels)."""
    out = {}
    valid = {(t, r, a, b) for t, r, a, b in pairs(exp)}
    for f in sorted((exp.d / "judge").glob("*.json")):
        v = jload(f)
        k = (v["task"], v["rep"], v["a"], v["b"])
        if k not in valid:
            continue
        o = out.setdefault(k, {"a": 0, "b": 0, "tie": 0, "n": 0, "sa": [], "sb": [], "reasons": [], "orders": {}})
        o["n"] += 1
        o["a" if v["winner"] == v["a"] else "b" if v["winner"] == v["b"] else "tie"] += 1
        o["orders"].setdefault(v["order"], []).append(v["winner"])
        o["sa"].append(v["scores"].get(v["a"]))
        o["sb"].append(v["scores"].get(v["b"]))
        o["reasons"].append(f"[{v['order']}] {v['winner']}: {v['reason']}")
    for o in out.values():
        o["outcome"] = "a" if o["a"] > o["b"] else "b" if o["b"] > o["a"] else "tie"
        both = [w for ws in o["orders"].values() for w in ws]
        o["consistent"] = len(set(both)) == 1
    return out


# ── report ─────────────────────────────────────────────────────────────────────

def stats(exp):
    names = list(exp.variants)
    rows = {}
    for t, v, r in exp.order():
        s, res = exp.state(t, v, r)
        rows[(t, v, r)] = {"state": s, **({k: res.get(k) for k in (
            "passed", "wall_s", "turns", "tools", "input_tokens", "output_tokens", "files", "add", "dele", "error",
            "timed_out", "others_mean", "others_max", "check_rc", "passed_initial", "phase2_first", "phase2_final",
            "rounds")} if res else {})}
    per = {}
    for v in names:
        done = [x for (t, vv, r), x in rows.items() if vv == v and x["state"] == "done"]
        k = sum(1 for x in done if x.get("passed"))
        lo, hi = wilson(k, len(done))
        per[v] = {"n": len(done), "pass": k, "rate": k / len(done) if done else None, "lo": lo, "hi": hi,
                  "wall": median(x.get("wall_s") for x in done), "out": median(x.get("output_tokens") for x in done),
                  "inp": median(x.get("input_tokens") for x in done), "turns": median(x.get("turns") for x in done),
                  "lines": median((x.get("add") or 0) + (x.get("dele") or 0) for x in done),
                  "errors": sum(1 for x in done if x.get("error")), "timeouts": sum(1 for x in done if x.get("timed_out")),
                  "contended": sum(1 for x in done if (x.get("others_max") or 0) > 0),
                  "initial": sum(1 for x in done if x.get("passed_initial")),
                  "p2_fail": sum(1 for x in done if x.get("phase2_first") is False),
                  "rounds": sum(x.get("rounds") or 0 for x in done),
                  "own_ok_hidden_fail": sum(1 for x in done if x.get("phase2_final") and not x.get("passed")),
                  "fixed": sum(1 for x in done if x.get("passed") and x.get("passed_initial") is False),
                  "broke": sum(1 for x in done if not x.get("passed") and x.get("passed_initial"))}
    vd = verdicts(exp)
    pw = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            only_a = only_b = both = neither = 0
            for t in exp.tasks:
                for r in range(1, exp.repeats + 1):
                    x, y = rows[(t, a, r)], rows[(t, b, r)]
                    if x["state"] != "done" or y["state"] != "done":
                        continue
                    pa, pb = bool(x.get("passed")), bool(y.get("passed"))
                    only_a += pa and not pb
                    only_b += pb and not pa
                    both += pa and pb
                    neither += not pa and not pb
            js = [o for (t, r, aa, bb), o in vd.items() if (aa, bb) == (a, b)]
            wa, wb, ti = (sum(o["outcome"] == s for o in js) for s in ("a", "b", "tie"))
            lo, hi = wilson(wa, wa + wb)
            pw.append({"a": a, "b": b, "only_a": only_a, "only_b": only_b, "both": both, "neither": neither,
                       "pairs": only_a + only_b + both + neither, "mcnemar_p": binom_p(min(only_a, only_b), only_a + only_b),
                       "wins_a": wa, "wins_b": wb, "ties": ti, "judged": len(js), "win_lo": lo, "win_hi": hi,
                       "sign_p": binom_p(min(wa, wb), wa + wb),
                       "consistent": sum(o["consistent"] for o in js),
                       "score_a": median(s for o in js for s in o["sa"]), "score_b": median(s for o in js for s in o["sb"])})
    grid = [{"task": t, "rep": r, "cells": {v: {**rows[(t, v, r)], "id": exp.tid(t, v, r)} for v in names}}
            for r in range(1, exp.repeats + 1) for t in exp.tasks]
    jv = {f"{t}.{r}": {f"{a}|{b}": {"outcome": o["outcome"], "a": a, "b": b, "consistent": o["consistent"],
                                    "reasons": o["reasons"]} for (tt, rr, a, b), o in vd.items() if (tt, rr) == (t, r)}
          for t in exp.tasks for r in range(1, exp.repeats + 1)}
    return {"variants": names, "per": per, "pairwise": pw, "grid": grid, "judge": jv}


TRIAL_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{css}</style></head><body><main>
<div class="note"><a href="../{exp}.html">← {exp}</a></div><h1>{h1}</h1><div class="sub">{meta}</div>
<div class="tiles">{tiles}</div>
<details><summary>prompt the agent got</summary><pre class="mono pre">{prompt}</pre></details>
<details><summary>variant</summary><pre class="mono pre">{variant}</pre></details>
<details{chk_open}><summary>acceptance check — {chk}</summary><pre class="mono pre">{check}</pre></details>
<details><summary>phase 2 (fresh-checkout re-check, every round)</summary><pre class="mono pre">{phase2}</pre></details>
<details><summary>agent's final message</summary><pre class="mono pre">{final}</pre></details>
<details><summary>console (last 200 lines)</summary><pre class="mono pre">{console}</pre></details>
<h2 class="sec">Changes ({nfiles} files, <span class="plus">+{add}</span> <span class="minus">−{dele}</span>)</h2>
{body}</main></body></html>"""


def trial_page(exp, r):
    td = exp.trials_d / r["id"]
    files = diffpage.parse(sh("git", "-C", str(exp.pdir), "diff", "-M", "--no-color", r["setup"], r["head"], check=False)) \
        if r.get("setup") and r.get("head") else []
    files.sort(key=lambda f: (f["path"] in diffpage.BOOKKEEPING, f["path"]))
    read = lambda p, n=None: html.escape("\n".join((td / p).read_text(errors="replace").splitlines()[-n:] if n else
                                                   (td / p).read_text(errors="replace").splitlines())) if (td / p).exists() else "–"
    tile = lambda k, v, cls="": f'<div class="tile"><div class="k">{k}</div><div class="v {cls}">{v}</div></div>'
    ok = r.get("passed")
    tiles = "".join([tile("check", "pass" if ok else "fail", "" if ok else "bad"),
                     tile("round 0", {True: "pass", False: "fail"}.get(r.get("passed_initial"), "–")),
                     tile("feedback rounds", r.get("rounds", 0)),
                     tile("wall time", f"{(r.get('wall_s') or 0) / 60:.1f} min"), tile("turns", r.get("turns") or "–"),
                     tile("tool calls", r.get("tools") or 0), tile("tokens out", r.get("output_tokens") or "–"),
                     tile("other agents", f"{r.get('others_max', 0)} max")])
    page = TRIAL_PAGE.format(
        title=html.escape(r["id"]), css=reportui.CSS + diffpage.CSS + "pre.pre{white-space:pre-wrap;max-height:420px;overflow:auto}",
        exp=html.escape(exp.name), h1=html.escape(f"{r['task']} · variant {r['variant']} · rep {r['rep']}"),
        meta=html.escape(f"{exp.project} {r.get('base', '')[:9]} → {r.get('head', '')[:9]} · branch ab/{exp.name}/{r['id']}"
                         + (f" · error: {r['error']}" if r.get("error") else "")),
        tiles=tiles, prompt=html.escape(r.get("prompt", "")),
        variant=html.escape(json.dumps(exp.variants[r["variant"]], indent=1)),
        chk=f"exit {r.get('check_rc')}", chk_open="" if ok else " open", check=read("check.txt", 300),
        final=html.escape(r.get("final") or "–"), console=read("console.txt", 200), phase2=read("phase2.txt", 300),
        nfiles=len(files), add=sum(f["add"] for f in files), dele=sum(f["del"] for f in files),
        body="\n".join(diffpage.render_file(f, f"f{i}") for i, f in enumerate(files)) or '<div class="note">no changes</div>')
    out = REPORTS / exp.name / f"{r['id']}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)


REPORT_JS = r"""
const P=D.per,V=D.variants,pct=x=>x==null?'–':Math.round(100*x)+'%',ci=(l,h)=>l==null?'':` <span class="note">[${pct(l)}–${pct(h)}]</span>`;
const pv=p=>p==null?'–':p<0.001?'<0.001':p.toFixed(3);
let s=`<h1>A/B · ${esc(D.name)}</h1><div class="sub">${esc(D.project)} @ ${esc((D.pin.base||'').slice(0,9))} · harness ${esc((D.pin.harness||'').slice(0,9))} · ${esc(D.pin.model||'')} · ${D.done}/${D.total} trials done${D.stale?` · ${D.stale} stale`:''}</div>`;
s+=card('Per variant','pass = hidden acceptance check exit 0, with a 95% Wilson interval. Medians for the rest. "contended" = trials that shared the model server with other agents (slower wall time, same quality).',
 '<table><tr><th>variant</th><th class="num">trials</th><th class="num">pass</th><th class="num">wall</th><th class="num">tokens in</th><th class="num">tokens out</th><th class="num">turns</th><th class="num">lines ±</th><th class="num">timeouts</th><th class="num">errors</th><th class="num">contended</th></tr>'+
 V.map((v,i)=>{const p=P[v];return `<tr><td><span class="sw" style="background:${col(i)}"></span>${esc(v)}</td><td class="num">${p.n}</td><td class="num">${pct(p.rate)}${ci(p.lo,p.hi)}</td><td class="num">${mins(p.wall/60)}</td><td class="num">${k(p.inp)}</td><td class="num">${k(p.out)}</td><td class="num">${f1(p.turns)}</td><td class="num">${k(p.lines)}</td><td class="num">${p.timeouts}</td><td class="num">${p.errors}</td><td class="num">${p.contended}</td></tr>`}).join('')+'</table>');
s+=card('Head to head','Same task, same repeat. Check: trials where only one side passed decide it (exact McNemar test). Judge: Claude compares the two diffs blind, in both orders; a pair counts as a win only if it wins across both orders, otherwise tie. p < 0.05 ≈ the difference is unlikely to be luck.',
 '<table><tr><th>pair</th><th class="num">paired trials</th><th class="num">only A passes</th><th class="num">only B passes</th><th class="num">check p</th><th class="num">judged</th><th class="num">A wins</th><th class="num">B wins</th><th class="num">ties</th><th class="num">A win rate</th><th class="num">judge p</th><th class="num">median score A / B</th></tr>'+
 D.pairwise.map(w=>`<tr><td>${esc(w.a)} vs ${esc(w.b)}</td><td class="num">${w.pairs}</td><td class="num">${w.only_a}</td><td class="num">${w.only_b}</td><td class="num">${pv(w.mcnemar_p)}</td><td class="num">${w.judged}</td><td class="num">${w.wins_a}</td><td class="num">${w.wins_b}</td><td class="num">${w.ties}</td><td class="num">${w.wins_a+w.wins_b?pct(w.wins_a/(w.wins_a+w.wins_b)):'–'}${ci(w.win_lo,w.win_hi)}</td><td class="num">${pv(w.sign_p)}</td><td class="num">${w.score_a??'–'} / ${w.score_b??'–'}</td></tr>`).join('')+'</table>');
if(V.some(v=>P[v].p2_fail!=null))s+=card('Verification phase','Every trial: after the agent stops, the task\'s own check runs twice in a fresh checkout of its committed work ("phase 2"). Variants with verify get the failure output back and continue (up to the variant\'s rounds); the others only record it. Hidden pass is graded on pristine tests, before feedback (round 0) and at the end.',
 '<table><tr><th>variant</th><th class="num">trials</th><th class="num">hidden pass, round 0</th><th class="num">hidden pass, final</th><th class="num">fixed by feedback</th><th class="num">broken by feedback</th><th class="num">"done" but phase 2 failed</th><th class="num">feedback rounds</th><th class="num">own check passes, hidden fails</th></tr>'+
 V.map((v,i)=>{const p=P[v];return `<tr><td><span class="sw" style="background:${col(i)}"></span>${esc(v)}</td><td class="num">${p.n}</td><td class="num">${p.n?pct(p.initial/p.n):'–'}</td><td class="num">${pct(p.rate)}</td><td class="num">${p.fixed}</td><td class="num">${p.broke}</td><td class="num">${p.p2_fail} (${p.n?pct(p.p2_fail/p.n):'–'})</td><td class="num">${p.rounds}</td><td class="num">${p.own_ok_hidden_fail}</td></tr>`}).join('')+'</table>');
const cell=c=>{if(c.state!=='done'&&c.state!=='stale')return `<td class="note">${c.state}</td>`;
 const cls=c.passed?'':'bad',t=c.passed?'✔ pass':c.error?'✖ error':c.timed_out?'✖ timeout':'✖ fail';
 return `<td><a class="${cls}" href="${esc(D.name)}/${esc(c.id)}.html">${t}</a>${c.state==='stale'?' <span class="warn">stale</span>':''}<div class="note">${mins((c.wall_s||0)/60)} · ${k(c.output_tokens)} out · ±${k((c.add||0)+(c.dele||0))}${c.others_max?' · shared':''}</div></td>`};
s+=card('Trials','Click a cell for the diff, the check output and the agent\'s final message. Judge column: who won that task + repeat (hover for the judge\'s reasons).',
 '<div class="scroll" style="max-height:none"><table><tr><th>task</th><th class="num">rep</th>'+V.map(v=>`<th>${esc(v)}</th>`).join('')+'<th>judge</th></tr>'+
 D.grid.map(g=>{const j=Object.values(D.judge[g.task+'.'+g.rep]||{});return `<tr><td class="mono">${esc(g.task)}</td><td class="num">${g.rep}</td>${V.map(v=>cell(g.cells[v])).join('')}<td>${j.map(o=>`<span title="${esc(o.reasons.join('\n'))}">${o.outcome==='tie'?'tie':esc(o.outcome==='a'?o.a:o.b)}${o.consistent?'':'<span class="note"> (orders disagree)</span>'}</span>`).join('<br>')||'<span class="note">–</span>'}</td></tr>`}).join('')+'</table></div>');
s+=card('How many trials?','',`<div class="note" style="color:var(--ink2);font-size:13px">Count <b>paired</b> trials (same task + repeat, both variants done). Rough sizes for 80% power at p &lt; 0.05:
<ul><li>Pass rate 40% → 70% (big effect): ~40 pairs. 50% → 70%: ~90 pairs. 10-point differences need ~400: not worth chasing locally.</li>
<li>Judge win rate: the 95% interval is about ±98/√n points, so 30 judged pairs → ±18, 100 → ±10.</li>
<li>Use many different tasks (8–20) with 2–5 repeats each rather than one task repeated: a single task only tells you about that task.</li>
<li>Decide n before you start and don't stop as soon as p dips below 0.05 — peeking inflates false positives.</li></ul></div>`);
app.innerHTML=s;
"""


def build_report(exp):
    REPORTS.mkdir(parents=True, exist_ok=True)
    for f in exp.trials_d.glob("*/result.json"):
        r = jload(f)
        if r and r.get("id"):
            try:
                trial_page(exp, r)
            except Exception as e:
                print(f"  trial page {r['id']}: {e}")
    st = stats(exp)
    states = [exp.state(t, v, r)[0] for t, v, r in exp.order()]
    data = {"name": exp.name, "project": exp.project, "pin": jload(exp.d / "pinned.json") or {},
            "done": states.count("done"), "stale": states.count("stale"), "total": len(states), **st}
    (REPORTS / f"{exp.name}.html").write_text(reportui.page(f"A/B {exp.name}", data, REPORT_JS))
    jsave(exp.d / "summary.json", {k: data[k] for k in ("per", "pairwise", "done", "total", "stale")})
    items = []
    for d in sorted(AB.glob("*/spec.toml")):
        n = d.parent.name
        s = jload(AB / n / "summary.json") or {}
        items.append(f'<li><a href="{html.escape(n)}.html">{html.escape(n)}</a> <span class="note">{s.get("done", 0)}/{s.get("total", "?")} trials</span></li>')
    (REPORTS / "index.html").write_text(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>A/B experiments</title><style>{reportui.CSS}</style></head><body><main><h1>A/B experiments</h1>'
        f'<div class="note"><a href="../index.html">← all runs</a></div><ul>{"".join(items)}</ul></main></body></html>')


def cmd_new(name, args):
    d = AB / name
    if (d / "spec.toml").exists():
        sys.exit(f"ab/{name}/spec.toml exists")
    project = args[0] if args else "PROJECT"
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.toml").write_text(TEMPLATE.format(project=project, exp=name))
    print(f"wrote ab/{name}/spec.toml — fill in tasks and variants, then ./agent ab run {name}")


def main():
    a = sys.argv[1:]
    if len(a) < 2:
        sys.exit(__doc__)
    cmd, name, rest = a[0], a[1], a[2:]
    if cmd == "new":
        return cmd_new(name, rest)
    if cmd == "import":
        import abbench
        return abbench.main(a[1:])
    exp = Exp(name)
    {"run": lambda: cmd_run(exp, rest), "status": lambda: cmd_status(exp), "judge": lambda: cmd_judge(exp, rest),
     "report": lambda: (build_report(exp), print(f"reports/ab/{name}.html")), "clean": lambda: cmd_clean(exp)
     }.get(cmd, lambda: sys.exit(__doc__))()


if __name__ == "__main__":
    main()
