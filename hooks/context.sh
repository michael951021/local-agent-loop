#!/usr/bin/env bash
# SessionStart hook: its stdout is added to the agent's context at the start of every session.
cd /work 2>/dev/null || exit 0
echo "## Current state of /work"
echo '### Recent commits'
git log --oneline -5 2>/dev/null
echo '### Uncommitted changes'
git status --short 2>/dev/null | head -20
if [[ -f TODO.md ]]; then
  echo '### Open tasks'
  grep -E '^\s*- \[ \]' TODO.md | head -10
fi
