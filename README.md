# Local agent loop

Claude Code running on a local Ollama model (Qwen 3.8 27B Q8) inside a bubblewrap sandbox.

## Daily use

```bash
./agent new myapp            # creates projects/myapp (its own git repo)
$EDITOR projects/myapp/CLAUDE.md projects/myapp/TODO.md
./agent loop myapp           # works through TODO.md, one task per iteration
./agent chat myapp           # or: interactive Claude Code session
./agent run myapp "add a --verbose flag"   # one-off headless task
./agent shell myapp          # bash inside the sandbox
```

Put `alias agent=~/Documents/coders/loop/agent` in `~/.bashrc` to use it from anywhere.

A TODO.md line ending in `(checklist: path/to/file.md)` is a milestone: while that file exists and
has open `- [ ]` lines, each iteration does its next line instead, and the milestone stays open. With
no file (or none open), the milestone line itself is the task. When a checklist step has failed
`MAX_STEP_ATTEMPTS` times, the agent is told to follow the checklist's `## On failure` section.
Each run is capped at `ITER_TIMEOUT` wall-clock time.

The loop stops when every `- [ ]` in TODO.md is checked, the agent creates `DONE`,
it hits `MAX_ITERS`, or `STUCK_LIMIT` iterations in a row change nothing.
Every iteration is git-committed, so `git log` / `git reset` in the project undoes anything.
Full transcripts are in `logs/*.jsonl`. Each finished task gets a 3-line entry (goal, approach,
blockers) appended to `projects/NAME/TASKLOG.md`; `projects/NAME/NOTES.md` is the rolling
current-state handoff the next iteration reads, not a log.

## Parallel agents

`./agent loop NAME` starts `AGENTS` agents (config.env, default 2; `-j N` overrides). With one, the
agent works in `projects/NAME` itself. With more, each agent gets a git worktree `work/NAME/wK` on
branch `agent/wK`, and they split up TODO.md:

- **Claiming.** `sched.py` hands each agent the first open task that nobody holds and whose
  dependencies are done. Claims live in `run/NAME/` and die with the agent.
- **Dependencies.** `(id: x)` names a task, `(after: x, y)` makes it wait for those; `(after: -)`
  means none. A line without `(after: ...)` waits for every open line above it, so an untagged
  TODO.md is worked top-down, exactly like a single agent.
- **Merging.** After each task the agent's branch is merged into the project branch under a lock.
  TODO.md, NOTES.md and TASKLOG.md merge line by line (`mergelines.py`); a real code conflict is
  handed back to the same agent as its next run, and parked on a branch if it fails twice.
- **Shared data.** Gitignored paths listed in `projects/NAME/.agent-shared` (venvs, clones,
  databases) are mounted into every agent's sandbox from the main checkout.
- **Tmux.** Each agent gets a pane (the current window if you are in tmux, else a new session).

```bash
./agent status contrib-loop       # who is on what, and what each open task waits for
./agent stop contrib-loop         # stop after the current tasks (--now: immediately)
./agent worker contrib-loop       # add one more agent to a running loop
AGENTS=1 ./agent loop other       # a second, single-agent loop on another project
```

Ollama must serve `NUM_PARALLEL` requests at once; `./agent setup` checks the systemd setting and
prints the one root command that changes it. Each slot owns a KV cache of `NUM_CTX` tokens.
`tests/smoke.sh` exercises all of this with a fake `claude` in about a minute.

## Reports

Every run gets `reports/<project>/<time>-[<agent>-]<session>-end.html` (and one per context
compaction); `reports/index.html` lists them and `reports/fleet.html` shows all agents on one
timeline with their share of the GPUs. Copy the folder anywhere to view it: the pages are
self-contained.

| Layer | Files | Job |
|---|---|---|
| Record | `./agent` (harness lines in `logs/*.jsonl`), `keepalive.py` (`logs/requests/`), `gpumon.py` (`logs/gpu/`), Ollama's journal | write facts as they happen, tagged by agent |
| Read | `telemetry.py` | parse and join them: runs, requests per agent, fair GPU share, VRAM split |
| Render | `reportui.py`, `ctxreport.py`, `fleet.py` | per-run page, index, fleet page |
| Trigger | `./agent` (end of run), `pretty.py` (compaction) | call the renderer; never block the agent |

## What the sandbox allows

| | Inside the sandbox |
|---|---|
| Your real home folder, SSH keys, `~/.claude` | not visible |
| `/usr`, `/etc` | read-only |
| `projects/NAME` | read-write, mounted at `/work` |
| `loop/home` | the agent's own `$HOME` (pip/npm installs, Claude config) |
| Network | on (`NET=on`) or Ollama-only (`NET=off`) in `config.env` |

## Prompt layers (outer → inner)

| Layer | File | Applies to |
|---|---|---|
| Built-in Claude Code prompt | — | everything |
| Appended system prompt | `prompts/system.md` | every run (`--append-system-prompt-file`) |
| User memory | `home/.claude/CLAUDE.md` | every project |
| Project memory | `projects/NAME/CLAUDE.md` (+ `.claude/rules/*.md`) | one project |
| Loop instructions | `prompts/loop.md` | each loop iteration (the user prompt) |
| Live context hook | `hooks/context.sh` via `home/.claude/settings.json` | injected at session start |
| Subagents | `home/.claude/agents/*.md` | own prompt + tools, called on demand |
| Skills | `home/.claude/skills/NAME/SKILL.md` | loaded when relevant |

Keep these short: every layer uses up the model's context window.

## Changing the model

Edit `BASE_MODEL` / `NUM_CTX` in `config.env`, then `./agent setup`.

## Web search

Claude Code's built-in WebSearch only works against Anthropic's API, so it and WebFetch are
disabled (`home/.claude/settings.json`). Instead, `mcp/websearch.py` (an MCP server, registered in
`home/.claude.json`) gives the agent `web_search` (DuckDuckGo/ddgs, no API key) and `fetch_page`.
Its Python deps live in the sandbox at `home/.venvs/mcp`. If you ever wipe `home/`, reinstall with:

    ./agent shell <any-project>
    python3 -m venv ~/.venvs/mcp && ~/.venvs/mcp/bin/pip install mcp ddgs
