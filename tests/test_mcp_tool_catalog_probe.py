"""Tests for the installed-companion MCP catalog probe."""

from __future__ import annotations

import importlib.util
import json
import queue
import time
import unittest
from pathlib import Path
from unittest import mock

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mcp_tool_catalog_probe.py"
_SPEC = importlib.util.spec_from_file_location("mcp_tool_catalog_probe", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
probe_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe_module)


class _FakePipe:
	def __init__(self, lines=(), *, block_after=False):
		self.lines = queue.Queue()
		for line in lines:
			self.lines.put(line)
		self.block_after = block_after
		self.closed = False

	def readline(self):
		if not self.block_after and self.lines.empty():
			return ""
		try:
			return self.lines.get(timeout=0.5)
		except queue.Empty:
			return ""

	def close(self):
		self.closed = True


class _FakeProcess:
	def __init__(self, lines, *, block_after=False):
		self.stdout = _FakePipe(lines, block_after=block_after)
		self.stdin = mock.Mock()
		self.stdin.write = mock.Mock()
		self.wait = mock.Mock(return_value=0)
		self.kill = mock.Mock()


def _rpc(id, result):
	return json.dumps({"jsonrpc": "2.0", "id": id, "result": result}) + "\n"


class CatalogProbeTests(unittest.TestCase):
	def test_probe_follows_next_cursor_and_aggregates_pages(self):
		first = {"name": "health_snapshot", "outputSchema": {"type": "object"}}
		second = {"name": "get_current_focus"}
		process = _FakeProcess(
			[
				_rpc(1, {"capabilities": {}, "instructions": "ready", "serverInfo": {"name": "test"}}),
				_rpc(2, {"tools": [first], "nextCursor": "t1"}),
				_rpc(3, {"tools": [second]}),
			]
		)

		with mock.patch.object(probe_module.subprocess, "Popen", return_value=process):
			result = probe_module.probe(Path("fake-companion"), timeout=1)

		self.assertEqual(result["tools"], ["health_snapshot", "get_current_focus"])
		self.assertEqual(result["output_schemas"], 1)
		self.assertEqual(result["catalog_bytes"], len(json.dumps({"tools": [first, second]}, separators=(",", ":")).encode("utf-8")))
		writes = "".join(call.args[0] for call in process.stdin.write.call_args_list)
		self.assertIn('"id": 3', writes)
		self.assertIn('"cursor": "t1"', writes)

	def test_empty_catalog_finishes_without_waiting_for_another_stdout_line(self):
		process = _FakeProcess(
			[
				_rpc(1, {"capabilities": {}, "instructions": "ready", "serverInfo": {"name": "test"}}),
				_rpc(2, {"tools": []}),
			],
			block_after=True,
		)

		with mock.patch.object(probe_module.subprocess, "Popen", return_value=process):
			started = time.monotonic()
			result = probe_module.probe(Path("fake-companion"), timeout=0.1)
			elapsed = time.monotonic() - started

		self.assertEqual(result["tools"], [])
		self.assertLess(elapsed, 0.3)
		process.wait.assert_called_once()

	def test_probe_deadline_applies_while_waiting_for_a_response(self):
		process = _FakeProcess([], block_after=True)

		with mock.patch.object(probe_module.subprocess, "Popen", return_value=process):
			started = time.monotonic()
			result = probe_module.probe(Path("fake-companion"), timeout=0.05)
			elapsed = time.monotonic() - started

		self.assertEqual(result["tools"], [])
		self.assertLess(elapsed, 0.3)
		process.kill.assert_called_once()
		process.wait.assert_called_once_with(timeout=0.25)


if __name__ == "__main__":
	unittest.main()
