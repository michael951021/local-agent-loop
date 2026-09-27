#!/usr/bin/env python3
"""Serve reports/ on the local network, always current (run by ./reportsrv, see there).

  reportsrv.py [--port 8765] [--bind 0.0.0.0]

- Only clients on private/loopback addresses are answered (LAN, not the internet), read-only.
- Opening the index or fleet page refreshes them (and the diffs of the runs still going) when they are
  older than REFRESH seconds; it happens in the background and the page reloads itself when done.
- Every page gets a small script that reloads it when its file changes (scroll position kept).
"""
import argparse
import ipaddress
import os
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
REFRESH = 60
LIVE = b"""<script>(()=>{const p=location.pathname,k='scroll:'+p;let m=null;
try{const y=sessionStorage.getItem(k);if(y!=null){scrollTo(0,+y);sessionStorage.removeItem(k)}}catch(e){}
async function poll(){try{const r=await fetch('/__mtime?p='+encodeURIComponent(p),{cache:'no-store'});const t=await r.text();
 if(m&&t!==m){try{sessionStorage.setItem(k,scrollY)}catch(e){}location.reload();return}m=t}catch(e){}setTimeout(poll,10000)}poll()})()</script>"""

_lock = threading.Lock()
_last = 0.0


def refresh():
    """Rebuild index + fleet and the live runs' diffs, at most once per REFRESH seconds, one at a time."""
    global _last
    if time.time() - _last < REFRESH or not _lock.acquire(blocking=False):
        return
    try:
        _last = time.time()
        for cmd in (["diffpage.py", "--live"], ["ctxreport.py", "--index"]):
            subprocess.run([sys.executable, str(ROOT / cmd[0]), *cmd[1:]], cwd=ROOT, capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        pass
    finally:
        _lock.release()


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):   # quiet: the journal only keeps errors
        pass

    def allowed(self):
        ip = ipaddress.ip_address(self.client_address[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        return ip.is_private or ip.is_loopback or ip.is_link_local

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        if not self.allowed():
            return self.send_error(403)
        u = urlparse(self.path)
        if u.path == "/__mtime":
            f = self.local(parse_qs(u.query).get("p", ["/"])[0])
            body = str(f.stat().st_mtime if f and f.exists() else 0).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        f = self.local(u.path)
        if f and f.name in ("index.html", "fleet.html") and f.parent == REPORTS:
            if time.time() - f.stat().st_mtime > REFRESH:
                threading.Thread(target=refresh, daemon=True).start()
        if f and f.suffix == ".html" and f.is_file():
            body = f.read_bytes()
            body = body.replace(b"</body>", LIVE + b"</body>", 1) if b"</body>" in body else body + LIVE
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        return super().do_GET()

    def do_HEAD(self):
        if not self.allowed():
            return self.send_error(403)
        return super().do_HEAD()

    def local(self, path):
        """The file under reports/ a URL path names (index.html for directories), or None if outside."""
        f = (REPORTS / unquote(path).lstrip("/")).resolve()
        if f != REPORTS and REPORTS not in f.parents:
            return None
        return f / "index.html" if f.is_dir() else f

    def list_directory(self, path):   # no directory listings
        self.send_error(404)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("REPORTS_PORT", 8765)))
    ap.add_argument("--bind", default=os.environ.get("REPORTS_BIND", "0.0.0.0"))
    a = ap.parse_args()
    REPORTS.mkdir(exist_ok=True)
    srv = ThreadingHTTPServer((a.bind, a.port), partial(Handler, directory=str(REPORTS)))
    print(f"serving {REPORTS} on http://{a.bind}:{a.port}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
