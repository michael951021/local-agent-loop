#!/usr/bin/env python3
"""Forward Ollama into a network-less sandbox over a unix socket (used when NET=off).

  bridge.py out SOCK HOST:PORT   host side:    unix SOCK  -> tcp HOST:PORT
  bridge.py in  SOCK PORT        sandbox side: tcp 127.0.0.1:PORT -> unix SOCK
"""
import asyncio
import os
import sys


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


def handler(connect):
    async def handle(r1, w1):
        try:
            r2, w2 = await connect()
        except OSError:
            w1.close()
            return
        await asyncio.gather(pipe(r1, w2), pipe(r2, w1))
    return handle


async def main():
    mode, sock = sys.argv[1], sys.argv[2]
    if mode == "out":
        host, port = sys.argv[3].rsplit(":", 1)
        if os.path.exists(sock):
            os.unlink(sock)
        server = await asyncio.start_unix_server(
            handler(lambda: asyncio.open_connection(host, int(port))), sock)
    else:
        server = await asyncio.start_server(
            handler(lambda: asyncio.open_unix_connection(sock)), "127.0.0.1", int(sys.argv[3]))
    async with server:
        await server.serve_forever()


asyncio.run(main())
