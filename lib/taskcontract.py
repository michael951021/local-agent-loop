"""Shared, backward-compatible task contract format for prompts and the standup board."""
import re

FIELDS = {"issue": "Issue", "question": "Question", "evidence": "Evidence", "done_when": "Done when",
          "stop": "Stop when", "plan": "Plan"}
GUIDE = """Write these short sections (about 25 lines total):
- `## Issue`: concrete user-visible failure or needed capability; include why it matters.
- `## Question`: one answerable question. Name the behavior/invariant and the observation that would refute your hypothesis.
- `## Evidence`: starting file/symbol or report, then the smallest reproduction/check and where its output will be saved. Distinguish observed facts from guesses.
- `## Done when`: exact acceptance command or observable check, expected result, and regression coverage when behavior changes. It runs on the real inputs, and you run it first. For a measured number also name a known-answer check (hand-computed case, independent reference, planted effect, null control or published value) and the plausible range, written before the run.
- `## Stop when`: scope exclusions and a concrete stop/split condition. After two attempts with no new evidence, record the blocker and stop or split; do not repeat unchanged work.
- `## Plan`: a few steps for this question only.
For security work name the authorized target, relevant trust boundary, and the evidence that distinguishes a real violation from intended behavior. A framework or scanner existing is not evidence that a finding was reproduced.
Keep the contract truthful as evidence changes. Existing three-section specs can be extended when picked up; missing fields do not block scheduling."""


def parse(text):
    sections, current = {}, None
    for line in text.splitlines():
        heading = re.match(r"^#+\s+(.+?)\s*$", line)
        if heading:
            current = heading.group(1).lower()
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line)
    aliases = {"done_when": ("done when", "done-when", "done", "completion", "tests"),
               "plan": ("plan", "solution plan", "solution")}
    result = {}
    for key, label in FIELDS.items():
        result[key] = next((value for name in aliases.get(key, (label.lower(),))
                            if (value := "\n".join(sections.get(name, [])).strip())), "")
    if not any(result.values()):
        result["issue"] = "\n".join(text.splitlines()[:15]).strip()
    return result


def gaps(spec):
    return [label for key, label in FIELDS.items() if not (spec or {}).get(key)]
