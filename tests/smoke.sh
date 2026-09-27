#!/usr/bin/env bash
# Harness smoke test with a fake claude (tests/fake_claude.py): no model, about a minute.
#   tests/smoke.sh          run all checks, then delete the test projects
#   KEEP=1 tests/smoke.sh   keep projects, logs and reports for a look
# Checks: 2 agents on one project (claims, (after:) dependencies, worktrees, shared dir, merge
# driver, a real merge conflict resolved by the agent), 1 agent in place (retries, giving up),
# two single-agent loops on two projects at once, and that every run gets a report.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
A="$ROOT/agent"
export AGENT_CLAUDE_BIN="$ROOT/tests/fake_claude.py" MAX_TASK_ATTEMPTS=2 STUCK_LIMIT=0 ITER_TIMEOUT=5m
P=zz-smoke
fails=0
ok()   { echo -e "  \033[32m✔\033[0m $*"; }
bad()  { echo -e "  \033[31m✖\033[0m $*"; fails=$((fails + 1)); }
check() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$what"; else bad "$what"; fi; }

cleanup() {
  tmux kill-session -t "agent-$P-par" 2>/dev/null
  for n in "$ROOT"/projects/$P-*; do
    [[ -d "$n" ]] || continue
    n="$(basename "$n")"
    rm -rf "$ROOT/projects/$n" "$ROOT/work/$n" "$ROOT/run/$n" "$ROOT/reports/$n" "$ROOT"/logs/"$n"-*.jsonl
  done
  python3 "$ROOT/ctxreport.py" --index 2>/dev/null
}
cleanup
new() {   # new NAME TODO_BODY
  "$A" new "$1" >/dev/null
  printf '# Tasks\n%s\n' "$2" > "$ROOT/projects/$1/TODO.md"
  git -C "$ROOT/projects/$1" -c user.name=t -c user.email=t@t commit -qam "test tasks"
}
wait_idle() {   # wait until NAME has no live agents (max 180 s)
  local i; for i in $(seq 180); do
    if grep -h '"subtype":"init"' "$ROOT"/logs/"$1"-*.jsonl 2>/dev/null | grep -vq '"model":"fake"'; then
      "$A" stop "$1" --now; bad "a real model session started (the fake claude was not used) — stopped"; return 1
    fi
    [[ -z "$(ls "$ROOT/run/$1/workers/"*.pid 2>/dev/null | while read -r f; do kill -0 "$(<"$f")" 2>/dev/null && echo x; done)" ]] && return 0
    sleep 1
  done; return 1
}
reports() { ls "$ROOT/reports/$1/"*-end.json 2>/dev/null | wc -l; }
runs()    { cat "$ROOT"/logs/"$1"-*.jsonl 2>/dev/null | grep -c '^{"type":"harness","event":"end"'; }

echo "1. two agents, one project"
new "$P-par" '- [ ] X: clash one [file clash.txt] [sleep 3] (id: x) (after: -)
- [ ] Y: clash two [file clash.txt] [sleep 6] (id: y) (after: -)
- [ ] A: first [file a.txt] (id: a) (after: -)
- [ ] C: needs a and x [file c.txt] (after: a, x)
- [ ] Z: last, waits for every task above [file z.txt]'
d="$ROOT/projects/$P-par"
printf 'shared/\n' >> "$d/.gitignore"; printf 'shared\n' > "$d/.agent-shared"
git -C "$d" add -A && git -C "$d" -c user.name=t -c user.email=t@t commit -qm "shared dir"
env -u TMUX "$A" loop "$P-par" -j 2 </dev/null
sleep 2
wait_idle "$P-par" || bad "agents did not finish in time"
"$A" status "$P-par" | sed 's/^/    /'
check "every task checked on the project branch" bash -c "! grep -q '^- \[ \]' '$d/TODO.md'"
check "no conflict markers anywhere" bash -c "! grep -rq '^<<<<<<<' '$d' --include=*.md --include=*.txt"
check "clash.txt kept both agents' lines" bash -c "grep -q X '$d/clash.txt' && grep -q Y '$d/clash.txt'"
check "TASKLOG.md has all 5 entries" bash -c "[[ \$(grep -c '^- ' '$d/TASKLOG.md') -ge 5 ]]"
check "both agents did work" bash -c "grep -q '^w1 ' '$d/shared/seen' && grep -q '^w2 ' '$d/shared/seen'"
check "a merge conflict happened and was resolved" bash -c "grep -h '\"merged\":\"conflict\"' '$ROOT'/logs/$P-par-*.jsonl"
check "C ran only after A and X were merged" bash -c "
  git -C '$d' log --format=%s master | grep -n 'fake: [ACX]$' | tac | cut -d' ' -f2 | tr -d '\n' | grep -q 'C$'"
