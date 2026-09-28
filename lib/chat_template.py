#!/usr/bin/env python3
"""Print a GGUF's chat template, patched for Claude Code (used by ./llamasrv).

  chat_template.py MODEL.gguf > template.jinja

Claude Code sends hook output (e.g. SessionStart) as a `system` message in the middle of the
conversation. Qwen's template raises "System message must be at the beginning." for that; the
patched template renders it as its own system turn instead. Everything else is unchanged.
"""
import re
import struct
import sys

SCALAR = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}


def read_template(path):
    with open(path, "rb") as f:
        def s():
            n, = struct.unpack("<Q", f.read(8))
            return f.read(n)

        def skip(t):
            if t in SCALAR:
                f.seek(struct.calcsize(SCALAR[t]), 1)
            elif t == 8:
                s()
            elif t == 9:
                at, n = struct.unpack("<IQ", f.read(12))
                if at in SCALAR:
                    f.seek(struct.calcsize(SCALAR[at]) * n, 1)
                else:
                    for _ in range(n):
                        skip(at)

        magic, _, _, nkv = struct.unpack("<4sIQQ", f.read(24))
        if magic != b"GGUF":
            sys.exit(f"{path}: not a GGUF file")
        for _ in range(nkv):
            k = s().decode()
            t, = struct.unpack("<I", f.read(4))
            if k == "tokenizer.chat_template" and t == 8:
                return s().decode()
            skip(t)
    sys.exit(f"{path}: no chat template")


tpl = read_template(sys.argv[1])
raise_ = re.compile(r"\{%-? if not loop\.first %\}\s*\{\{-? raise_exception\('System message must be at the beginning\.'\) \}\}\s*\{%-? endif %\}")
render = r"{%- if not loop.first %}{{- '<|im_start|>system\n' + content + '<|im_end|>\n' }}{%- endif %}"
patched, n = raise_.subn(lambda m: render, tpl)
if n != 1:
    print("chat_template.py: system-message check not found; template left unchanged", file=sys.stderr)
sys.stdout.write(patched)
