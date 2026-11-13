# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Action verification and bounded evidence (API level 1).

Verification is a statement about a post-action observation. These tests fail
if a result claims confirmation without one, if evidence is fabricated from the
request, or if the evidence block is unbounded.
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


class Clock:
	def __init__(self, step=0.001):
		self.now = 0.0
		self.step = step

	def __call__(self):
		value = self.now
		self.now += self.step
		return value


class Projector:
	def __init__(self, projections, activity=False, clock=None):
		self._projections = list(projections)
		self.revision = 0
		self.state_version = 0
		self.activity = activity
		self._clock = clock

	def refresh(self):
		projection = self._projections.pop(0) if len(self._projections) > 1 else self._projections[0]
		self.revision += 1
		self.state_version += 1
		return projection, {}, self.state_version

	def wait_for_revision(self, revision, timeout_ms):
		if self.activity and self._clock is not None:
			self._clock.now += 0.4
			return True
		return False


def run(projector, action=None, expect=None, cancellation_event=None, clock=None, budget_ms=1000):
	clock = clock or Clock()
	settler = settle.Settler(
		projector,
		dispatch=lambda operation: operation(),
		budget_ms=budget_ms,
		quiet_ms=1,
		clock=clock,
		sleep=lambda seconds: None,
	)
	return settler.run(action or (lambda: {"dispatched": True}), expect=expect,
					   cancellation_event=cancellation_event)


