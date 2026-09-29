#!/usr/bin/env python3
"""Small, task-selected repository map. Paths and Python declarations, never source bodies.

Use inside an agent sandbox:
  python3 /opt/agent/hooks/repo_map.py --query 'authorization check'
The scheduler embeds a shorter query result with each assigned task. No index is
stored, so changes in a worktree are reflected on the next query.
"""
import argparse
import ast
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess

SOURCE = {".py", ".go", ".rs", ".js", ".jsx", ".ts", ".tsx", ".java", ".c", ".cc", ".cpp", ".h",
          ".sh", ".md", ".yaml", ".yml", ".toml", ".json"}
SKIP = {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__", ".agent-evidence"}
STOP = {"a", "an", "and", "the", "for", "with", "from", "into", "when", "this", "that", "make", "add", "fix",
        "build", "test", "tests", "agent", "agents", "task", "step", "file", "files", "script", "scripts", "loop"}


def files(root, cap=2500):
    """Git's tracked/untracked list respects ignores; fallback supports fresh fixtures."""
    try:
        p = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                           cwd=root, capture_output=True, timeout=3)
        if p.returncode == 0:
            names = [os.fsdecode(x) for x in p.stdout.split(b"\0") if x]
        else:
            names = None
    except (OSError, subprocess.TimeoutExpired):
        names = None
    if names is None:
        names = []
        for base, dirs, filenames in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith("."))
            names.extend(str((Path(base) / f).relative_to(root)) for f in sorted(filenames))
            if len(names) >= cap:
                break
    out = []
    for name in sorted(set(names)):
        path = Path(name)
        if len(out) >= cap:
            break
        if path.suffix.lower() not in SOURCE or any(part in SKIP for part in path.parts):
            continue
        full = root / path
        if full.is_file() and not full.is_symlink():
            out.append(name)
    return out


def symbols(root, name):
    """Top-level Python classes/functions and class methods; syntax errors degrade to path hints."""
    path = root / name
    try:
        if path.stat().st_size > 128_000:
            return []
        tree = ast.parse(path.read_text(errors="replace"), filename=name)
    except (OSError, SyntaxError, ValueError):
        return []
    found = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append((node.name, node.lineno))
        elif isinstance(node, ast.ClassDef):
            found.append((node.name, node.lineno))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.append((f"{node.name}.{child.name}", child.lineno))
    return found[:180]


def words(value):
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    # A five-character prefix catches common forms such as validate/validation
    # and secure/security without an NLP dependency. It is only a navigation hint.
    return {s.lower()[:5] for s in re.findall(r"[A-Za-z][A-Za-z0-9]*", spaced)
            if len(s) > 2 and s.lower() not in STOP}


def inventory(root, max_chars=650):
    names = files(root)
    if not names:
        return "Repository map: no source files found."
    code_names = [n for n in names if Path(n).suffix.lower() not in {".md", ".yaml", ".yml", ".toml", ".json"}]
    groups = Counter(Path(n).parts[0] if len(Path(n).parts) > 1 else "root" for n in (code_names or names))
    langs = Counter(Path(n).suffix.lower() for n in names)
    text = (f"Repository map: {len(names)} source, config and documentation files. Top code areas: " +
            ", ".join(f"{n} ({v})" for n, v in groups.most_common(7)) +
            ". Types: " + ", ".join(f"{n} ({v})" for n, v in langs.most_common(5)) +
            ". Use repo_map.py --query for relevant paths and Python declarations.")
    return text[:max_chars]


def query(root, task, max_chars=2400, max_files=7):
    names = files(root)
    terms = words(task)
    if not terms or not names:
        return ""
    candidates = []
    for name in names:
        path_terms = words(name)
        path_score = len(terms & path_terms) * 4
        # Declaration matches find modules whose filenames do not say what
        # they implement. Large files are skipped in symbols().
        decls = symbols(root, name) if Path(name).suffix == ".py" else []
        matched = [(s, line) for s, line in decls if terms & words(s)]
        score = path_score + 3 * min(3, len(matched))
        if score:
            candidates.append((score, name, matched, decls))
    candidates.sort(key=lambda row: (-row[0], row[1]))
    if not candidates:
        return "Repository map: no matching paths. Search source text with rg -n."
    lines = ["Repository hints (paths and declarations only; verify with rg and small reads):"]
    for _, name, matched, decls in candidates[:max_files]:
        chunk = ["- " + json.dumps(name, ensure_ascii=True)]
        shown = matched[:5] or decls[:3]
        if shown:
            chunk.append("  " + ", ".join(f"{s}:{line}" for s, line in shown))
        if len("\n".join(lines + chunk)) > max_chars:
            break
        lines.extend(chunk)
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--query")
    parser.add_argument("--max-chars", type=int, default=4000)
    args = parser.parse_args(argv)
    if not 100 <= args.max_chars <= 10000:
        parser.error("--max-chars must be between 100 and 10000")
    root = args.root.resolve()
    print(query(root, args.query, args.max_chars) if args.query else inventory(root, args.max_chars))


if __name__ == "__main__":
    main()
