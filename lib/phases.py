#!/usr/bin/env python3
"""Every point where the loop calls the model: what for, how often, and why it is its own call.

  phases.py            print the registry (the table in docs/LLM_CALLS.md is generated from it)
  phases.py --md       the registry as Markdown
  phases.py --measure [SINCE_HOURS]   calls, tokens and GPU time per phase from logs/requests/

keepalive.py tags every request it relays with classify(), so reports can count calls per phase.
Stdlib only: keepalive.py imports this.
"""
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"


@dataclass(frozen=True)
class Phase:
    key: str
    name: str
    layer: str      # harness (Claude Code runs) or pipeline (the project's own scripts)
    when: str       # what triggers it
    count: str      # how many calls, per what
    produces: str   # the information it yields
    separate: str   # why it cannot be folded into another call (or what folding was done)


PHASES = [
    Phase("agent", "Agent turn", "harness", "every step of a task run (Claude Code's tool loop)",
          "15–100 per run", "the next tool call, or the run's final message",
          "Each step needs the result of the previous tool call, so the loop is sequential by nature. Its cost is "
          "the re-read prompt, which the server serves from cache (~95% reused)."),
    Phase("final", "Final message + outcome", "harness", "the last turn of a run",
          "1 per run (part of the agent turns)", "one-line summary plus the RESULT block: done or not, why, "
          "what would have helped, handoff for a successor",
          "Folded: this used to be a separate debrief call that re-read the whole session after it ended. The "
          "agent now writes it in its last turn, when everything is still in context."),
    Phase("compact", "Compaction", "harness", "the context nears the window (~window − 33k tokens)",
          "0–4 per run", "a summary that replaces the conversation",
          "Triggered by size mid-run and needs the whole context as input; a larger window (128k per agent) "
          "is what reduces it."),
    Phase("debrief", "Debrief (fallback)", "harness", "a run ended normally but its final message has no RESULT block",
          "0–1 per run, rare", "why the task was not finished, what would have helped",
          "Only a fallback for the folded final message; resumes the session with no tools and 1 turn."),
    Phase("handoff", "Handoff", "harness", "a run was interrupted (./agent stop --now, kill, crash)",
          "0–1 per interrupted run", "2 lines for the agent that continues: what is done, what is left",
          "The interrupted run was cut off mid-generation, so it could not write this itself."),
    Phase("merge", "Merge-conflict run", "harness", "the branch still conflicts with the main branch after the run",
          "rare (agents now merge the main branch before they end)", "a resolved merge",
          "Folded where possible: the agent merges the main branch itself before ending, with its change in "
          "context. A separate run remains only when another agent merged something conflicting in between."),
    Phase("side", "Claude Code side call", "harness", "Claude Code's own small-model requests",
          "measured", "housekeeping (e.g. titles, command checks)",
          "Not ours; they go to the same 27B model because every model name maps to it. Measured so they can "
          "be switched off if they cost GPU time."),
    Phase("claims", "Claim judge", "pipeline", "scripts/claims.py: a comment the rules call ambiguous",
          "1 per ambiguous comment", "claim / not_claim",
          "Candidate for folding: one structured call per issue can judge all its comments and tags at once "
          "(TODO: one issue-judge call)."),
    Phase("maint", "Maintainer-status judge", "pipeline", "scripts/maint.py: an ambiguous maintainer comment",
          "1 per ambiguous maintainer comment", "fixed / blocked / intentional / awaiting / needs_discussion / none",
          "Same issue as the claim judge: folded into the per-issue call."),
    Phase("relevance", "Relevance tagger", "pipeline", "scripts/relevance.py: an issue no rule tagged",
          "1 per untagged issue", "subsystem tags with reasons",
          "Same issue, same input (title + body): folded into the per-issue call."),
    Phase("judge", "Issue judge", "pipeline", "scripts/judge.py: an issue with anything the rules left ambiguous",
          "1 per such issue (replaces claims + maint + relevance calls)", "claim and maintainer verdicts for "
          "every ambiguous comment, plus relevance tags",
          "One structured (JSON-schema) call per issue: all three questions read the same thread."),
    Phase("pipeline", "Other project call", "pipeline", "any other scripts/ call (writer, coder, benchmarks)",
          "measured", "varies", "Listed here once they run in the loop."),
]
BY_KEY = {p.key: p for p in PHASES}


