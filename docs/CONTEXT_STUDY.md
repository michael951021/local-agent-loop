# Making the local loop measurable and bounded

This is a working engineering case study on two 3090s, a local 27B model, two
workers and 128K contexts. It uses existing transcripts and reports; the changes
add no model calls. The question is: **can tighter task contracts and bounded
evidence reduce tokens and compactions per accepted change without lowering
completion quality?** Improvements to that outcome still need measurement.

## Observations, September 29, 2026

The initial study snapshot contained 326 runs and 94.1 agent-hours. In the
current server era, generation accounted for about 79% of attributed run time;
prefill about 10%. Estimated generated thinking was 6.5 million tokens across
the study. Bash and Read results contributed about 8.2 million estimated tokens.
Those estimates use the study's character calibration and are not independent
token measurements or evidence that reasoning can safely be removed.

After adding the deterministic audit, a later snapshot had 332 runs, 12,120 tool
results and 23,831,111 text characters. It found 176 results over 16,000
characters, 983 of 1,345 Read calls without explicit limits, and 346 tool-result
errors. It found **zero** identical Read requests with identical output within
one compaction segment. Changed-file reads are not duplicates. These numbers
support controlling large outputs; they do not establish redundant reads as
a current bottleneck. The workers kept running during analysis, so totals vary.

The standup's large backlog was also ambiguous: `myapp` had no recorded runs,
while `contrib-loop` had active claims and completed work. A planned checkbox is
neither missing framework code nor a promise that a worker will implement it.

## Implemented controls

* PreToolUse/Read checks the actual requested text range, defaulting to 240
  lines / 16,000 characters. A small file still works without an explicit limit.
  Oversized ranges return a corrective error before their content enters the
  context. Binary/document tools retain their native behavior. The settings are
  `AGENT_READ_MAX_LINES` and `AGENT_READ_MAX_CHARS` in `config.env`.
* `hooks/capture.py` saves complete command evidence and prints at most the last
  40 lines / approximately 6 KB. It propagates the command's exit code, refuses
  overwrites, and terminates the process group on timeout (exit 124). This fixes
  the old prompt's `command | tail` pattern, which could hide test failure.
  Shell commands can still emit large results; the helper is a prompted workflow,
  not an enforced cap on Bash.
* SessionStart supplies a bounded NOTES snapshot and working-tree state, without
  unrelated TODO entries. Handoffs name evidence, exit codes, unresolved questions,
  and the next check. A new session or compaction can restore the snapshot.
* SessionStart now includes a small repository inventory. Each task prompt adds
  paths and Python declarations matching that task; `repo_map.py --query` refreshes
  the map from the current worktree on demand. It omits source bodies and untracked ignored
  files. This is a navigation hint: source search and tests remain necessary.
* Contracts specify Issue, Question, Evidence, Done when, Stop when, and Plan.
  Agents stop or split after two attempts with no new evidence. This is a prompt
  rule, not a hard scheduler retry limit; existing configured limits still apply.
* Standup separates dependency-ready work, claimed work, waiting/blocked work,
  and done work. It shows missing contract fields at pickup. Presence is not a
  semantic quality score. Existing three-section specs remain valid.
* Run reports and the study audit main-thread tool text, errors, unbounded reads,
  large results and exact repeated reads. Repeats reset at explicit compaction
  boundaries. Subagent contexts and non-text payloads are excluded. Audit output
  contains counts, paths and turn numbers, not copied tool bodies or commands.
* Newly started loop processes record their harness commit, context policy and
  Read budgets in run metadata, exported with `study/context_audit.csv`. A commit
  is a revision identifier, not proof of an unchanged working tree. The map
  version uses `bounded-map-v2`; workers started before this change may use the
  new prompt code while retaining an older policy label, so treat those runs as
  mixed rather than a clean comparison group.

## Better questions for this backlog

“Build a security agent” bundles discovery, trust boundaries, reproduction,
classification and reporting. It does not say what would make a finding true.
Keep framework capability, a completed investigation and a verified vulnerability
as separate claims. An investigation may correctly conclude intended behavior.

Example contract for a bounded, authorized local investigation:

```markdown
## Issue
Determine whether the local fixture enforces the documented resource ownership rule.
## Question
Can fixture user B read user A's private record through the tested handler?
A permission denial with no record disclosure refutes this suspected bypass.
## Evidence
Start at the handler and authorization test fixture; record the fixture revision,
request, response and relevant assertion in .agent-evidence/ownership-1.log.
## Done when
The focused test distinguishes owner access from non-owner denial; a real failure
gets a regression test and fix. Otherwise record the intended-behavior conclusion.
## Stop when
Only the local fixture is in scope. Stop if target authorization or ownership
semantics are missing, or two attempts produce no new evidence.
## Plan
Inspect the boundary, run the smallest reproducer, classify, then fix if warranted.
```

For a performance task, ask “Does bounded output reduce result characters and
compactions per accepted task on the same fixtures?” and define a quality check.
For a portfolio task, ask “Can a reviewer reproduce this finding from the pinned
fixture and saved command?” A case study needs an observable outcome, not just a
feature list or a generated narrative.

## Reproduce and evaluate

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
bash tests/smoke.sh                      # fake agents; needs local services/sockets
.venv/bin/python lib/study.py
.venv/bin/python lib/standup.py
.venv/bin/python lib/ctxreport.py LOG.jsonl --run RUN_ID
```

Open `reports/study.html` for the baseline and ranked large-output runs, then
follow a run link. Historical reports get the new audit when rebuilt; new run
reports include it automatically. The study invalidates old derived caches.

Compare tasks of similar kind and size using the same model/server configuration:
accepted completion rate, generation tokens per accepted task, wall time,
compactions, result characters, hook errors and saved-log retrievals. Exported run
IDs join the context CSV, timing CSV and original transcripts. Track regressions
as well as improvements. Do not
compare only the fastest successes or treat historical era differences as causal.
Older running shell workers can pick up prompt/hook files without acquiring the
new run metadata; treat `unrecorded` policy as unknown, not a clean control cohort.
The full harness configuration takes effect when the loop is next started.

## Highest-value remaining work

1. **Independent acceptance evidence.** The scheduler still relies on checked
   tasks and agent-reported outcomes. Add trusted acceptance commands for stable
   fixtures, with separately recorded exit codes and artifact hashes. A self-written
   test and a done checkbox alone are weak evidence of correctness.
2. **A small replay suite.** Pin representative bug, security and refactoring
   fixtures; run both policies repeatedly under equal budgets. Record failures and
   completion quality as well as speed. This is needed before claiming a speedup.
3. **Durable, addressable evidence.** Saved logs currently live in project/worktree
   storage. Add lifecycle/retention rules and artifact manifests before treating
   them as permanent research records. Preserve failed reproductions too.
4. **Question-aware scheduling.** A contract's headings can all exist while its
   question is still poor. Review high-cost or repeatedly blocked tasks for a
   falsifiable question and smaller scope before spending another long run.
5. **Semantic navigation when needed.** The repository map handles paths and
   Python declarations without a background process. It does not resolve types,
   references or implementations. Add a language server only for projects and
   tasks where those queries measurably cut search or mistakes; record server
   startup, memory use and extra context before enabling it for every run.

These are concrete gaps still open after this patch, not claims about proprietary
lab systems. The public portfolio artifact here is the reproducible measurement
method, tested controls, honest baseline and an explicit evaluation question.
