#!/usr/bin/env python3
"""Does llama-server with N slots finish more loop work than one slot? (run against a live server)

  tests/llama_bench.py URL [--ctx 30000] [--gen 300] [--quick]

1. throughput: the same request (fresh CTX-token prompt, GEN tokens out) run 1 at a time vs 2 at once
2. decode:     generation only (prompt already cached), 1 vs 2 at once
3. reuse:      a Claude-Code-shaped conversation over /v1/messages (growing history, thinking blocks)
               — how much of each prompt comes from the cache
"""
import argparse
import json
import random
import threading
import time
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("url")
ap.add_argument("--ctx", type=int, default=30000)
ap.add_argument("--gen", type=int, default=300)
ap.add_argument("--quick", action="store_true", help="skip the conversation test")
a = ap.parse_args()
WORDS = ("slot cache token merge worker branch issue label commit review sync timeline prompt "
         "server model layer thread queue report agent config state batch").split()


def post(path, body):
    req = urllib.request.Request(a.url + path, json.dumps(body).encode(), {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=3600) as r:
        return json.load(r)


def text(n_tokens, seed):
    rnd = random.Random(seed)
    return " ".join(rnd.choice(WORDS) + str(rnd.randint(0, 99)) for _ in range(n_tokens // 3))


def complete(prompt, n, cache=False):
    t = time.time()
    r = post("/completion", {"prompt": prompt, "n_predict": n, "cache_prompt": cache, "ignore_eos": True,
                             "temperature": 1.0, "top_k": 20, "top_p": 0.95})
    tm = r["timings"]
    return {"wall": time.time() - t, "prompt_n": tm["prompt_n"], "prompt_ms": tm["prompt_ms"],
            "gen_n": tm["predicted_n"], "gen_ms": tm["predicted_ms"], "cache_n": tm.get("cache_n", 0)}


def together(fns):
    out = [None] * len(fns)
    th = [threading.Thread(target=lambda i=i, f=f: out.__setitem__(i, f())) for i, f in enumerate(fns)]
    t = time.time()
    [x.start() for x in th]
    [x.join() for x in th]
    return time.time() - t, out


print(f"1. throughput: fresh {a.ctx}-token prompt + {a.gen} tokens out")
seq = [complete(text(a.ctx, s), a.gen) for s in (1, 2)]
seq_wall = sum(r["wall"] for r in seq)
par_wall, par = together([lambda s=s: complete(text(a.ctx, s), a.gen) for s in (3, 4)])
pp1 = seq[0]["prompt_n"] / seq[0]["prompt_ms"] * 1000
tg1 = seq[0]["gen_n"] / seq[0]["gen_ms"] * 1000
print(f"   one at a time: 2 requests in {seq_wall:6.1f} s  (prefill {pp1:.0f} tok/s, generate {tg1:.1f} tok/s)")
print(f"   two at once:   2 requests in {par_wall:6.1f} s  -> {seq_wall / par_wall:.2f}x")

print(f"2. decode only: {a.gen * 2} tokens out, prompt cached")
base = [text(2000, s) for s in (5, 6)]
for b in base:
    complete(b, 1, cache=True)
r1 = [complete(b, a.gen * 2, cache=True) for b in base]
w1 = sum(r["wall"] for r in r1)
w2, _ = together([lambda b=b: complete(b, a.gen * 2, cache=True) for b in base])
print(f"   one at a time: {w1:6.1f} s ({r1[0]['gen_n'] / r1[0]['gen_ms'] * 1000:.1f} tok/s)   two at once: {w2:6.1f} s -> {w1 / w2:.2f}x")

print("2b. writing code (realistic text, matters for speculative decoding): 600 tokens out")
CODE = ["Write a complete Python module that parses GitHub issue timeline events (assigned, closed, "
        "cross-referenced) from JSON into dataclasses, with docstrings and a unittest suite.",
        "Write a complete Python CLI with argparse that syncs a SQLite table of issues from a JSON file, "
        "with upserts, a --dry-run flag, logging, and tests."]


def code(q, think=False):
    t = time.time()
    r = post("/v1/chat/completions", {"messages": [{"role": "user", "content": q}], "max_tokens": 600,
                                      "chat_template_kwargs": {"enable_thinking": think}})
    tm = r["timings"]
    return {"wall": time.time() - t, "tps": tm["predicted_n"] / tm["predicted_ms"] * 1000, "n": tm["predicted_n"],
            "draft": (tm.get("draft_n_accepted"), tm.get("draft_n"))}


c1 = [code(q) for q in CODE]
cw1 = sum(r["wall"] for r in c1)
cw2, c2 = together([lambda q=q: code(q) for q in CODE])
print(f"   one at a time: {cw1:6.1f} s ({c1[0]['tps']:.1f} tok/s, draft accepted/drafted {c1[0]['draft']})"
      f"   two at once: {cw2:6.1f} s -> {cw1 / cw2:.2f}x, {sum(r['n'] for r in c2) / cw2:.1f} tok/s total")

print("2c. thinking on (most of what agents generate): 600 tokens out")
k1 = [code(q, True) for q in CODE]
kw1 = sum(r["wall"] for r in k1)
kw2, k2 = together([lambda q=q: code(q, True) for q in CODE])
print(f"   one at a time: {kw1:6.1f} s ({k1[0]['tps']:.1f} tok/s, draft accepted/drafted {k1[0]['draft']})"
      f"   two at once: {kw2:6.1f} s -> {kw1 / kw2:.2f}x, {sum(r['n'] for r in k2) / kw2:.1f} tok/s total")

if a.quick:
    raise SystemExit
print("3. prompt reuse over a Claude-Code-shaped conversation (/v1/messages, thinking on)")
system = "You are a coding agent. " + text(12000, 7)
msgs = [{"role": "user", "content": "Task: " + text(3000, 8)}]
for turn in range(6):
    t = time.time()
    r = post("/v1/messages", {"model": "x", "max_tokens": 200, "system": system, "messages": msgs,
                              "thinking": {"type": "enabled", "budget_tokens": 100}})
    got = r.get("usage", {})
    blocks = r.get("content", [])
    msgs.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "ok"}]})
    msgs.append({"role": "user", "content": "Tool output: " + text(1500, 100 + turn)})
    print(f"   turn {turn}: input {got.get('input_tokens')}, cache_read {got.get('cache_read_input_tokens')}, "
          f"{time.time() - t:5.1f} s")