check "worktrees are clean and on their branches" bash -c "git -C '$ROOT/work/$P-par/w1' diff --quiet && git -C '$ROOT/work/$P-par/w2' diff --quiet"
sleep 3
check "one end report per run ($(runs "$P-par") runs)" bash -c "[[ $(reports "$P-par") -eq $(runs "$P-par") ]]"
check "reports name the agent" bash -c "ls '$ROOT/reports/$P-par/' | grep -q -- '-w1-' && ls '$ROOT/reports/$P-par/' | grep -q -- '-w2-'"
check "fleet page lists both agents" bash -c "grep -q '$P-par/w1' '$ROOT/reports/fleet.html' && grep -q '$P-par/w2' '$ROOT/reports/fleet.html'"

echo "2. one agent in place, a task that keeps failing"
new "$P-one" '- [ ] P: works [file p.txt] [sleep 1]
- [ ] Q: always fails [fail] [sleep 1]
- [ ] R: after the failing one [file r.txt]'
out="$("$A" loop "$P-one" -j 1 </dev/null 2>&1)"
check "P done in place (no worktree)" bash -c "grep -q '^- \[x\] P' '$ROOT/projects/$P-one/TODO.md' && [[ ! -d '$ROOT/work/$P-one' ]]"
check "Q retried with an attempt-2 prompt, then given up" grep -q "gave up after 2 attempts" <<<"$out"
check "R not started: it depends on Q" bash -c "grep -q '^- \[ \] R' '$ROOT/projects/$P-one/TODO.md'"
check "loop ended on the blocked dependency" grep -q "depend on tasks that were given up" <<<"$out"

echo "3. two single-agent loops on two projects at once"
new "$P-a" '- [ ] A1: one [file a1] [sleep 4]
- [ ] A2: two [file a2] [sleep 2]'
new "$P-b" '- [ ] B1: one [file b1] [sleep 4]
- [ ] B2: two [file b2] [sleep 2]'
"$A" loop "$P-a" -j 1 </dev/null >"$ROOT/run/$P-a.out" 2>&1 &
"$A" loop "$P-b" -j 1 </dev/null >"$ROOT/run/$P-b.out" 2>&1 &
wait
check "both projects finished" bash -c "! grep -q '^- \[ \]' '$ROOT/projects/$P-a/TODO.md' && ! grep -q '^- \[ \]' '$ROOT/projects/$P-b/TODO.md'"
check "they overlapped in time" bash -c "grep -h '\"event\":\"start\"' '$ROOT'/logs/$P-a-*.jsonl | head -1 | jq -e --argjson b \$(grep -h '\"event\":\"end\"' '$ROOT'/logs/$P-b-*.jsonl | head -1 | jq .t) '.t < \$b'"
rm -f "$ROOT/run/$P-a.out" "$ROOT/run/$P-b.out"
sleep 3
check "reports for both projects" bash -c "[[ $(reports "$P-a") -ge 2 && $(reports "$P-b") -ge 2 ]]"

[[ -n "${KEEP:-}" ]] || cleanup
if ((fails)); then echo -e "\033[31m$fails check(s) failed\033[0m"; exit 1; fi
echo -e "\033[32mall checks passed\033[0m"