class VerificationTests(unittest.TestCase):
	def test_confirmed_requires_and_records_a_matching_observation(self):
		projector = Projector([{"focus": {"name": "Save"}}])
		outcome = run(projector, expect={"focus.name": "Save"})
		payload = outcome.as_dict()
		self.assertEqual(payload["verification"], "confirmed")
		self.assertTrue(payload["observed"])
		self.assertEqual(payload["evidence"][0]["source"], "projection")
		self.assertTrue(payload["evidence"][0]["matched"])
		self.assertEqual(payload["evidence"][0]["value"], "Save")

	def test_a_contradictory_read_back_is_reported_as_contradicted(self):
		projector = Projector([{"focus": {"name": "Cancel"}}])
		outcome = run(projector, expect={"focus.name": "Save"})
		payload = outcome.as_dict()
		self.assertEqual(payload["verification"], "contradicted")
		entry = payload["evidence"][0]
		self.assertFalse(entry["matched"])
		self.assertEqual(entry["expected"], "Save")
		self.assertEqual(entry["observed"], "Cancel")

	def test_no_read_back_is_unavailable_not_confirmed(self):
		projector = Projector([None])
		outcome = run(projector, expect={"focus.name": "Save"})
		payload = outcome.as_dict()
		self.assertEqual(payload["verification"], "unavailable")
		self.assertFalse(payload["observed"])
		self.assertEqual(payload["evidence"], [])

	def test_successful_dispatch_without_an_expectation_is_not_verification(self):
		projector = Projector([{"focus": {"name": "Save"}}])
		calls = []
		outcome = run(projector, action=lambda: calls.append(1) or {"dispatched": True})
		payload = outcome.as_dict()
		self.assertEqual(calls, [1], "the action was dispatched")
		self.assertEqual(payload["verification"], "unavailable")
		self.assertTrue(payload["observed"], "a projection was still obtained")
		self.assertEqual(payload["evidence"], [])

	def test_dispatch_failure_never_produces_a_verified_result(self):
		projector = Projector([{"focus": {"name": "Save"}}])

		def explode():
			raise RuntimeError("dispatch failed")

		with self.assertRaises(RuntimeError):
			run(projector, action=explode, expect={"focus.name": "Save"})

	def test_cancellation_before_dispatch_does_not_run_the_action(self):
		projector = Projector([{"focus": {"name": "Save"}}])
		cancellation = types.SimpleNamespace(is_set=lambda: True)
		calls = []
		outcome = run(projector, action=lambda: calls.append(1), cancellation_event=cancellation)
		payload = outcome.as_dict()
		self.assertEqual(calls, [], "nothing was dispatched")
		self.assertTrue(payload["cancelled"])
		self.assertEqual(payload["verification"], "unavailable")

	def test_cancellation_after_dispatch_is_uncertain(self):
		projector = Projector([{"focus": {"name": "Save"}}])
		state = {"set": False}

		def action():
			state["set"] = True
			return {"dispatched": True}

		cancellation = types.SimpleNamespace(is_set=lambda: state["set"])
		outcome = run(projector, action=action, cancellation_event=cancellation)
		payload = outcome.as_dict()
		self.assertTrue(payload["cancelled"])
		self.assertTrue(payload["uncertain"], "the action ran; its outcome is unknown")
		self.assertEqual(payload["verification"], "unavailable")

	def test_a_timeout_after_dispatch_does_not_claim_confirmation(self):
		clock = Clock(step=0.2)
		projector = Projector([None], activity=True, clock=clock)
		outcome = run(projector, expect={"focus.name": "Save"}, clock=clock, budget_ms=50)
		payload = outcome.as_dict()
		self.assertTrue(payload["budget_expired"])
		self.assertEqual(payload["settling_status"], "unavailable", "no observation was obtained")
		self.assertEqual(payload["verification"], "unavailable")
		self.assertFalse(payload["observed"])

	def test_evidence_uses_the_final_observation_not_the_baseline(self):
		projector = Projector([{"focus": {"name": "Before"}}, {"focus": {"name": "After"}}])
		outcome = run(projector, expect={"focus.name": "After"})
		self.assertEqual(outcome.verification, "confirmed")
		self.assertEqual(outcome.evidence[0]["value"], "After")

	def test_an_unrelated_change_still_verifies_the_declared_expectation(self):
		projector = Projector([{"focus": {"name": "Save"}, "dialog": None}])
		outcome = run(projector, expect={"dialog": None})
		self.assertEqual(outcome.verification, "confirmed")
		self.assertEqual(len(outcome.evidence), 1)

	def test_an_operation_without_causally_identifiable_effects_is_not_verified(self):
		projector = Projector([{"dialog": None}])
		outcome = run(projector, expect=None)
		payload = outcome.as_dict()
		self.assertFalse(payload["changed"])
		self.assertEqual(payload["verification"], "unavailable")

	def test_evidence_is_bounded_by_entry_count_and_text_length(self):
		big = "x" * 5000
		expect = {f"field{index}": "wanted" for index in range(20)}
		projection = {f"field{index}": big for index in range(20)}
		evidence = settle.build_expectation_evidence(projection, expect, state_version=3)
		self.assertLessEqual(len(evidence), settle.MAX_EVIDENCE_ENTRIES)
		for entry in evidence:
			self.assertLessEqual(len(str(entry.get("observed", ""))), settle.MAX_EVIDENCE_TEXT + 1)
			self.assertLessEqual(len(str(entry.get("expected", ""))), settle.MAX_EVIDENCE_TEXT + 1)

	def test_sensitive_paths_are_redacted(self):
		projection = {"password": "hunter2", "focus.name": "Sign in"}
		expect = {"password": "hunter2", "focus.name": "Sign in"}
		evidence = settle.build_expectation_evidence(projection, expect, state_version=1)
		password_entry = next(entry for entry in evidence if "password" in entry["path"])
		self.assertTrue(password_entry.get("redacted"))
		self.assertNotIn("hunter2", str(password_entry))

	def test_the_evidence_source_vocabulary_is_closed(self):
		self.assertIn("projection", settle.EVIDENCE_SOURCES)
		self.assertIn("uia_value", settle.EVIDENCE_SOURCES)
		self.assertNotIn("command", settle.EVIDENCE_SOURCES)
		self.assertNotIn("expectation", settle.EVIDENCE_SOURCES)


if __name__ == "__main__":
	unittest.main()
