#!/usr/bin/env bash
# SessionStart hook: its stdout is added to the agent's context at the start of every session.
exec python3 /opt/agent/hooks/session_context.py
