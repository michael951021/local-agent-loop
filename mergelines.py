#!/usr/bin/env python3
"""Git merge driver for the files every agent edits: TODO.md, NOTES.md, TASKLOG.md.

  mergelines.py BASE OURS THEIRS      (git: driver = python3 mergelines.py %O %A %B)

A line-based 3-way merge like git's, with two differences that suit these files:
- changes to adjacent lines merge cleanly (two agents checking off neighbouring TODO lines);
- when both sides change the same lines, both versions are kept (ours, then the lines of theirs
  that ours lacks) instead of writing conflict markers. For TASKLOG.md that is two appended
  entries; for NOTES.md the next agent condenses the duplicate (loop.md tells it to).
Writes the result to OURS and always exits 0.
"""
import sys
from difflib import SequenceMatcher


def hunks(base, side):
    """[(i1, i2, new_lines)]: base[i1:i2] becomes new_lines."""
    sm = SequenceMatcher(None, base, side, autojunk=False)
    return [(i1, i2, side[j1:j2]) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"]


def overlap(a, b):
    (a1, a2, _), (b1, b2, _) = a, b
    if a1 == a2 and b1 == b2:
        return a1 == b1                      # two insertions at the same point
    if a1 == a2:
        return b1 < a1 < b2                  # insertion strictly inside the other change
    if b1 == b2:
        return a1 < b1 < a2
    return a1 < b2 and b1 < a2


def apply(base, lo, hi, hs):
    out, i = [], lo
    for h1, h2, new in sorted(hs, key=lambda h: (h[0], h[1])):
        out += base[i:h1] + new
        i = h2
    return out + base[i:hi]


def merge(base, ours, theirs):
    tagged = sorted([(h, 0) for h in hunks(base, ours)] + [(h, 1) for h in hunks(base, theirs)],
                    key=lambda x: (x[0][0], x[0][1]))
    clusters = []
    for h, side in tagged:
        if clusters and any(overlap(h, g) for g, _ in clusters[-1]):
            clusters[-1].append((h, side))
        else:
            clusters.append([(h, side)])
    out, i = [], 0
    for cl in clusters:
        lo, hi = min(h[0] for h, _ in cl), max(h[1] for h, _ in cl)
        mine = apply(base, lo, hi, [h for h, s in cl if s == 0])
        other = apply(base, lo, hi, [h for h, s in cl if s == 1])
        sides = {s for _, s in cl}
        out += base[i:lo]
        if sides == {0}:
            out += mine
        elif sides == {1}:
            out += other
        else:
            out += mine + [ln for ln in other if ln not in mine]
        i = hi
    return out + base[i:]


def main():
    base, ours, theirs = (open(p, encoding="utf-8", errors="surrogateescape").read().splitlines(keepends=True)
                          for p in sys.argv[1:4])
    for side in (base, ours, theirs):          # a missing final newline must not glue two lines together
        if side and not side[-1].endswith("\n"):
            side[-1] += "\n"
    with open(sys.argv[2], "w", encoding="utf-8", errors="surrogateescape") as f:
        f.writelines(merge(base, ours, theirs))


if __name__ == "__main__":
    main()
