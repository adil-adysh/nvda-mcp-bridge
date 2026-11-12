# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Executor instrumentation: every pipeline stage reaches the trace.

The rule these pin is that adding diagnostics changes nothing about how a call
fails: the same ``ExecutionResult`` and the same error codes come back, and the
trace is what gained a record.
"""

import dataclasses
import importlib
import os
import sys
import types
import unittest
from unittest import mock

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

core = importlib.import_module(f"{_PACKAGE_NAME}.core")
executor_module = importlib.import_module(f"{_PACKAGE_NAME}.core.executor")
traces = importlib.import_module(f"{_PACKAGE_NAME}.server.action_trace")


@dataclasses.dataclass(frozen=True)
class _Input:
	value: str = "x"


@dataclasses.dataclass(frozen=True)
class _Output:
	echo: str


def _tool(handler):
	return core.TypedToolImpl.from_handler(
		core.ToolMetadata(name="echo_value", version="1.0.0", description="Echo.", category="test"),
		_Input,
		_Output,
		handler,
	)


class ExecutorTraceTests(unittest.TestCase):
	def setUp(self):
		self.executor = core.ToolExecutor()

	def _recorder(self):
		recorder = traces.ActionTraceRecorder()
		recorder.start_action("echo_value", {"value": "hi"})
		return recorder

	def test_a_successful_call_records_every_stage(self):
		recorder = self._recorder()

		result = self.executor.execute(_tool(lambda value: {"echo": value.value}), {"value": "hi"}, trace=recorder)

		self.assertTrue(result.success)
		execution = recorder.trace["execution"]
		self.assertEqual(execution["dispatch_status"], "dispatched")
		self.assertEqual(execution["handler_status"], "succeeded")
		self.assertEqual(execution["tool_status"], "succeeded")
		self.assertIsNone(execution["exception"])

	def test_invalid_input_is_recorded_before_the_handler_runs(self):
		recorder = self._recorder()
		called = []

		result = self.executor.execute(
			_tool(lambda value: called.append(1) or {"echo": "x"}),
			{"unexpected": "field"},
			trace=recorder,
		)

		self.assertFalse(result.success)
		self.assertEqual(called, [], "the handler must not run on invalid input")
		self.assertFalse(recorder.trace["request"]["validated"])
		self.assertEqual(recorder.trace["execution"]["failure_stage"], "validation")
		self.assertEqual(recorder.trace["execution"]["tool_status"], "failed")

	def test_a_handler_exception_is_captured_structurally(self):
		recorder = self._recorder()

		def explode(_value):
			raise LookupError("no such element")

		result = self.executor.execute(_tool(explode), {"value": "hi"}, trace=recorder)

		self.assertFalse(result.success)
		self.assertEqual(result.error.error_code.value, "handler_execution_failed")
		exception = recorder.trace["execution"]["exception"]
		self.assertEqual(exception["type"], "LookupError")
		self.assertEqual(exception["message"], "no such element")
		self.assertEqual(exception["stage"], "handler")
		self.assertEqual(exception["category"], traces.CATEGORY_HANDLER)
		self.assertIn("Traceback", exception["traceback"])
		self.assertEqual(recorder.trace["execution"]["handler_status"], "failed")

	def test_a_bad_argument_still_maps_to_the_callers_error_code(self):
		recorder = self._recorder()

		def reject(_value):
			raise ValueError("'value' must be short")

		result = self.executor.execute(_tool(reject), {"value": "hi"}, trace=recorder)

		self.assertEqual(result.error.error_code.value, "invalid_input_type")
		self.assertEqual(recorder.trace["execution"]["error_category"], traces.CATEGORY_INPUT_VALIDATION)

	def test_a_domain_error_keeps_its_wire_code(self):
		recorder = self._recorder()

		class StaleReference(LookupError):
			protocol_code = 4206

		def stale(_value):
			raise StaleReference("the element is gone")

		result = self.executor.execute(_tool(stale), {"value": "hi"}, trace=recorder)

		self.assertEqual(result.error.protocol_code, 4206)
		self.assertEqual(recorder.trace["execution"]["exception"]["code"], 4206)
		self.assertEqual(recorder.trace["execution"]["error_category"], traces.CATEGORY_DOMAIN)

	def test_an_output_contract_violation_is_recorded_at_its_own_stage(self):
		recorder = self._recorder()

		result = self.executor.execute(_tool(lambda value: {"wrong": "field"}), {"value": "hi"}, trace=recorder)

		self.assertFalse(result.success)
		self.assertEqual(result.error.error_code.value, "invalid_output_schema")
		# The coercion happens inside the handler wrapper, so the stage is the
		# handler; the category is what says it was the output contract.
		self.assertEqual(recorder.trace["execution"]["failure_stage"], "handler")
		self.assertEqual(recorder.trace["execution"]["error_category"], traces.CATEGORY_SERIALIZATION)
		self.assertEqual(recorder.trace["execution"]["handler_status"], "failed")

	def test_a_serialization_failure_is_recorded_and_still_raised(self):
		recorder = self._recorder()
		with mock.patch.object(
			executor_module.ExecutionResult, "as_dict", side_effect=TypeError("not serializable")
		):
			with self.assertRaises(TypeError):
				self.executor.execute_and_serialize(
					_tool(lambda value: {"echo": value.value}), {"value": "hi"}, trace=recorder
				)

		self.assertEqual(recorder.trace["execution"]["failure_stage"], "serialization")
		self.assertEqual(recorder.trace["execution"]["tool_status"], "failed")

	def test_execution_without_a_recorder_is_unchanged(self):
		result = self.executor.execute_and_serialize(_tool(lambda value: {"echo": value.value}), {"value": "hi"})

		self.assertEqual(result, {"success": True, "output": {"echo": "hi"}})

	def test_a_broken_recorder_cannot_fail_the_call(self):
		class Exploding:
			def __getattr__(self, name):
				raise AssertionError("recorder must be called defensively")

		result = self.executor.execute(_tool(lambda value: {"echo": value.value}), {"value": "hi"}, trace=Exploding())

		self.assertTrue(result.success)

	def test_the_finished_trace_is_retrievable_after_a_handler_failure(self):
		recorder = self._recorder()
		store = traces.ActionTraceStore()

		def explode(_value):
			raise RuntimeError("boom")

		self.executor.execute(_tool(explode), {"value": "hi"}, trace=recorder)
		trace = recorder.finalize(store=store)

		stored = store.get(trace["trace_id"])
		self.assertEqual(stored["completion"]["outcome"], "failed")
		self.assertEqual(stored["execution"]["exception"]["type"], "RuntimeError")


if __name__ == "__main__":
	unittest.main()
