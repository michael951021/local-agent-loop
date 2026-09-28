# Model calls

Every point where the loop calls the model, how often, and why it is its own call. Generated from
`phases.py` (`python3 phases.py --md`); `python3 phases.py --measure 24` counts the last 24 h from the
proxy's request log, where every request is tagged with its phase. Each run report has a
"Model calls by phase" table.

Rule: a new model call needs a row here saying why it could not be folded into an existing one.

| Phase | Layer | When | Calls | Yields | Why its own call |
|---|---|---|---|---|---|
| **Agent turn** (`agent`) | harness | every step of a task run (Claude Code's tool loop) | 15–100 per run | the next tool call, or the run's final message | Each step needs the result of the previous tool call, so the loop is sequential by nature. Its cost is the re-read prompt, which the server serves from cache (~95% reused). |
| **Final message + outcome** (`final`) | harness | the last turn of a run | 1 per run (part of the agent turns) | one-line summary plus the RESULT block: done or not, why, what would have helped, handoff for a successor | Folded: this used to be a separate debrief call that re-read the whole session after it ended. The agent now writes it in its last turn, when everything is still in context. |
| **Compaction** (`compact`) | harness | the context nears the window (~window − 33k tokens) | 0–4 per run | a summary that replaces the conversation | Triggered by size mid-run and needs the whole context as input; a larger window (128k per agent) is what reduces it. |
| **Debrief (fallback)** (`debrief`) | harness | a run ended normally but its final message has no RESULT block | 0–1 per run, rare | why the task was not finished, what would have helped | Only a fallback for the folded final message; resumes the session with no tools and 1 turn. |
| **Handoff** (`handoff`) | harness | a run was interrupted (./agent stop --now, kill, crash) | 0–1 per interrupted run | 2 lines for the agent that continues: what is done, what is left | The interrupted run was cut off mid-generation, so it could not write this itself. |
| **Merge-conflict run** (`merge`) | harness | the branch still conflicts with the main branch after the run | rare (agents now merge the main branch before they end) | a resolved merge | Folded where possible: the agent merges the main branch itself before ending, with its change in context. A separate run remains only when another agent merged something conflicting in between. |
| **Claude Code side call** (`side`) | harness | Claude Code's own small-model requests | measured | housekeeping (e.g. titles, command checks) | Not ours; they go to the same 27B model because every model name maps to it. Measured so they can be switched off if they cost GPU time. |
| **Claim judge** (`claims`) | pipeline | scripts/claims.py: a comment the rules call ambiguous | 1 per ambiguous comment | claim / not_claim | Candidate for folding: one structured call per issue can judge all its comments and tags at once (TODO: one issue-judge call). |
| **Maintainer-status judge** (`maint`) | pipeline | scripts/maint.py: an ambiguous maintainer comment | 1 per ambiguous maintainer comment | fixed / blocked / intentional / awaiting / needs_discussion / none | Same issue as the claim judge: folded into the per-issue call. |
| **Relevance tagger** (`relevance`) | pipeline | scripts/relevance.py: an issue no rule tagged | 1 per untagged issue | subsystem tags with reasons | Same issue, same input (title + body): folded into the per-issue call. |
| **Issue judge** (`judge`) | pipeline | scripts/judge.py: an issue with anything the rules left ambiguous | 1 per such issue (replaces claims + maint + relevance calls) | claim and maintainer verdicts for every ambiguous comment, plus relevance tags | One structured (JSON-schema) call per issue: all three questions read the same thread. |
| **Other project call** (`pipeline`) | pipeline | any other scripts/ call (writer, coder, benchmarks) | measured | varies | Listed here once they run in the loop. |

## Before this table existed (contrib-loop, 27 Sep, 71 runs, 3183 requests, 28 GPU-hours)

- Agent turns: nearly all of it. Claude Code made no side calls of its own (`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`).
- Compactions: 56, mostly in the 96k-context period. The 128k window cut them.
- Debrief calls: 4. These are now part of the final message, and a separate call only happens as a fallback.
- Handoff calls: 8, one per interrupted run.
- Merge-fix runs: 3. Agents now merge the main branch before they end.
- Pipeline calls (claims, maint, relevance) went through the proxy but were not logged. They are logged from now on,
  and the "One judge call per issue" task folds them into one call per issue.
