# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Contract tests for input strictness, output coercion and error mapping.

These cover the defects found by exercising the live bridge:

* wrong-typed input was accepted and coerced (``"false"`` became ``True``, a
  string satisfied a boolean and restarted NVDA with add-ons disabled);
* handler output fields missing from the model were silently dropped;
* every tool failure surfaced as the generic 5303 code.
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

_fake_logger = types.SimpleNamespace(
	debug=lambda *args, **kwargs: None,
	info=lambda *args, **kwargs: None,
	warning=lambda *args, **kwargs: None,
	error=lambda *args, **kwargs: None,
)
sys.modules.setdefault("logHandler", types.SimpleNamespace(log=_fake_logger))
for _name in (
	"api",
	"ui",
	"keyboardHandler",
	"controlTypes",
	"textInfos",
	"speech",
	"core",
	"versionInfo",
	"globalVars",
	"config",
	"winUser",
	"tones",
	"speechDictHandler",
	"addonHandler",
):
	sys.modules.setdefault(_name, types.ModuleType(_name))

_PACKAGE_NAME = "nvdaMcpBridge"
if _PACKAGE_NAME not in sys.modules:
	_package = types.ModuleType(_PACKAGE_NAME)
	_package.__path__ = [_ADDON_DIR]
	sys.modules[_PACKAGE_NAME] = _package

_core = importlib.import_module(f"{_PACKAGE_NAME}.core")
_models = importlib.import_module(f"{_PACKAGE_NAME}.tools.models")
_ui = importlib.import_module(f"{_PACKAGE_NAME}.nvda_bridge.ui")


class InputStrictnessTests(unittest.TestCase):
	"""Wrong-typed input must be rejected, never coerced."""

	def _schema(self, model):
		return _core.InputSchema(model)

	def test_string_is_not_a_boolean(self):
		schema = self._schema(_models.HealthSnapshotInput)
		with self.assertRaises(_core.ValidationError):
			schema.validate({"include_logs": "no"})

	def test_int_is_not_a_boolean(self):
		schema = self._schema(_models.InspectObjectInput)
		with self.assertRaises(_core.ValidationError):
			schema.validate({"include_relations": 1})

	def test_bool_is_not_an_integer(self):
		schema = self._schema(_models.WalkObjectTreeInput)
		with self.assertRaises(_core.ValidationError):
			schema.validate({"max_depth": True})

	def test_string_is_not_an_integer(self):
		schema = self._schema(_models.PressInput)
		with self.assertRaises(_core.ValidationError):
			schema.validate({"keys": "shift", "repeat": "2"})

	def test_integer_is_not_a_string(self):
		schema = self._schema(_models.PressInput)
		with self.assertRaises(_core.ValidationError):
			schema.validate({"keys": 5})

	def test_literal_is_enforced(self):
		schema = self._schema(_models.InspectObjectInput)
		with self.assertRaises(_core.ValidationError):
			schema.validate({"target": "mouse"})

	def test_unknown_field_is_rejected(self):
		schema = self._schema(_models.RestartNvdaInput)
		with self.assertRaises(_core.ValidationError):
			schema.validate({"disable_addons": False, "force": True})

	def test_valid_input_still_passes(self):
		schema = self._schema(_models.PressInput)
		validated = schema.validate({"keys": "control+a", "repeat": 3, "delay_ms": 40})
		self.assertEqual(validated.repeat, 3)


class OutputCoercionTests(unittest.TestCase):
	def test_extra_output_field_is_a_contract_error(self):
		tool = _core.TypedToolImpl.from_inputless(
			_core.ToolMetadata(
				name="output_extra",
				version="1.0.0",
				description="test",
				category="test",
			),
			_models.PressResult,
			lambda: {
				"gesture": "shift",
				"repeat": 1,
				"delay_ms": 0,
				"sent": True,
				"unexpected": "field",
			},
		)

		result = _core.ToolExecutor().execute(tool, {})

		self.assertFalse(result.success)
		self.assertEqual(result.error.error_code, _core.ErrorCode.INVALID_OUTPUT_SCHEMA)

	def test_declared_fields_are_accepted(self):
		tool = _core.TypedToolImpl.from_inputless(
			_core.ToolMetadata(name="output_ok", version="1.0.0", description="test", category="test"),
			_models.PressResult,
			lambda: {
				"gesture": "shift",
				"repeat": 1,
				"delay_ms": 0,
				"sent": True,
				"sent_count": 1,
				"cancelled": False,
				"dispatched": True,
				"uncertain": False,
			},
		)

		result = _core.ToolExecutor().execute(tool, {})

		self.assertTrue(result.success, msg=str(result.error))
		self.assertTrue(result.output.dispatched)


