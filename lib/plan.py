#!/usr/bin/env python3
"""The plan as a tree: goal → sections → subsections → tasks, each with a one-line "why".

  plan.py tree  FILE            print the tree (titles and whys)
  plan.py lint  FILE            problems: open tasks or sections without a why, whys over the word limit
  plan.py path  FILE LINE       JSON breadcrumb from the goal down to the task on LINE

TODO.md (and checklist files) in this shape:

  # Goal: merged upstream fixes to ML-infra repos        ← root (one # heading)
  > why: what done looks like, in one or two lines
  ## Candidate funnel                                    ← sections nest by heading level
  > why: how this section serves its parent (≤ 45 words)
  - [ ] **Short title** — spec (id: x) (after: y)
    why: how this task serves its section (≤ 30 words)

The why lines are what the agent sees above its task ("Where this fits") and what reports show, so a
task can be judged by whether it moved its section forward, not only whether its line got checked.
"""
import json
import re
import sys
from typing import Optional

from pydantic import BaseModel, Field, field_validator

SECTION_WORDS, TASK_WORDS = 45, 30
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
TASK = re.compile(r"^\s*- \[( |x|X)\] (.*)$")
SECTION_WHY = re.compile(r"^\s*>\s*why:\s*(.*)$", re.I)
QUOTE = re.compile(r"^\s*>\s?(.*)$")
TASK_WHY = re.compile(r"^\s+why:\s*(.*)$", re.I)
TITLE = re.compile(r"^\*\*(.+?)\*\*\s*(?:—\s*)?(.*)$")
TAGS = re.compile(r"\s*\((?:id|after|checklist): *[^)]*\)")


class Task(BaseModel):
    line: int
    done: bool
    text: str
    why: Optional[str] = None

    @property
    def title(self) -> str:
        m = TITLE.match(self.text)
        return m.group(1).strip() if m else TAGS.sub("", self.text).split(":")[0][:70].strip()


class Section(BaseModel):
    title: str
    level: int
    line: int
    why: Optional[str] = None
    tasks: list[Task] = Field(default_factory=list)
    children: list["Section"] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def _clean(cls, v: str) -> str:
        return re.sub(r"^Goal:\s*", "", v).strip()

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def has_open(self) -> bool:
        return any(not t.done for s in self.walk() for t in s.tasks)


class Step(BaseModel):
    """One level of a breadcrumb."""
    kind: str      # goal | section | milestone | task
    title: str
    why: Optional[str] = None


class Plan(BaseModel):
    root: Section

    @classmethod
    def parse(cls, text: str) -> "Plan":
        root = Section(title="(no goal heading)", level=0, line=0)
        stack, last, in_comment = [root], None, False   # last: the heading or task a why line attaches to
        for i, raw in enumerate((text or "").splitlines(), 1):
            line = raw
            if in_comment or line.lstrip().startswith("<!--"):
                in_comment = "-->" not in line
                continue
            h = HEADING.match(line)
            if h:
                level, title = len(h.group(1)), h.group(2)
                if level == 1 and root.line == 0 and not root.tasks and not root.children:
                    root.title, root.level, root.line = Section._clean(title), 1, i
                    last = root
                    continue
                while len(stack) > 1 and stack[-1].level >= level:
                    stack.pop()
                sec = Section(title=title, level=level, line=i)
                stack[-1].children.append(sec)
                stack.append(sec)
                last = sec
                continue
            t = TASK.match(line)
            if t:
                task = Task(line=i, done=t.group(1) != " ", text=t.group(2))
                stack[-1].tasks.append(task)
                last = task
                continue
            w = SECTION_WHY.match(line) if isinstance(last, Section) else TASK_WHY.match(line)
            if w and last is not None and last.why is None:
                last.why = w.group(1).strip()
                continue
            q = QUOTE.match(line) if isinstance(last, Section) else re.match(r"^\s{2,}(\S.*)$", line)
            if q and last is not None and last.why and q.group(1).strip():   # a why continued on the next line
                last.why += " " + q.group(1).strip()
                continue
            if line.strip():
                last = None if not isinstance(last, Section) else last
        return cls(root=root)

    def initiatives(self) -> list[Section]:
        """The top-level sections (## under the # goal). These are the plan's initiatives: each gets a
        colour, and every ticket beneath one carries that colour on the board."""
        return list(self.root.children)

    def initiative_colors(self) -> dict[str, int]:
        """{initiative title: colour slot}, by reading order, so the colours are distinct and stable while
        the set of initiatives holds. A ticket's colour = its top-level ancestor's slot."""
        return {s.title: i for i, s in enumerate(self.root.children)}

    def path(self, line: int) -> list[Step]:
        """Goal → sections → the task on LINE (empty if no task there)."""
        def go(sec, trail):
            here = trail + ([Step(kind="goal" if sec is self.root else "section", title=sec.title, why=sec.why)]
                            if sec.line else [])
            for t in sec.tasks:
                if t.line == line:
                    return here + [Step(kind="task", title=t.title, why=t.why)]
            for c in sec.children:
                r = go(c, here)
                if r:
                    return r
            return None
        return go(self.root, []) or []

    def lint(self) -> list[str]:
        out = []
        words = lambda s: len((s or "").split())
        for sec in self.root.walk():
            if sec.line and sec.has_open():
                if not sec.why:
                    out.append(f"line {sec.line}: section '{sec.title}' has no '> why:' line")
                elif words(sec.why) > SECTION_WORDS:
                    out.append(f"line {sec.line}: section '{sec.title}' why is {words(sec.why)} words (max {SECTION_WORDS})")
            for t in sec.tasks:
                if t.done:
                    continue
                if not t.why:
                    out.append(f"line {t.line}: task '{t.title}' has no 'why:' line under it")
                elif words(t.why) > TASK_WORDS:
                    out.append(f"line {t.line}: task '{t.title}' why is {words(t.why)} words (max {TASK_WORDS})")
        return out


def render(path: list[Step]) -> str:
    """The breadcrumb as the agent sees it."""
    out = []
    for depth, s in enumerate(path):
        label = {"goal": "Goal", "section": "Section", "milestone": "Milestone", "task": "Your task"}.get(s.kind, s.kind)
        out.append(f"{'  ' * depth}{label}: {s.title}" + (f" — {s.why}" if s.why else ""))
    return "\n".join(out)


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("tree", "lint", "path"):
        sys.exit(__doc__)
    p = Plan.parse(open(sys.argv[2]).read())
    if sys.argv[1] == "lint":
        probs = p.lint()
        print("\n".join(probs) or "ok")
        return 1 if probs else 0
    if sys.argv[1] == "path":
        print(json.dumps([s.model_dump() for s in p.path(int(sys.argv[3]))]))
        return 0
    for sec in p.root.walk():
        ind = "  " * max(0, sec.level - 1)
        print(f"{ind}{sec.title}" + (f"  — {sec.why}" if sec.why else ""))
        for t in sec.tasks:
            print(f"{ind}  [{'x' if t.done else ' '}] {t.title}" + (f"  — {t.why}" if t.why else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