def _prompt_head(name):
    try:
        return " ".join((PROMPTS / name).read_text().split())[:60]
    except OSError:
        return None


HANDOFF_HEAD, DEBRIEF_HEAD = _prompt_head("handoff.md"), _prompt_head("debrief.md")
COMPACT = re.compile(r"(create a detailed summary of the conversation|summary of the conversation so far|"
                     r"Your task is to create a detailed summary)", re.I)
PIPELINE_SYSTEM = [("claims", re.compile(r"claim", re.I)), ("maint", re.compile(r"maintainer", re.I)),
                   ("relevance", re.compile(r"subsystem|area|relevan", re.I))]


def _text(content):
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def classify(path, body):
    """(phase key, summary dict) for one request body (bytes)."""
    try:
        d = json.loads(body or b"{}")
    except ValueError:
        return "unknown", {}
    msgs = d.get("messages") or []
    system = d.get("system")
    system = _text(system) if not isinstance(system, str) else system
    if "chat/completions" in path:   # the project's scripts (OpenAI API)
        system = next((_text(m.get("content")) for m in msgs if m.get("role") == "system"), "")
        s = {"msgs": len(msgs), "max_tokens": d.get("max_tokens"), "sys": " ".join(system.split())[:80]}
        caller = d.get("metadata", {}).get("phase") if isinstance(d.get("metadata"), dict) else None
        if caller in BY_KEY:
            return caller, s
        return next((k for k, rx in PIPELINE_SYSTEM if rx.search(system[:400])), "pipeline"), s
    last = next((m for m in reversed(msgs) if m.get("role") == "user"), {})
    last_text = " ".join(_text(last.get("content")).split())
    s = {"msgs": len(msgs), "tools": len(d.get("tools") or []), "max_tokens": d.get("max_tokens"),
         "sys": " ".join((system or "").split())[:80]}
    # Claude Code prepends <system-reminder> blocks to a user message, so look for the prompt anywhere in it.
    if HANDOFF_HEAD and HANDOFF_HEAD[:40] in last_text:
        return "handoff", s
    if DEBRIEF_HEAD and DEBRIEF_HEAD[:40] in last_text:
        return "debrief", s
    if COMPACT.search(last_text[-4000:]) and not s["tools"] or COMPACT.search((system or "")[:2000]):
        return "compact", s
    if s["tools"] == 0 and (s["msgs"] <= 2 or "Claude Code" not in (system or "")[:400]):
        s["hint"] = last_text[:80]
        return "side", s
    return "agent", s


def measure(since_h=24):
    import time
    t0 = time.time() - since_h * 3600
    agg = {}
    for f in sorted((ROOT / "logs" / "requests").glob("*.jsonl")):
        for line in open(f):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("t0", 0) < t0 or "phase" not in r:
                continue
            a = agg.setdefault(r["phase"], {"calls": 0, "prompt": 0, "out": 0, "sec": 0.0})
            a["calls"] += 1
            a["prompt"] += sum(r.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                     "cache_creation_input_tokens", "prompt_tokens"))
            a["out"] += r.get("output_tokens", 0) + r.get("completion_tokens", 0)
            a["sec"] += r.get("t1", r["t0"]) - r["t0"]
    return agg


def markdown():
    out = ["| Phase | Layer | When | Calls | Yields | Why its own call |", "|---|---|---|---|---|---|"]
    for p in PHASES:
        out.append(f"| **{p.name}** (`{p.key}`) | {p.layer} | {p.when} | {p.count} | {p.produces} | {p.separate} |")
    return "\n".join(out)


if __name__ == "__main__":
    if "--measure" in sys.argv:
        i = sys.argv.index("--measure")
        h = float(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 24
        agg = measure(h)
        tot = sum(a["sec"] for a in agg.values()) or 1
        print(f"{'phase':<11}{'calls':>7}{'prompt tok':>12}{'out tok':>10}{'GPU min':>9}{'share':>7}   (last {h:g} h)")
        for k, a in sorted(agg.items(), key=lambda kv: -kv[1]["sec"]):
            print(f"{k:<11}{a['calls']:>7}{a['prompt']:>12}{a['out']:>10}{a['sec'] / 60:>9.1f}{100 * a['sec'] / tot:>6.1f}%")
    else:
        print(markdown())
