Your task for this run is given at the bottom, taken from TODO.md. Do only that task.

BE SUCCINCT. Think and write in the fewest words that stay correct: short sentences, no preamble, no
restating the task, no narrating what you are about to do. Prose costs context and slows every later run.
If it can be said in 25 words, do not use 50.

Context is limited. Do not read TODO.md in full; use `grep -n` on it if you need to find a line.
Read NOTES.md (the handoff from earlier runs) and CLAUDE.md. Read other files only when the task needs them,
and use `head`, `grep` or line ranges instead of whole large files.

1. **Expand the ticket first** (only if the harness gave you a ticket-spec path below). Before coding, write
   that file: three short sections — `## Issue` (what is wrong / what is needed), `## Done when` (the check or
   test that proves it), `## Plan` (the approach). A few lines each, whole file under ~15 lines. It is your
   contract for this ticket and is shown on the board; keep it truthful and terse, and update it if the plan
   changes.
2. Implement the task.
3. Run the tests or run the code to prove it works. Fix whatever fails. Pipe long output through `tail`.
4. Change that task's line from `- [ ]` to `- [x]` in the file named in the task header (TODO.md or a checklist).
   Edit only that line, and leave its `(id: ...)` / `(after: ...)` tags as they are (the scheduler reads them).
   If the step failed and you took a checklist's On failure path instead, leave it.
   Exception: a TODO.md milestone line ending in `(checklist: PATH)` is checked only when the line itself says so.
5. Update NOTES.md. It is a short current-state summary, not a log: keep it under 4 KB and ~60 lines.
   Rewrite or delete outdated lines instead of appending. Keep only what later runs need:
   decisions, commands, file locations, gotchas. If it is over 4 KB now, condense it first
   (merging parallel work can leave two versions of a line: keep the true one).
6. Append an entry to TASKLOG.md (create it with a one-line header if it doesn't exist yet). Unlike NOTES.md,
   this is a permanent record — never edit or delete earlier entries, only append. Exactly 3 lines for this task:
   - Goal: what this task was trying to accomplish
   - Approach: how you did it
   - Blockers: any big blocker hit (looping/retrying, missing context, flaky tests, tooling gaps), or "none"
7. git commit with a message describing the task.
8. End the run with this final message and nothing after it (the harness reads it; it is the only report of
   how the run went, so make it true, not hopeful):
   ```
   RESULT: done | not done
   SUMMARY: one line: what changed and how it works
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
