"""Deterministic context diagnostics from transcripts; no model calls or saved result bodies."""
import hashlib
import json
from collections import Counter, defaultdict

LARGE_CHARS = 16000
FIELDS = ("results", "result_chars", "large_results", "errors", "reads", "unbounded_reads",
          "repeat_reads", "repeat_chars")


def analyse(events):
    counts = Counter({k: 0 for k in FIELDS})
    pending, completed, seen = {}, set(), set()
    files = defaultdict(Counter)
    largest = []
    turn = 0
    last_mid = None
    for event in events:
        if event.get("parent_tool_use_id"):
            continue
        if event.get("subtype") == "compact_boundary":
            seen.clear()  # a reread after summarisation can restore lost evidence
        message = event.get("message") or {}
        content = message.get("content") or []
        if not isinstance(content, list):
            continue
        if event.get("type") == "assistant":
            if message.get("id") != last_mid:
                turn += 1
                last_mid = message.get("id")
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    pending[block["id"]] = (block["name"], block.get("input") or {}, turn)
        elif event.get("type") == "user":
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tid = block.get("tool_use_id")
                if tid in completed or tid not in pending:
                    continue
                completed.add(tid)
                name, args, call_turn = pending.pop(tid)
                body = block.get("content") or ""
                if isinstance(body, list):
                    body = "".join(b.get("text", "") for b in body if isinstance(b, dict))
                if not isinstance(body, str):
                    continue
                n = len(body)
                error = bool(block.get("is_error"))
                counts.update(results=1, result_chars=n, errors=int(error), large_results=int(n > LARGE_CHARS))
                # Do not copy commands or tool output into the audit (may contain secrets).
                item = {"tool": name, "turn": call_turn, "chars": n, "error": error}
                largest.append(item)
                largest = sorted(largest, key=lambda x: -x["chars"])[:8]
                if name != "Read":
                    continue
                counts.update(reads=1, unbounded_reads=int("limit" not in args))
                path = str(args.get("file_path") or "?")
                row = files[path]
                row.update(reads=1, chars=n, unbounded=int("limit" not in args))
                if error:
                    continue
                # Same request AND same bytes; changed files and different ranges aren't duplicates.
                request = dict(args, offset=args.get("offset", 1), limit=args.get("limit", 2000))
                key = (json.dumps(request, sort_keys=True), hashlib.sha256(body.encode()).digest())
                if key in seen:
                    counts.update(repeat_reads=1, repeat_chars=n)
                    row.update(repeats=1, repeat_chars=n)
                seen.add(key)
    return {**counts, "files": [{"path": p, **v} for p, v in sorted(files.items(), key=lambda x: -x[1]["chars"])[:12]],
            "largest": largest, "large_threshold_chars": LARGE_CHARS}


def aggregate(audits):
    audits = list(audits)
    return {k: sum(a.get(k, 0) for a in audits) for k in FIELDS}


JS = r"""
function contextAudit(a){
 if(!a)return '';
 let body=`<p>${a.results} tool results · ${k(a.result_chars)} characters · ${a.large_results} over 16,000 characters · ${a.errors} tool errors.</p>`;
 body+=`<p>${a.reads} reads (${a.unbounded_reads} without an explicit limit); ${a.repeat_reads} unchanged repeated reads (${k(a.repeat_chars)} characters).</p>`;
 if(a.files)body+='<table><tr><th>File</th><th>Reads</th><th>Characters</th><th>Repeated reads</th></tr>'+a.files.map(f=>`<tr><td>${esc(f.path)}</td><td>${f.reads}</td><td>${k(f.chars)}</td><td>${f.repeats||0}</td></tr>`).join('')+'</table>';
 if(a.largest)body+='<details><summary>Largest results (assistant turn, 1-based)</summary>'+a.largest.map(r=>`<div>${esc(r.tool)} · turn ${r.turn} · ${k(r.chars)} characters${r.error?' · error':''}</div>`).join('')+'</details>';
 return card('Context audit','Recorded main-thread text characters, not token estimates. Repeats require identical Read arguments and output within a compaction segment; they are investigation signals, not proof of waste. Bash rereads and image content are not counted as repeated reads.',body);
}
"""
