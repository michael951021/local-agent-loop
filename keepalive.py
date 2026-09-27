#!/usr/bin/env python3
"""HTTP proxy to Ollama that keeps slow SSE streams alive.

  keepalive.py PORT HOST:PORT     listen on 127.0.0.1:PORT (0 = pick one; the port is printed first)

Ollama sends a tool_use block only once the whole call is generated, so a long Write can leave the
stream silent for minutes and trip Claude Code's 300 s idle watchdog. While the upstream request is
still open, this inserts Anthropic `ping` events (ignored by the client) between complete events.
Pings never complete a block or a message, so nothing runs early. They stop after MAX_AGE seconds,
so a request that is truly stuck still times out.
"""
import asyncio
import os
import sys
import time

IDLE = 20        # seconds of upstream silence before a ping
MAX_AGE = 1800   # no pings for requests older than this
PING = b'event: ping\ndata: {"type": "ping"}\n\n'


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


async def relay_sse(up_r, headers, client_w, start):
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
            client_w.write(pending[:cut])
            pending = pending[cut:]
            await client_w.drain()
    client_w.write(pending)
    return pings


async def handle(client_r, client_w, upstream):
    up_w = None
    try:
        first, lines, headers = await read_head(client_r)
        body = await client_r.readexactly(int(headers.get("content-length", 0)))
        start = time.monotonic()
        up_r, up_w = await asyncio.open_connection(*upstream)
        up_w.write(rewrite(first, lines, {"connection", "keep-alive"}) + body)
        await up_w.drain()

        status, lines, headers = await read_head(up_r)
        sse = headers.get("content-type", "").startswith("text/event-stream")
        # SSE is re-sent de-chunked and close-delimited; anything else passes through as is.
        drop = {"connection", "keep-alive"} | ({"transfer-encoding", "content-length"} if sse else set())
        client_w.write(rewrite(status, lines, drop))
        if sse:
            pings = await relay_sse(up_r, headers, client_w, start)
            if pings:
                log(f"{first} → {status.split(' ', 2)[1]} in {time.monotonic() - start:.0f}s, {pings} pings")
        else:
            while data := await up_r.read(65536):
                client_w.write(data)
        await client_w.drain()
    except (OSError, asyncio.IncompleteReadError, ValueError) as e:
        log(f"error: {e!r}")
    finally:
        for w in (up_w, client_w):
            if w:
                w.close()


async def main():
    port, target = int(sys.argv[1]), sys.argv[2]
    host, tport = target.rsplit(":", 1)
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, (host, int(tport))), "127.0.0.1", port, limit=1 << 20)
    print(server.sockets[0].getsockname()[1], flush=True)
    parent = os.getppid()
    async with server:
        while os.getppid() == parent:   # exit with the ./agent process that started us
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
