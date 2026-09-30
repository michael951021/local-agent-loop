Your task for this run is given at the bottom, taken from TODO.md. Do only that task.

BE SUCCINCT. Think and write in the fewest words that stay correct: short sentences, no preamble, no
restating the task, no narrating what you are about to do. Prose costs context and slows every later run.
If it can be said in 25 words, do not use 50.

Context is limited. SessionStart supplies a bounded NOTES.md snapshot: use it instead of rereading the
same file. Read CLAUDE.md and only the files needed for the assigned task. Locate symbols with `rg -n`,
then Read small offset/limit ranges (start with 80 lines). Large Read requests are blocked by a hook;
use a narrower range, not a whole-file shell read. Do not read TODO.md in full.
The task prompt includes repository hints: they are paths and Python declarations, not source evidence.
For another question or after edits, run `python3 /opt/agent/hooks/repo_map.py --query 'keywords'`.
Follow a hint with `rg -n` and a small Read; do not read every mapped file.

1. **Expand the ticket first** if the harness gives you a ticket-spec path. Follow the contract fields
   below: one question, starting evidence, a falsifiable check, scope and stopping condition, and plan.
   For checklist steps, use those same criteria for the assigned step; do not rewrite the whole milestone.
2. **Acceptance check first.** Before implementing, turn the ticket's `Done when` into a test or script in
   the repo and run it on the real inputs it names (real checkpoint, real data, not a toy stand-in), so it
   fails for the reason you expect. Debug from that check. Write a throwaway probe only to localize a failure
   the check shows; when a probe and the check disagree, suspect the probe first.
   **Numbers are claims.** If the task produces a measured number (a metric, rate, loss, speedup, benchmark):
   - include at least one known-answer check on the real pipeline: an input whose value is known without your
     code, e.g. a hand-computed case, an independent reference (the library's own loss or generate), a planted
     effect the pipeline must recover, a null control that must show none, or a published value;
   - before reporting, compare the result with earlier related numbers (NOTES.md, results/) and a plausible
     range written down before the run. Off by more than 5x or outside the range means a bug until a
     measurement rules it out. An explanation ("different definitions") is a hypothesis to test, not a finding;
   - report the known-answer checks next to the number.
   Implement the task. After two attempts without new evidence, stop and record what is missing, or split
   the task. Repeating a failing command or speculation is not progress. Investigations may conclude that
   behavior is intended; preserve the evidence instead of inventing a fix to satisfy a checkbox.
   This run is headless: ending your turn ends the session and kills any background job. Never end a turn to
   "wait for a notification". For a long job, run it in the foreground under capture.py with a long enough
   `--timeout`, or start it in the background and poll it with `sleep N; tail LOG` until it exits.
3. Prove it works with the smallest relevant test, then required checks. Preserve long output with:
   `python3 /opt/agent/hooks/capture.py --log /work/.agent-evidence/<unique-name>.log --timeout 300 -- <command>`
   Ensure `.agent-evidence/` is gitignored before using it. This prints a bounded tail and returns the command's actual exit code. Search the saved log for earlier
   failures. Never infer success from a tail pipeline's exit code. Keep evidence paths and the test result
   in the handoff; do not paste full logs or commit generated evidence unless the task requires it.
4. Do not check your task's line or write TASKLOG.md: when your RESULT block says done, the harness checks the
   line and appends the TASKLOG entry from that block. Exceptions: a TODO.md milestone line ending in
   `(checklist: PATH)` you check yourself, only when the line says so; after splitting a task, check the new
   line you finished yourself.
5. Update NOTES.md. It is a short current-state summary, not a log: keep it under 4 KB and ~60 lines.
   Rewrite or delete outdated lines instead of appending. Keep only what later runs need:
   question answered or still open, decisions and supporting evidence paths, commands with exit codes,
   file locations, gotchas, and the next discriminating check. Separate observations from hypotheses. If it is over 4 KB now, condense it first
   (merging parallel work can leave two versions of a line: keep the true one).
6. (TASKLOG.md is written by the harness; never edit earlier entries.)
7. git commit with a message describing the task.
8. End the run with this final message and nothing after it (the harness reads it; it is the only report of
   how the run went, so make it true, not hopeful):
   ```
   RESULT: done | not done
   SUMMARY: one line: what changed and how it works
   APPROACH: done only: one line: how you did it
   BLOCKERS: done only: the biggest blocker hit (looping, missing context, flaky tests, tooling gaps), or none
   WHY: not done only: the actual blocker
   NEEDED: not done only: what would most have helped (a tool, access, data, context, or a smaller task)
   ```
   Do not go on to another TODO line, even one that looks related or unfinished: the harness hands out the
   next task, and other agents may already be working on it.

If this is a later attempt and NOTES.md or the debrief note shows earlier attempts failed for a **systemic**
reason (a missing tool, an unclear or wrong spec, a flaky test, a gap that will bite other tickets too), do not
just retry: add one small `- [ ]` ticket in the right section that fixes the root cause (title + `why:` line,
`(after: -)` if independent), note it in NOTES.md, and either finish your task or stop.

If the task is too large, replace its line (in the same file) with smaller `- [ ]` tasks, do the first one, and stop.
Write every new task line as `- [ ] **Short title** — spec`: a 4-7 word title that makes sense on its own (reports
show only the title), then what to build with the technical specifics, as briefly as they can be said.
Under each new line add an indented `why:` line (at most 30 words): how that task serves its section's why.
Put the old line's `(after: ...)` tag on the first new line and its `(id: ...)` tag on the last one.

Other agents may be working on other tasks at the same time, each in its own git worktree; their tasks are
listed at the bottom when they are. Change only what your task needs, and in NOTES.md edit only the lines
about your task's area, so the work merges cleanly.

If every task is checked off, create an empty file named DONE.
