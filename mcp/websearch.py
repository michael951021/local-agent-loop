"""MCP server giving the sandboxed agent web search (DuckDuckGo, no API key) and fast page fetching."""
import re
import sys
from contextlib import redirect_stdout
from html import unescape
from urllib.request import Request, urlopen

from ddgs import DDGS
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("websearch")


@mcp.tool()
def web_search(query: str, max_results: int = 8) -> str:
    """Search the web. Returns title, URL and snippet for each result."""
    try:
        with redirect_stdout(sys.stderr):  # stdout is the MCP channel; ddgs prints debug lines
            results = DDGS().text(query, max_results=max_results)
    except Exception as e:  # rate limits, network errors
        return f"search failed: {e}"
    if not results:
        return "no results"
    return "\n\n".join(f"{r['title']}\n{r['href']}\n{r['body']}" for r in results)


@mcp.tool()
def fetch_page(url: str, max_chars: int = 20000) -> str:
    """Download a web page and return its readable text (much faster than WebFetch)."""
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
        with urlopen(req, timeout=30) as resp:
            html = resp.read(5_000_000).decode(resp.headers.get_content_charset() or "utf-8", "replace")
    except Exception as e:
        return f"fetch failed: {e}"
    html = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|pre)>", "\n", html)
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:max_chars] + ("\n…[truncated]" if len(text) > max_chars else "")


if __name__ == "__main__":
    mcp.run()
