Your task for this run is given at the bottom, taken from TODO.md. Do only that task.

Context is limited. Do not read TODO.md in full; use `grep -n` on it if you need to find a line.
Read NOTES.md (the handoff from earlier runs) and CLAUDE.md. Read other files only when the task needs them,
and use `head`, `grep` or line ranges instead of whole large files.

1. Implement the task.
2. Run the tests or run the code to prove it works. Fix whatever fails. Pipe long output through `tail`.
3. Change that task's line from `- [ ]` to `- [x]` in the file named in the task header (TODO.md or a checklist).
   Edit only that line. If the step failed and you took a checklist's On failure path instead, leave it.
   Exception: a TODO.md milestone line ending in `(checklist: PATH)` is checked only when the line itself says so.
4. Update NOTES.md. It is a short current-state summary, not a log: keep it under 4 KB and ~60 lines.
   Rewrite or delete outdated lines instead of appending. Keep only what later runs need:
   decisions, commands, file locations, gotchas. If it is over 4 KB now, condense it first.
5. Append an entry to TASKLOG.md (create it with a one-line header if it doesn't exist yet). Unlike NOTES.md,
   this is a permanent record — never edit or delete earlier entries, only append. Exactly 3 lines for this task:
   - Goal: what this task was trying to accomplish
   - Approach: how you did it
   - Blockers: any big blocker hit (looping/retrying, missing context, flaky tests, tooling gaps), or "none"
6. git commit with a message describing the task.

If the task is too large, replace its line (in the same file) with smaller `- [ ]` tasks, do the first one, and stop.
If every task is checked off, create an empty file named DONE.
