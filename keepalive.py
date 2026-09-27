#!/usr/bin/env python3
"""HTTP proxy to Ollama that keeps slow SSE streams alive.

  keepalive.py PORT HOST:PORT [--tag TAG] [--ollama-shim MODEL]
                                           listen on 127.0.0.1:PORT (0 = pick one; printed first)

Ollama sends a tool_use block only once the whole call is generated, so a long Write can leave the
stream silent for minutes and trip Claude Code's 300 s idle watchdog. While the upstream request is
still open, this inserts Anthropic `ping` events (ignored by the client) between complete events.
Pings never complete a block or a message, so nothing runs early. They stop after MAX_AGE seconds,
so a request that is truly stuck still times out.

Every /v1/messages request is also appended to logs/requests/YYYYMMDD.jsonl with TAG (the agent it
belongs to), its timing and token usage, so reports can attribute Ollama's GPU time per agent. The bodies of requests the server rejects
(status >= 400) are kept in logs/requests/failed/ (the last 20).

With --ollama-shim (the upstream is llama-server), Ollama's metadata endpoints /api/tags, /api/ps and
/api/version are answered here with MODEL, so project scripts that check the model the Ollama way
keep working; everything else (/v1/...) goes to llama-server.
"""
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

IDLE = 20        # seconds of upstream silence before a ping
MAX_AGE = 1800   # no pings for requests older than this
PING = b'event: ping\ndata: {"type": "ping"}\n\n'
LEDGER = Path(__file__).resolve().parent / "logs" / "requests"
USAGE = re.compile(rb'"(input_tokens|output_tokens|cache_read_input_tokens|cache_creation_input_tokens)":\s*(\d+)')


def log(msg):
    print(time.strftime("%F %T"), msg, file=sys.stderr, flush=True)


async def read_head(reader):
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return lines[0], lines[1:-2], headers


def rewrite(first, lines, drop):
    kept = [l for l in lines if l.split(":", 1)[0].strip().lower() not in drop]
    return ("\r\n".join([first, *kept, "Connection: close"]) + "\r\n\r\n").encode("latin-1")


async def unchunk(reader):
    """Yield the decoded body of a chunked response."""
    while True:
        size = int((await reader.readline()).split(b";")[0], 16)
        if size == 0:
            return
        yield await reader.readexactly(size)
        await reader.readexactly(2)


def record(entry):
    try:
        LEDGER.mkdir(parents=True, exist_ok=True)
        with open(LEDGER / time.strftime("%Y%m%d.jsonl"), "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log(f"ledger: {e!r}")


def save_failed(body, entry):
    try:
        d = LEDGER / "failed"
        d.mkdir(parents=True, exist_ok=True)
        old = sorted(d.glob("*.json"))
        for f in old[:-20]:   # keep the last 20
            f.unlink()
        (d / f"{time.strftime('%Y%m%d-%H%M%S')}-{entry['status']}-{entry['tag'].replace('/', '_')}.json").write_bytes(body)
    except OSError as e:
        log(f"failed-request dump: {e!r}")


async def relay_sse(up_r, headers, client_w, start, usage):
    body = unchunk(up_r) if "chunked" in headers.get("transfer-encoding", "") else None
    pending, pings = b"", 0
    nxt = None
    while True:
        if nxt is None:
            nxt = asyncio.ensure_future(anext(body) if body else up_r.read(65536))
        done, _ = await asyncio.wait({nxt}, timeout=IDLE)
        if not done:
            # Only between complete events, and only while the request is young enough.
            if not pending and time.monotonic() - start < MAX_AGE:
                client_w.write(PING)
                await client_w.drain()
                pings += 1
            continue
        try:
            data = nxt.result()
        except (StopAsyncIteration, asyncio.IncompleteReadError):
            data = b""
        nxt = None
        if not data:
            break
        pending += data
        cut = pending.rfind(b"\n\n") + 2
        if cut >= 2:
            if b"message_start" in pending[:cut] or b"message_delta" in pending[:cut]:
                for k, v in USAGE.findall(pending[:cut]):
                    usage[k.decode()] = int(v)
            client_w.write(pending[:cut])
            pending = pending[cut:]
            await client_w.drain()
    client_w.write(pending)
    return pings


def shim(path, model):
    """Ollama-style answers for llama-server (see --ollama-shim)."""
    m = {"name": f"{model}:latest", "model": f"{model}:latest", "size": 0, "size_vram": 0,
         "details": {"family": "qwen35", "format": "gguf"}, "capabilities": ["completion", "tools", "thinking"]}
    return {"/api/tags": {"models": [m]}, "/api/ps": {"models": [m]},
            "/api/version": {"version": "llama-server"}}.get(path)


async def handle(client_r, client_w, upstream, tag, model=None):
    up_w, entry = None, None
    try:
        first, lines, headers = await read_head(client_r)
        body = await client_r.readexactly(int(headers.get("content-length", 0)))
        answer = model and shim(first.split(" ")[1].split("?")[0], model)
        if answer:
            data = json.dumps(answer).encode()
            client_w.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                           b"Connection: close\r\n\r\n" % len(data) + data)
            await client_w.drain()
            return
        start = time.monotonic()
        if "/v1/messages" in first and "count_tokens" not in first:
            entry = {"tag": tag, "t0": round(time.time(), 3), "path": first.split(" ")[1].split("?")[0]}
        up_r, up_w = await asyncio.open_connection(*upstream)
        up_w.write(rewrite(first, lines, {"connection", "keep-alive"}) + body)
        await up_w.drain()

        status, lines, headers = await read_head(up_r)
        if entry:
            entry["status"], entry["ttfb"] = int(status.split(" ")[1]), round(time.monotonic() - start, 3)
            if entry["status"] >= 400:   # keep the request, to see what the server choked on
                save_failed(body, entry)
        sse = headers.get("content-type", "").startswith("text/event-stream")
        # SSE is re-sent de-chunked and close-delimited; anything else passes through as is.
        drop = {"connection", "keep-alive"} | ({"transfer-encoding", "content-length"} if sse else set())
        client_w.write(rewrite(status, lines, drop))
        if sse:
            usage = {}
            pings = await relay_sse(up_r, headers, client_w, start, usage)
            if entry:
                entry.update(pings=pings, **usage)
            if pings:
                log(f"{first} → {status.split(' ', 2)[1]} in {time.monotonic() - start:.0f}s, {pings} pings")
        else:
            while data := await up_r.read(65536):
                client_w.write(data)
        await client_w.drain()
    except (OSError, asyncio.IncompleteReadError, ValueError) as e:
        log(f"error: {e!r}")
        if entry:
            entry["error"] = type(e).__name__
    finally:
        if entry:
            entry["t1"] = round(time.time(), 3)
            record(entry)
        for w in (up_w, client_w):
            if w:
                w.close()


async def main():
    port, target = int(sys.argv[1]), sys.argv[2]
    tag = sys.argv[sys.argv.index("--tag") + 1] if "--tag" in sys.argv else "untagged"
    model = sys.argv[sys.argv.index("--ollama-shim") + 1] if "--ollama-shim" in sys.argv else None
    host, tport = target.rsplit(":", 1)
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, (host, int(tport)), tag, model), "127.0.0.1", port, limit=1 << 20)
    print(server.sockets[0].getsockname()[1], flush=True)
    parent = os.getppid()
    async with server:
        while os.getppid() == parent:   # exit with the ./agent process that started us
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
