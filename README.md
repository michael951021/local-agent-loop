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

- **Stopping mid-task.** `stop --now` interrupts the runs: each agent's session is resumed once,
  without tools, to write a 2-line handoff (≤ 60 words: what is done and how it works / what is
  left). The work is committed on the agent's branch, not merged, and the same agent continues
  that task next time with the handoff in its prompt. A run cut off by `ITER_TIMEOUT` is paused
  the same way; after `stop --kill`, a crash or a reboot the handoff is written when the agent
  starts again.

```bash
./agent status contrib-loop       # who is on what, paused tasks and their handoffs, what each task waits for
./agent stop contrib-loop         # stop after the current tasks (--now: interrupt with a handoff; --kill)
./agent worker contrib-loop       # add one more agent to a running loop
AGENTS=1 ./agent loop other       # a second, single-agent loop on another project
```

### Model server (`BACKEND` in config.env)

Ollama runs this model (architecture `qwen35`, part attention and part recurrent) one request at a
time, whatever `OLLAMA_NUM_PARALLEL` says, and reuses almost none of the previous prompt. So
`BACKEND=llama` (the default) serves the same GGUF with the `llama-server` that ships inside
Ollama, as a user systemd unit, with no root needed:

```bash
./llamasrv start | stop | status | logs [-f]   # ./agent starts it by itself when needed
```

It unloads the model from Ollama first (both copies do not fit). `NUM_PARALLEL` slots of `NUM_CTX`
tokens each; context checkpoints (kept in host RAM) let each slot reuse its previous prompt, so a
turn only processes the new tokens. `chat_template.py` patches the model's template to accept the
mid-conversation system messages Claude Code sends. `BACKEND=ollama` switches back (stop the
server first); with Ollama, `./agent setup` checks the systemd `OLLAMA_NUM_PARALLEL` setting.
Requests a server rejects are saved in `logs/requests/failed/`.
`tests/smoke.sh` exercises all of this with a fake `claude` in about a minute.

## Reports

Every run gets `reports/<project>/<time>-[<agent>-]<session>-end.html` (and one per context
compaction); `reports/index.html` lists them and `reports/fleet.html` shows all agents on one
timeline with their share of the GPUs. Each run also gets a diff page,
`reports/<project>/diffs/<run>.html`: every file it changed, line by line, with its commits
(merge-conflict fixes show only the resolution; a run still going shows its work so far,
uncommitted files included). `./diffpage.py --all` rebuilds them for past runs.

**On your network:** `./reportsrv start` serves the reports at `http://<this machine>:8765/`
(user service, starts at login). Pages reload themselves when they change, and opening the index
refreshes it and the running agents' diffs, so there is nothing to sync. It answers only
private/loopback addresses and is read-only. With ufw on, allow the LAN once:
`sudo ufw allow from 192.168.0.0/16 to any port 8765 proto tcp`.

| Layer | Files | Job |
|---|---|---|
| Record | `./agent` (harness lines in `logs/*.jsonl`), `keepalive.py` (`logs/requests/`), `gpumon.py` (`logs/gpu/`), Ollama's journal | write facts as they happen, tagged by agent |
| Read | `telemetry.py` | parse and join them: runs, requests per agent, fair GPU share, VRAM split |
| Render | `reportui.py`, `ctxreport.py`, `fleet.py`, `diffpage.py` | per-run page, index, fleet page, diff pages |
| Serve | `reportsrv.py` (`./reportsrv`) | the folder on the LAN, live |
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
