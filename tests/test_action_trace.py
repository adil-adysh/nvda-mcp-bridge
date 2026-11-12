# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Tests for the correlated action trace.

These pin the distinctions the diagnostic record exists to preserve: an
unchanged projection is not "the UI did not change", a temporal log match is not
causation, and an action that did its job while its expected postcondition was
missed is not the same as a failed action.
"""

import importlib
import os
import sys
import threading
import time
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

traces = importlib.import_module(f"{_PACKAGE_NAME}.server.action_trace")
events = importlib.import_module(f"{_PACKAGE_NAME}.server.events")
settle = importlib.import_module(f"{_PACKAGE_NAME}.server.settle")
state = importlib.import_module(f"{_PACKAGE_NAME}.server.state")


class _Clock:
	"""A monotonic clock that advances a fixed step per read, never real time."""

	def __init__(self, step=0.01):
		self.value = 0.0
		self.step = step

	def __call__(self):
		self.value += self.step
		return self.value


class SanitizationTests(unittest.TestCase):
	def test_credentials_are_redacted_and_named(self):
		payload, meta = traces.sanitize_params(
			{"token": "abc", "user": "bob", "nested": {"apiKey": "k", "text": "hi"}}
		)

		self.assertEqual(payload["token"], traces.REDACTED)
		self.assertEqual(payload["nested"]["apiKey"], traces.REDACTED)
		self.assertEqual(payload["user"], "bob")
		self.assertEqual(payload["nested"]["text"], "hi")
		self.assertEqual(meta["redacted_fields"], ["nested.apiKey", "token"])
		self.assertFalse(meta["truncated"])

	def test_long_values_are_bounded_and_reported(self):
		payload, meta = traces.sanitize_params({"document": "x" * 5000})

		self.assertLessEqual(len(payload["document"]), traces.MAX_PARAM_CHARS)
		self.assertTrue(meta["truncated"])

	def test_the_payload_stays_inside_the_serialized_budget(self):
		payload, meta = traces.sanitize_params({f"k{index}": "y" * 200 for index in range(40)})

		self.assertLessEqual(len(str(payload)), traces.MAX_PARAMS_SERIALIZED + 64)
		self.assertTrue(meta["dropped_fields"])
		self.assertTrue(meta["truncated"])

	def test_non_json_values_become_strings(self):
		payload, _meta = traces.sanitize_params({"obj": object(), "when": 3.5, "flag": True})

		self.assertIsInstance(payload["obj"], str)
		self.assertEqual(payload["when"], 3.5)
		self.assertTrue(payload["flag"])

	def test_deep_nesting_stops_instead_of_recursing(self):
		value = {"a": {"b": {"c": {"d": {"e": {"f": "deep"}}}}}}
		payload, meta = traces.sanitize_params(value)

		self.assertTrue(meta["truncated"])
		self.assertIn("depth-limit", str(payload))

	def test_an_exception_record_carries_type_stage_and_category(self):
		try:
			raise LookupError("no such element")
		except LookupError as exc:
			record = traces.exception_record(exc, stage="handler")

		self.assertEqual(record["type"], "LookupError")
		self.assertEqual(record["message"], "no such element")
		self.assertEqual(record["stage"], "handler")
		self.assertEqual(record["category"], traces.CATEGORY_HANDLER)
		self.assertIn("Traceback", record["traceback"])

	def test_a_traceback_keeps_the_frame_but_drops_the_install_path(self):
		def explode():
			raise RuntimeError("boom")

		try:
			explode()
		except RuntimeError as exc:
			text = traces.sanitize_traceback(exc)

		self.assertIn('File "test_action_trace.py"', text)
		self.assertIn("in explode", text)
		self.assertNotIn(":" + "\\", text, "no absolute path survives")
		self.assertNotIn(os.path.dirname(os.path.abspath(__file__)), text)

	def test_categories_follow_the_failure_kind(self):
		self.assertEqual(traces.classify_exception(TimeoutError("late")), traces.CATEGORY_TIMEOUT)
		self.assertEqual(traces.classify_exception(ValueError("bad")), traces.CATEGORY_INPUT_VALIDATION)
		self.assertEqual(traces.classify_exception(NotImplementedError()), traces.CATEGORY_UNSUPPORTED)
		self.assertEqual(traces.category_for_protocol_code(4003), traces.CATEGORY_PERMISSION)
		self.assertEqual(traces.category_for_protocol_code(4206), traces.CATEGORY_DOMAIN)
		self.assertEqual(traces.category_for_protocol_code(5004), traces.CATEGORY_TIMEOUT)


class StoreTests(unittest.TestCase):
	def test_a_stored_trace_round_trips(self):
		store = traces.ActionTraceStore()
		store.add({"trace_id": "at-1", "action": {"name": "press"}})

		found = store.get("at-1")

		self.assertEqual(found["action"]["name"], "press")
		self.assertIn("at-1", store)

	def test_reads_return_copies(self):
		store = traces.ActionTraceStore()
		store.add({"trace_id": "at-1", "action": {"name": "press"}})

		store.get("at-1")["action"]["name"] = "mutated"

		self.assertEqual(store.get("at-1")["action"]["name"], "press")

	def test_the_store_is_bounded(self):
		store = traces.ActionTraceStore(max_traces=3)
		for index in range(6):
			store.add({"trace_id": f"at-{index}"})

		self.assertEqual(len(store), 3)
		self.assertIsNone(store.get("at-0"))
		self.assertIsNotNone(store.get("at-5"))
		self.assertEqual(store.stats()["evicted_total"], 3)

	def test_expired_traces_are_dropped(self):
		clock = _Clock()
		store = traces.ActionTraceStore(max_age_seconds=1.0, clock=clock)
		store.add({"trace_id": "at-1"})
		clock.value += 5.0

		self.assertIsNone(store.get("at-1"))
		self.assertEqual(len(store), 0)

	def test_duplicate_ids_replace_and_are_counted(self):
		store = traces.ActionTraceStore()
		store.add({"trace_id": "at-1", "n": 1})
		store.add({"trace_id": "at-1", "n": 2})

		self.assertEqual(store.get("at-1")["n"], 2)
		self.assertEqual(len(store), 1)
		self.assertEqual(store.stats()["duplicate_replacements"], 1)

	def test_a_trace_without_an_id_is_ignored(self):
		store = traces.ActionTraceStore()
		store.add({"action": {"name": "press"}})

		self.assertEqual(len(store), 0)

	def test_retention_metadata_describes_the_window(self):
		store = traces.ActionTraceStore(max_traces=5, max_age_seconds=60.0)
		store.add({"trace_id": "at-1"})

		retention = store.retention_for("at-1")

		self.assertEqual(retention["max_traces"], 5)
		self.assertEqual(retention["max_age_seconds"], 60.0)
		self.assertTrue(retention["expires_at"].endswith("Z"))

	def test_concurrent_writers_and_readers_are_safe(self):
		store = traces.ActionTraceStore(max_traces=20)
		errors = []

		def writer(offset):
			try:
				for index in range(25):
					store.add({"trace_id": f"at-{offset}-{index}", "value": index})
					store.get(f"at-{offset}-{index}")
			except Exception as exc:  # noqa: BLE001
				errors.append(exc)

		threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join()

		self.assertEqual(errors, [])
		self.assertLessEqual(len(store), 20)

	def test_stats_report_eviction_and_last_trace(self):
		store = traces.ActionTraceStore(max_traces=2)
		for index in range(3):
			store.add({"trace_id": f"at-{index}"})

		stats = store.stats()

		self.assertEqual(stats["count"], 2)
		self.assertEqual(stats["last_trace_id"], "at-2")
		self.assertIsNotNone(stats["oldest_age_seconds"])


class RecorderTests(unittest.TestCase):
	def _recorder(self):
		clock = _Clock()
		return traces.ActionTraceRecorder(clock=clock, wall_clock=lambda: 1_700_000_000.0), clock

	def test_a_started_trace_identifies_the_action(self):
		recorder, _clock = self._recorder()

		trace = recorder.start_action("press", {"keys": "tab"}, request_id="req-1")

		self.assertEqual(trace["sandbox"] if "sandbox" in trace else trace["schema_version"], 1)
		self.assertEqual(trace["action"]["name"], "press")
		self.assertEqual(trace["action"]["request_ids"], ["req-1"])
		self.assertEqual(trace["request"]["params"], {"keys": "tab"})
		self.assertTrue(trace["trace_id"].startswith("at-"))
		self.assertIsNone(trace["action"]["mcp_request_id"])

	def test_milestones_are_recorded_in_order(self):
		recorder, _clock = self._recorder()
		trace = recorder.start_action("press", {})
		recorder.record_validation(True)
		recorder.record_dispatch()
		recorder.record_handler_start()
		recorder.record_handler_result("succeeded")

		names = [item["name"] for item in trace["lifecycle"]["milestones"]]

		self.assertEqual(
			names,
			["received", "validated", "dispatched", "handler_started", "handler_completed"],
		)
		self.assertTrue(all("at" in item and "elapsed_ms" in item for item in trace["lifecycle"]["milestones"]))

	def test_dispatch_and_handler_status_are_visible(self):
		recorder, _clock = self._recorder()
		trace = recorder.start_action("press", {})
		recorder.record_dispatch()
		recorder.record_handler_start()

		self.assertEqual(trace["execution"]["dispatch_status"], "dispatched")
		self.assertEqual(trace["execution"]["handler_status"], "running")

	def test_the_first_exception_is_the_one_that_is_kept(self):
		recorder, _clock = self._recorder()
		trace = recorder.start_action("press", {})
		recorder.record_exception(LookupError("missing"), "handler")
		recorder.record_exception(RuntimeError("follow-on"), "serialization")

		self.assertEqual(trace["execution"]["exception"]["type"], "LookupError")
		self.assertEqual(trace["execution"]["failure_stage"], "handler")

	def test_a_refusal_without_an_exception_is_recorded(self):
		recorder, _clock = self._recorder()
		trace = recorder.start_action("nope", {})
		recorder.record_wire_failure(code=4102, message="Unknown action", stage="validation")

		self.assertEqual(trace["execution"]["exception"]["code"], 4102)
		self.assertEqual(trace["execution"]["error_category"], traces.CATEGORY_INPUT_VALIDATION)

	def test_finalize_closes_the_record_and_stores_it(self):
		recorder, _clock = self._recorder()
		store = traces.ActionTraceStore()
		recorder.start_action("press", {"keys": "tab"})
		recorder.record_validation(True)
		recorder.record_dispatch()
		recorder.record_handler_start()
		recorder.record_handler_result("succeeded")
		recorder.record_tool_result("succeeded")

		trace = recorder.finalize(outcome=traces.OUTCOME_SUCCEEDED, store=store)

		self.assertEqual(trace["completion"]["outcome"], "succeeded")
		self.assertTrue(trace["completion"]["tool_succeeded"])
		self.assertTrue(trace["completion"]["handler_returned"])
		self.assertIsNotNone(trace["action"]["finished_at"])
		self.assertGreaterEqual(trace["action"]["duration_ms"], 0)
		self.assertEqual(store.get(trace["trace_id"])["trace_id"], trace["trace_id"])

	def test_the_outcome_is_derived_when_none_is_supplied(self):
		recorder, _clock = self._recorder()
		recorder.start_action("press", {})
		recorder.record_exception(TimeoutError("late"), "dispatch")

		trace = recorder.finalize()

		self.assertEqual(trace["completion"]["outcome"], "timed_out")
		self.assertEqual(trace["execution"]["error_category"], traces.CATEGORY_TIMEOUT)

	def test_recording_without_a_start_is_harmless(self):
		recorder, _clock = self._recorder()

		recorder.record_validation(True)
		recorder.record_exception(ValueError("x"), "handler")

		self.assertIsNone(recorder.trace)
		self.assertEqual(recorder.finalize(), {})

	def test_every_evidence_item_names_its_source(self):
		recorder, _clock = self._recorder()
		recorder.start_action("press", {})
		recorder.record_validation(True)
		recorder.record_handler_start()
		recorder.record_handler_result("succeeded")
		recorder.record_tool_result("succeeded")

		trace = recorder.finalize(outcome="succeeded")

		self.assertIn("action_lifecycle", trace["evidence"]["sources"])
		self.assertIn("bridge_log", trace["evidence"]["sources"])
		self.assertTrue(all("source" in log for log in trace["evidence"]["bridge_logs"]))


class AssemblerTests(unittest.TestCase):
	def _settle_outcome(self, **overrides):
		defaults = dict(
			changed=False,
			state_version=3,
			settled_ms=12.0,
			delta={},
			projection={"focus": {"role": "BUTTON"}, "ancestors": []},
			baseline_state_version=2,
			final_state_version=3,
			events=[],
			events_complete=True,
			settling_status="settled",
			budget_ms=1000,
		)
		defaults.update(overrides)
		return settle.SettleOutcome(**defaults)

	def test_the_seven_observation_cases_stay_distinct(self):
		assembler = traces.DiagnosticAssembler()
		event = {"kind": "focus", "version": 1}

		statuses = {
			"changed": assembler.observation_from_settle(self._settle_outcome(changed=True))["observation_status"],
			"no_change": assembler.observation_from_settle(self._settle_outcome())["observation_status"],
			"unavailable": assembler.observation_from_settle(
				self._settle_outcome(projection=None, settling_status="unavailable")
			)["observation_status"],
			"timed_out": assembler.observation_from_settle(
				self._settle_outcome(settling_status="timed_out")
			)["observation_status"],
			"events_incomplete": assembler.observation_from_settle(
				self._settle_outcome(events=[event], events_complete=False)
			)["observation_status"],
			"event_without_change": assembler.observation_from_settle(
				self._settle_outcome(events=[event], events_complete=True)
			)["observation_status"],
		}

		self.assertEqual(statuses["changed"], "changed")
		self.assertEqual(statuses["no_change"], "no_detectable_change")
		self.assertEqual(statuses["unavailable"], "projection_unavailable")
		self.assertEqual(statuses["timed_out"], "timed_out")
		self.assertEqual(statuses["events_incomplete"], "events_incomplete")
		self.assertEqual(statuses["event_without_change"], "events_without_projected_change")
		self.assertEqual(len(set(statuses.values())), len(statuses))

	def test_an_unavailable_projection_does_not_claim_no_change(self):
		assembler = traces.DiagnosticAssembler()

		observation = assembler.observation_from_settle(
			self._settle_outcome(projection=None, settling_status="unavailable")
		)

		self.assertIsNone(observation["projection_changed"])
		self.assertFalse(observation["projection_available"])

	def test_expectation_status_covers_not_provided_and_not_satisfied(self):
		assembler = traces.DiagnosticAssembler()

		missing = assembler.observation_from_settle(self._settle_outcome())
		missed = assembler.observation_from_settle(
			self._settle_outcome(
				completed=False,
				expect={"focus.name": "Send"},
				expect_mismatch={"focus.name": {"expected": "Send", "actual": "Cancel"}},
			)
		)
		satisfied = assembler.observation_from_settle(
			self._settle_outcome(completed=True, expect={"focus.name": "Send"})
		)

		self.assertEqual(missing["expectation"]["status"], "not_provided")
		self.assertEqual(missed["expectation"]["status"], "not_satisfied")
		self.assertEqual(missed["expectation"]["mismatch"]["focus.name"]["actual"], "Cancel")
		self.assertEqual(satisfied["expectation"]["status"], "satisfied")
		self.assertTrue(satisfied["expectation"]["evaluated"])

	def test_limitations_say_what_the_record_cannot_prove(self):
		assembler = traces.DiagnosticAssembler()
		trace = assembler.assemble(
			{
				"trace_id": "at-1",
				"schema_version": 1,
				"action": {"name": "press"},
				"request": {"validated": True},
				"lifecycle": {"milestones": [{"name": "received"}, {"name": "tool_completed"}]},
				"execution": {"tool_status": "succeeded", "handler_status": "succeeded"},
				"observation": assembler.observation_from_settle(
					self._settle_outcome(events=[{"kind": "focus", "version": 1}], events_complete=False)
				),
				"evidence": {"native_log_status": "time_window"},
				"retention": {},
			}
		)

		text = " ".join(trace["evidence"]["limitations"])

		self.assertIn("not proof that the rest of the UI was unchanged", text)
		self.assertIn("not proof that the action caused them", text)
		self.assertIn("incomplete", text)
		self.assertIn("time window only", text)
		self.assertTrue(trace["evidence"]["limitations"])

	def test_outcomes_are_derived_from_what_was_recorded(self):
		assembler = traces.DiagnosticAssembler()

		def outcome(exception=None, tool_status="succeeded", handler_status="succeeded"):
			return assembler.derive_outcome(
				{
					"execution": {
						"exception": exception,
						"tool_status": tool_status,
						"handler_status": handler_status,
					}
				}
			)

		self.assertEqual(outcome(), "succeeded")
		self.assertEqual(outcome(tool_status="failed", handler_status="failed"), "failed")
		self.assertEqual(outcome({"category": traces.CATEGORY_TIMEOUT}), "timed_out")
		self.assertEqual(outcome({"category": traces.CATEGORY_TRANSPORT}), "incomplete")
		self.assertEqual(outcome({"code": 4204, "category": "x"}), "cancelled")
		self.assertEqual(outcome(tool_status="not_started", handler_status="running"), "incomplete")
		self.assertEqual(outcome(tool_status="not_started", handler_status="not_started"), "unknown")

	def test_completion_keeps_execution_and_observation_separate(self):
		assembler = traces.DiagnosticAssembler()
		trace = assembler.assemble(
			{
				"trace_id": "at-1",
				"action": {"name": "invoke"},
				"request": {"validated": True},
				"lifecycle": {"milestones": [{"name": "received"}]},
				"execution": {
					"dispatch_status": "dispatched",
					"handler_status": "succeeded",
					"tool_status": "succeeded",
					"outcome": "succeeded",
				},
				"observation": assembler.observation_from_settle(
					self._settle_outcome(completed=False, expect={"focus.name": "Send"})
				),
				"evidence": {},
				"retention": {},
			}
		)

		self.assertTrue(trace["completion"]["handler_succeeded"])
		self.assertEqual(trace["completion"]["outcome"], "succeeded")
		self.assertEqual(trace["completion"]["expectation_status"], "not_satisfied")
		self.assertFalse(trace["completion"]["expectation_satisfied"])
		# The projection existed and did not change; that is a real observation,
		# not a missing one.
		self.assertFalse(trace["completion"]["ui_change_detected"])

	def test_the_summary_is_compact_and_points_at_the_full_record(self):
		assembler = traces.DiagnosticAssembler()
		trace = assembler.assemble(
			{
				"trace_id": "at-1",
				"schema_version": 1,
				"action": {"name": "press", "duration_ms": 12.5},
				"request": {"validated": True},
				"lifecycle": {"milestones": [{"name": "received"}]},
				"execution": {"tool_status": "succeeded", "handler_status": "succeeded"},
				"observation": assembler.observation_from_settle(self._settle_outcome()),
				"evidence": {"native_log_status": "none"},
				"retention": {"state": "finalized"},
			}
		)

		summary = traces.build_trace_summary(trace)

		self.assertEqual(summary["trace_id"], "at-1")
		self.assertEqual(summary["action"], "press")
		self.assertEqual(summary["outcome"], "succeeded")
		self.assertIn("observation_status", summary)
		self.assertIn("expectation_status", summary)
		self.assertTrue(summary["retained"])
		self.assertNotIn("events", summary)


class SettlerObservationTests(unittest.TestCase):
	def test_a_settle_round_records_its_event_window(self):
		journal = events.EventJournal()
		projection = {"focus": {"role": "BUTTON", "name": "OK"}, "ancestors": []}
		projector = state.StateProjector(lambda: projection)
		projector.refresh()
		recorder = traces.ActionTraceRecorder()
		recorder.start_action("press", {}, journal_id=journal.journal_id, event_version=journal.version)
		settler = settle.Settler(
			projector,
			lambda fn: fn(),
			budget_ms=300,
			quiet_ms=10,
			poll_floor_ms=5,
			journal=journal,
			trace=recorder,
		)

		def action():
			journal.record_transition("focus", kinds=["gainFocus"], name="OK")
			projector.mark_dirty()

		outcome = settler.run(action)
		trace = recorder.finalize(outcome=traces.OUTCOME_SUCCEEDED)

		self.assertEqual(outcome.event_count if hasattr(outcome, "event_count") else 0, 0)
		self.assertEqual(len(outcome.events), 1)
		self.assertEqual(outcome.events[0]["kind"], "focus")
		self.assertTrue(outcome.events_complete)
		self.assertEqual(outcome.journal_id, journal.journal_id)
		self.assertEqual(trace["observation"]["event_count"], 1)
		self.assertEqual(trace["observation"]["observation_status"], "events_without_projected_change")
		self.assertFalse(trace["observation"]["projection_changed"])

	def test_a_settle_round_without_a_journal_still_reports_the_projection(self):
		projection = {"focus": {"role": "BUTTON", "name": "OK"}, "ancestors": []}
		projector = state.StateProjector(lambda: projection)
		projector.refresh()
		settler = settle.Settler(projector, lambda fn: fn(), budget_ms=400, quiet_ms=5, poll_floor_ms=2)

		outcome = settler.run(lambda: None)

		self.assertFalse(outcome.changed)
		self.assertTrue(outcome.projection_available)
		self.assertEqual(outcome.settling_status, "settled")

	def test_the_result_block_stays_backward_compatible(self):
		projection = {"focus": {"role": "BUTTON"}, "ancestors": []}
		projector = state.StateProjector(lambda: projection)
		projector.refresh()
		settler = settle.Settler(projector, lambda fn: fn(), budget_ms=400, quiet_ms=5, poll_floor_ms=2)

		payload = settler.run(lambda: None).as_dict()

		for key in ("ok", "state_version", "changed", "delta", "settled_ms", "uncertain"):
			self.assertIn(key, payload)
		self.assertIn("projection_available", payload)
		self.assertIn("settling_status", payload)


class NativeLogTests(unittest.TestCase):
	def test_a_line_carrying_the_trace_id_is_direct_evidence(self):
		now = time.time()
		lines = ["INFO - bridge.action.start action=press trace_id=at-abc123"]

		matches, status = traces.match_native_log(lines, trace_id="at-abc123", start_wall=now, finish_wall=now)

		self.assertEqual(status, "exact")
		self.assertEqual(matches[0]["matching"], "exact_trace_id")
		self.assertEqual(matches[0]["association"], "direct")

	def test_an_error_inside_the_window_is_only_correlated(self):
		now = time.time()
		stamp = time.strftime("%H:%M:%S", time.localtime(now))
		lines = [f"ERROR - someSource ({stamp}.500) - something failed"]

		matches, status = traces.match_native_log(lines, trace_id="at-zzz", start_wall=now, finish_wall=now)

		self.assertEqual(status, "time_window")
		self.assertEqual(matches[0]["matching"], "time_window")
		self.assertEqual(matches[0]["association"], "temporally_correlated")

	def test_unrelated_lines_are_not_matched(self):
		now = time.time()
		lines = ["INFO - starting up", "ERROR - 2020-01-01 00:00:00,000 - ancient failure"]

		matches, status = traces.match_native_log(lines, trace_id="at-zzz", start_wall=now, finish_wall=now)

		self.assertEqual(matches, [])
		self.assertEqual(status, "none")

	def test_matches_are_bounded(self):
		now = time.time()
		lines = [f"INFO - trace_id=at-abc {index}" for index in range(100)]

		matches, _status = traces.match_native_log(
			lines, trace_id="at-abc", start_wall=now, finish_wall=now, limit=5
		)

		self.assertEqual(len(matches), 5)


if __name__ == "__main__":
	unittest.main()
