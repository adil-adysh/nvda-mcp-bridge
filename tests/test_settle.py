# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Tests for the settle contract (P2).

Settle is defined on the observation, not on a fixed quiet window, because the
browser measurements showed `valueChange` arriving 146 ms before the matching
`stateChange`. These tests pin: "changed" is measured, "unchanged" is a real
answer, and the budget bounds the work.
"""

import importlib
import os
import sys
import types
import unittest

_ADDON_DIR = os.path.normpath(
	os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "addon", "globalPlugins", "nvdaMcp-bridge")
)
if _ADDON_DIR not in sys.path:
	sys.path.insert(0, _ADDON_DIR)

if "logHandler" not in sys.modules:
	sys.modules["logHandler"] = types.SimpleNamespace(
		log=types.SimpleNamespace(
			debug=lambda *args, **kwargs: None,
			info=lambda *args, **kwargs: None,
			warning=lambda *args, **kwargs: None,
			error=lambda *args, **kwargs: None,
		)
	)

_PACKAGE_NAME = "nvdaMcpBridge"
if _PACKAGE_NAME not in sys.modules:
	_package = types.ModuleType(_PACKAGE_NAME)
	_package.__path__ = [_ADDON_DIR]
	sys.modules[_PACKAGE_NAME] = _package

settle = importlib.import_module(f"{_PACKAGE_NAME}.server.settle")


def focus(name="field", role="EDIT", value="", states=None):
	return {
		"focus": {
			"role": role,
			"name": name,
			"value": value,
			"states": states or [],
			"location": [0, 0, 10, 10],
		},
		"ancestors": [],
		"children": [],
		"dialog": None,
		"window": {"title": "Notepad", "handle": 1, "className": "Notepad"},
	}


class Clock:
	def __init__(self):
		self.now = 100.0

	def __call__(self):
		return self.now

	def advance(self, seconds):
		self.now += seconds


class FakeProjector:
	"""Serves a scripted sequence of projections and a revision counter."""

	def __init__(self, projections, clock):
		self.projections = list(projections)
		self.clock = clock
		self.revision = 0
		self.state_version = 0
		self.refreshes = 0

	def refresh(self):
		self.refreshes += 1
		projection = self.projections.pop(0) if len(self.projections) > 1 else self.projections[0]
		self.state_version += 1
		return projection, {}, self.state_version

	def wait_for_revision(self, since, timeout_ms):
		if len(self.projections) > 1:
			# Simulate an event arriving shortly after the wait starts.
			self.clock.advance(0.005)
			self.revision += 1
			return True
		# Nothing further pending: the debounce window passes.
		self.clock.advance(max(1, timeout_ms) / 1000.0)
		return False


def dispatch(operation):
	return operation()


class SettleTests(unittest.TestCase):
	def make(self, projections, **kwargs):
		clock = Clock()
		projector = FakeProjector(projections, clock)
		settler = settle.Settler(
			projector,
			dispatch,
			clock=clock,
			sleep=lambda seconds: clock.advance(seconds),
			**kwargs,
		)
		return settler, projector

	def test_a_change_is_reported_with_a_delta(self):
		settler, _ = self.make([focus(), focus(name="Search")])
		outcome = settler.run(lambda: {"sent": True})
		self.assertTrue(outcome.changed)
		self.assertEqual(outcome.delta["name"], {"from": "field", "to": "Search"})
		self.assertGreaterEqual(outcome.settled_ms, 0)
		self.assertEqual(outcome.state_version, 2)

	def test_no_change_is_a_reportable_outcome(self):
		settler, projector = self.make([focus()])
		outcome = settler.run(lambda: None)
		self.assertFalse(outcome.changed)
		self.assertEqual(outcome.delta, {})
		self.assertTrue(outcome.as_dict()["ok"])
		self.assertFalse(outcome.as_dict()["changed"])
		# One baseline projection plus at most one confirmation is enough when
		# nothing ever moved.
		self.assertLessEqual(projector.refreshes, 2)

	def test_the_budget_bounds_the_rounds(self):
		settler, projector = self.make([focus(), focus(name="a"), focus(name="b"), focus(name="c")])
		settler.run(lambda: None)
		self.assertLessEqual(projector.refreshes, 3)

	def test_uncertain_is_carried_from_the_action(self):
		settler, _ = self.make([focus(), focus(name="Search")])
		outcome = settler.run(lambda: {"uncertain": True})
		self.assertTrue(outcome.uncertain)

	def test_expect_satisfied_sets_completed(self):
		settler, _ = self.make([focus(), focus(name="Search")])
		outcome = settler.run(lambda: None, expect={"focus.name": "Search"})
		self.assertTrue(outcome.completed)
		self.assertIsNone(outcome.expect_mismatch)

	def test_expect_mismatch_is_reported_with_both_values(self):
		settler, _ = self.make([focus(), focus(name="Search")])
		outcome = settler.run(lambda: None, expect={"focus.name": "Run"})
		self.assertFalse(outcome.completed)
		self.assertEqual(
			outcome.expect_mismatch,
			{"focus.name": {"expected": "Run", "actual": "Search"}},
		)

	def test_no_expect_means_completed_is_absent(self):
		settler, _ = self.make([focus()])
		payload = settler.run(lambda: None).as_dict()
		self.assertNotIn("completed", payload)

	def test_dialog_appearing_is_a_change(self):
		settler, _ = self.make(
			[
				focus(),
				{"focus": focus()["focus"], "ancestors": [], "dialog": {"name": "Replace"}, "window": None},
			]
		)
		outcome = settler.run(lambda: None)
		self.assertTrue(outcome.changed)
		self.assertEqual(outcome.delta["dialog"], {"from": None, "to": "Replace"})


class LookupTests(unittest.TestCase):
	def test_dotted_paths(self):
		projection = focus()
		self.assertEqual(settle.lookup(projection, "focus.name"), "field")
		self.assertEqual(settle.lookup(projection, "window.title"), "Notepad")
		self.assertIsNone(settle.lookup(projection, "focus.missing"))
		self.assertIsNone(settle.lookup(projection, "nope.name"))

	def test_list_indices(self):
		projection = {"children": [{"name": "first"}, {"name": "second"}]}
		self.assertEqual(settle.lookup(projection, "children.1.name"), "second")
		self.assertIsNone(settle.lookup(projection, "children.9.name"))

	def test_list_expectation_is_order_sensitive(self):
		projection = {"focus": {"states": ["FOCUSABLE", "FOCUSED"]}}
		self.assertTrue(settle.evaluate_expect(projection, {"focus.states": ["FOCUSABLE", "FOCUSED"]}).satisfied)
		self.assertFalse(settle.evaluate_expect(projection, {"focus.states": ["FOCUSED"]}).satisfied)


if __name__ == "__main__":
	unittest.main()
