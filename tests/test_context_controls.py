"""Offline regressions for context controls, transcript metrics and task contracts.

Run: .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import contextaudit
import taskcontract
import standup
import study


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


budget = module("read_budget", "hooks/read_budget.py")
session = module("session_context", "hooks/session_context.py")


class ContextControls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def read(self, path, **args):
        return budget.reason({"tool_name": "Read", "cwd": str(self.root),
                              "tool_input": {"file_path": path, **args}})

    def test_read_ranges_and_long_lines(self):
        (self.root / "large.py").write_text("line\n" * 500)
        with patch.dict(os.environ, AGENT_READ_MAX_LINES="240", AGENT_READ_MAX_CHARS="16000"):
            self.assertIn("Read budget", self.read("large.py"))
            self.assertIsNone(self.read("large.py", offset=240, limit=80))
            self.assertIsNone(self.read("large.py", offset=490))
            (self.root / "minified.json").write_text("x" * 20000)
            self.assertIn("Read budget", self.read("minified.json", limit=1))
            self.assertIsNone(self.read("missing"))
            self.assertIsNone(self.read("."))
            self.assertIsNone(budget.reason([]))

    def test_hook_exit_contract(self):
        (self.root / "long").write_text("x\n" * 500)
        data = {"tool_name": "Read", "tool_input": {"file_path": str(self.root / "long")}}
        p = subprocess.run([sys.executable, ROOT / "hooks/read_budget.py"], input=json.dumps(data),
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, "")
        self.assertIn("offset and limit", p.stderr)

    def capture(self, code, timeout="5", name="test.log"):
        return subprocess.run([sys.executable, ROOT / "hooks/capture.py", "--log", self.root / name,
                               "--timeout", timeout, "--", sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=10)

    def test_capture_preserves_failure_and_evidence(self):
        p = self.capture("import sys; print('EARLY_FAILURE'); print('x'*100000); sys.exit(7)")
        self.assertEqual(p.returncode, 7)
        self.assertLess(len(p.stdout), 6400)
        self.assertIn("tail only", p.stdout)
        self.assertIn("EARLY_FAILURE", (self.root / "test.log").read_text())
        self.assertIn("EXIT_CODE=7", p.stdout)
        before = (self.root / "test.log").read_bytes()
        self.assertEqual(self.capture("print('overwrite')").returncode, 2)
        self.assertEqual((self.root / "test.log").read_bytes(), before)

    def test_capture_timeout_and_success(self):
        p = self.capture("import time; time.sleep(3)", timeout="0.1")
        self.assertEqual(p.returncode, 124)
        self.assertIn("Stopped: timeout", p.stdout)
        self.assertEqual(self.capture("print('ok')", name="ok.log").returncode, 0)

    def test_session_snapshot_bounded(self):
        (self.root / "NOTES.md").write_text("a" * 9000)
        (self.root / "TODO.md").write_text("irrelevant backlog")
        text = session.snapshot(self.root)
        self.assertIn("NOTES truncated", text)
        self.assertNotIn("irrelevant backlog", text)
        self.assertLess(len(text), 7500)


def exchange(tid, body="same", args=None, error=False, subagent=False):
    extra = {"parent_tool_use_id": "parent"} if subagent else {}
    return [{"type": "assistant", **extra, "message": {"id": tid, "content": [
        {"type": "tool_use", "id": tid, "name": "Read", "input": args or {"file_path": "a.py"}}]}},
        {"type": "user", **extra, "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": body, "is_error": error}]}}]


class AuditTests(unittest.TestCase):
    def test_study_parses_audit_with_run_identity(self):
        events = [{"type": "harness", "event": "start", "run": "r1", "t": 1,
                   "project": "fixture", "context_policy": "bounded-v1"}]
        for e in exchange("a") + exchange("b"):
            e["timestamp"] = "2026-09-29T00:00:00Z"
            if e["type"] == "assistant":
                e["message"]["usage"] = {"input_tokens": 100, "output_tokens": 10}
            events.append(e)
        events.append({"type": "harness", "event": "end", "run": "r1", "t": 2})
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "fixture.jsonl"
            log.write_text("\n".join(json.dumps(e, separators=(",", ":")) for e in events))
            runs = study.parse_log(log)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["start"]["run"], "r1")
        self.assertEqual(runs[0]["audit"]["repeat_reads"], 1)
        self.assertEqual(len(runs[0]["calls"]), 2)

    def test_repeat_requires_same_range_and_output_and_segment(self):
        ev = exchange("a") + exchange("b") + exchange("c", "changed")
        ev += exchange("d", args={"file_path": "a.py", "offset": 20, "limit": 1})
        ev += [{"type": "system", "subtype": "compact_boundary"}] + exchange("e")
        a = contextaudit.analyse(ev)
        self.assertEqual(a["repeat_reads"], 1)
        self.assertEqual(a["repeat_chars"], 4)
        self.assertEqual(a["reads"], 5)
        self.assertEqual(a["unbounded_reads"], 4)

    def test_duplicate_events_errors_and_subagents(self):
        ev = exchange("a", "x" * 16001)
        ev += ev[:] + exchange("b", error=True) + exchange("c", error=True)
        ev += exchange("child", subagent=True)
        a = contextaudit.analyse(ev)
        self.assertEqual(a["results"], 3)
        self.assertEqual(a["large_results"], 1)
        self.assertEqual(a["errors"], 2)
        self.assertEqual(a["repeat_reads"], 0)
        self.assertNotIn("x" * 50, json.dumps(a))
        self.assertEqual(contextaudit.aggregate([a, a])["results"], 6)

    def test_nontext_results_and_empty_logs(self):
        ev = exchange("a", [{"type": "image", "data": "binary"}, {"type": "text", "text": "hi"}])
        self.assertEqual(contextaudit.analyse(ev)["result_chars"], 2)
        self.assertEqual(contextaudit.analyse([])["results"], 0)


class ContractTests(unittest.TestCase):
    def test_legacy_and_new_contracts(self):
        legacy = taskcontract.parse("## Issue\nBug\n## Tests\npytest\n## Solution\nfix")
        self.assertEqual(legacy["done_when"], "pytest")
        self.assertEqual(taskcontract.gaps(legacy), ["Question", "Evidence", "Stop when"])
        full = taskcontract.parse("\n".join(f"## {v}\nanswer" for v in taskcontract.FIELDS.values()))
        self.assertEqual(taskcontract.gaps(full), [])

    def test_scheduler_supplies_contract_and_board_reads_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            spec = root / "state/tickets/t1.md"
            spec.parent.mkdir(parents=True)
            spec.write_text("## Question\nCan an invalid input bypass validation?\n## Stop when\nTwo attempts")
            self.assertEqual(standup.read_spec(root, "t1")["stop"], "Two attempts")
            job = dict(tid="t1", parent=None, src="TODO.md", line=1, task="Implement validation",
                       cl=None, next=[])
            prompt = standup.sched.prompt(standup.sched.Files(root), job, 1, False, [])
            self.assertIn("state/tickets/t1.md", prompt)
            self.assertIn("## Question", prompt)
            self.assertIn("## Stop when", prompt)
            self.assertIn("capture.py", prompt)
            self.assertNotIn("Pipe long output through", prompt)


if __name__ == "__main__":
    unittest.main()