class CoordinatorTests(unittest.TestCase):
	def test_coordinator_receives_context(self):
		seen = {}

		def handler(input_obj, context):
			seen["context"] = context
			seen["keys"] = input_obj.keys
			return {"gesture": "shift", "repeat": 1, "delay_ms": 0, "sent": True, "sent_count": 1, "dispatched": True}

		tool = _core.TypedToolImpl.from_coordinator(
			_core.ToolMetadata(
				name="coordinator_tool",
				version="1.0.0",
				description="test",
				category="test",
				execution_mode=_core.ExecutionMode.COORDINATOR,
			),
			_models.PressInput,
			_models.PressResult,
			handler,
		)
		context = _core.ExecutionContext(timeout=5.0)

		result = _core.ToolExecutor().execute(tool, {"keys": "shift"}, context)

		self.assertTrue(result.success, msg=str(result.error))
		self.assertIs(seen["context"], context)

	def test_coordinator_requires_coordinator_mode(self):
		with self.assertRaises(ValueError):
			_core.TypedToolImpl.from_coordinator(
				_core.ToolMetadata(name="bad_mode", version="1.0.0", description="test", category="test"),
				_models.PressInput,
				_models.PressResult,
				lambda input_obj, context: {},
			)

	def test_main_thread_tool_ignores_context(self):
		def handler(input_obj):
			return {"gesture": input_obj.keys, "repeat": 1, "delay_ms": 0, "sent": True, "sent_count": 1, "dispatched": True}

		tool = _core.TypedToolImpl.from_handler(
			_core.ToolMetadata(name="plain_tool", version="1.0.0", description="test", category="test"),
			_models.PressInput,
			_models.PressResult,
			handler,
		)

		result = _core.ToolExecutor().execute(tool, {"keys": "shift"}, _core.ExecutionContext())

		self.assertTrue(result.success, msg=str(result.error))


class NullHandlingTests(unittest.TestCase):
	"""Free-form values may be null; typed required fields may not.

	Add-on metadata reports ``isEnabled``/``isDisabled`` as null for running
	add-ons, and log entries report a null filter level. Treating those as
	"missing required field" rejected two working tools.
	"""

	def test_any_value_may_be_null(self):
		from nvdaMcpBridge.validation.dataclass_validator import validate_and_map

		output = validate_and_map(
			{
				"scope": "running",
				"count": 1,
				"runningCount": 1,
				"availableCount": 0,
				"addons": [{"name": "example", "isEnabled": None, "isBlocked": None}],
			},
			_models.AddonMetadataList,
		)

		self.assertIsNone(output.addons[0]["isEnabled"])

	def test_optional_nested_dict_may_have_null_values(self):
		from nvdaMcpBridge.validation.dataclass_validator import validate_and_map

		output = validate_and_map(
			{
				"timestamp": 1,
				"options": {
					"include_logs": True,
					"log_lines": 5,
					"include_tree": False,
					"tree_depth": 1,
					"tree_max_children": 5,
					"addon_scope": "running",
				},
				"errors": {},
				"ok": True,
				"errorCount": 0,
				"logs": {"log_file": "x", "returned_lines": 0, "max_lines": 5, "lines": [], "file_size": 0, "filter_level": None},
			},
			_models.HealthSnapshot,
		)

		self.assertIsNone(output.logs["filter_level"])

	def test_typed_required_field_still_rejects_null(self):
		from nvdaMcpBridge.validation.dataclass_validator import (
			ValidationError as DataclassValidationError,
			validate_and_map,
		)

		with self.assertRaises(DataclassValidationError):
			validate_and_map(
				{"timestamp": 1, "options": {}, "errors": {}, "ok": None, "errorCount": 0},
				_models.HealthSnapshot,
			)


class ErrorMappingTests(unittest.TestCase):
	def test_validation_errors_map_to_4103(self):
		self.assertEqual(_core.protocol_code_for_serialized({"error_code": "invalid_input_schema"}), 4103)

	def test_unknown_tool_maps_to_4102(self):
		self.assertEqual(_core.protocol_code_for_serialized({"error_code": "tool_not_found"}), 4102)

	def test_unknown_error_code_maps_to_internal(self):
		self.assertEqual(_core.protocol_code_for_serialized({"error_code": "something_new"}), 5303)
		self.assertEqual(_core.protocol_code_for_serialized({}), 5303)

	def test_explicit_protocol_code_wins(self):
		self.assertEqual(
			_core.protocol_code_for_serialized(
				{"error_code": "handler_execution_failed", "protocol_code": 4204},
			),
			4204,
		)

	def test_handler_error_preserves_exception_protocol_code(self):
		class Cancelled(Exception):
			protocol_code = 4204

			def to_details(self):
				return {"sent_count": 2}

		error = _core.HandlerError(
			tool_name="send_keyboard",
			version="1.0.0",
			message="cancelled",
			root_cause=Cancelled(),
			details={"sent_count": 2},
		)

		self.assertEqual(_core.protocol_code_for(error), 4204)
		self.assertEqual(error.as_dict()["protocol_code"], 4204)


class GestureAliasTests(unittest.TestCase):
	def test_ctrl_maps_to_control(self):
		self.assertEqual(_ui.normalize_gesture_name("ctrl+a"), "control+a")

	def test_esc_maps_to_escape(self):
		self.assertEqual(_ui.normalize_gesture_name("shift+esc"), "shift+escape")

	def test_win_maps_to_windows(self):
		self.assertEqual(_ui.normalize_gesture_name("win+d"), "windows+d")

	def test_already_normalised_is_unchanged(self):
		self.assertEqual(_ui.normalize_gesture_name("control+shift+tab"), "control+shift+tab")


if __name__ == "__main__":
	_ = unittest.main()
