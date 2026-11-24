# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Tests for the action tool surface and its handlers (P2).

Two contracts are pinned here:

* the *published* surface — names, permissions, allowed states, deprecations and
  strict input schemas — because an agent discovers the vocabulary from it;
* the *handler orchestration* — that every action settles and every read does
  not claim more than it observed.
"""

import importlib
import os
import sys
import threading
import types
import unittest
from unittest.mock import patch

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
_package = sys.modules[_PACKAGE_NAME]

# ---------------------------------------------------------------------------
# NVDA-facing modules are replaced before the handlers import them. The point of
# these tests is the orchestration, not NVDA's API.
# ---------------------------------------------------------------------------

ACTION_CALLS = []


class _FakeActions:
	CANCEL_DIALOG_BUTTON = None

	@staticmethod
	def do_action(obj, action=None):
		ACTION_CALLS.append(("do_action", action))
		return {"index": 1, "actionName": "Press", "available": ["Press", "Focus"]}

	@staticmethod
	def set_value(obj, value, force_keyboard=False):
		ACTION_CALLS.append(("set_value", value, force_keyboard))
		mode = "keyboard" if force_keyboard else "platform"
		sticks = KEYBOARD_WRITE_STICKS if force_keyboard else PLATFORM_WRITE_STICKS
		if sticks:
			VALUE_AFTER_WRITE["value"] = value
		return {"mode": mode, "value": value, "units": 0 if mode == "platform" else len(value)}

	@staticmethod
	def set_focus(obj):
		ACTION_CALLS.append(("set_focus", None))
		return {"requested": True}

	@staticmethod
	def read_text(obj, scope="line", max_chars=4000):
		return {"text": "hello", "scope": scope, "truncated": False, "source": "textinfo"}

	@staticmethod
	def navigate(obj, direction):
		ACTION_CALLS.append(("navigate", direction))
		return ("target-object", True)

	@staticmethod
	def scroll(obj, direction, amount="page"):
		return {"method": "keyboard", "key": "pagedown"}

	@staticmethod
	def click_at(x, y, button="left"):
		return {"x": x, "y": y, "button": button, "hit": {"name": "Run"}}

	@staticmethod
	def dialog_object():
		return "dialog"

	@staticmethod
	def default_dialog_button(dialog):
		return "button"

	@staticmethod
	def cancel_dialog_button(dialog):
		return _FakeActions.CANCEL_DIALOG_BUTTON


#: What the fake control contains when `set_value` is read back afterwards.
#: Tests flip this to model a write that never landed.
VALUE_AFTER_WRITE = {"value": None}

#: Whether each write path actually applies. A browser's contenteditable accepts
#: the platform write and ignores it, so the default is "platform lies".
PLATFORM_WRITE_STICKS = False
KEYBOARD_WRITE_STICKS = True


def _fake_activate_window(title="", handle=0, exact=False):
	ACTION_CALLS.append(("activate_window", title, handle, exact))
	return {
		"activated": True,
		"handle": 4242,
		"title": title or "Notepad",
		"previous_title": "DeepSeek Harness",
		"method": "foreground",
		"restored": False,
		"waited_ms": 18.0,
		"matches": 1,
		"focus_in_window": True,
		"focus_window": "",
		"object": "window-object",
	}


def _fake_send_keyboard(gesture, repeat=1, delay_ms=0, **kwargs):
	return {
		"requested": gesture,
		"normalized_keys": gesture,
		"repeat": repeat,
		"delay_ms": delay_ms,
		"sent": True,
		"sent_count": repeat,
		"cancelled": False,
		"dispatched": True,
		"uncertain": False,
	}


_fake_bridge = types.ModuleType(f"{_PACKAGE_NAME}.nvda_bridge")
_fake_bridge.actions = _FakeActions
_fake_bridge.send_keyboard = _fake_send_keyboard
_fake_bridge.ui = types.SimpleNamespace(
	send_single_gesture=lambda gesture: ACTION_CALLS.append(("gesture", gesture)),
	resolve_gesture=lambda gesture: gesture,
)


class _FakeTargets:
	"""The dispatch seam between a ref and the primitive that acts on it.

	A handler does not know whether a reference points at an NVDA object or a
	located UIA element; this stands in for the module that does.
	"""

	invoke = staticmethod(_FakeActions.do_action)
	set_focus = staticmethod(_FakeActions.set_focus)
	set_value = staticmethod(_FakeActions.set_value)
	read_text = staticmethod(_FakeActions.read_text)
	navigate = staticmethod(_FakeActions.navigate)
	scroll = staticmethod(_FakeActions.scroll)

	@staticmethod
	def read_value(obj):
		return {"value": VALUE_AFTER_WRITE["value"], "source": "value"}


_fake_bridge.targets = _FakeTargets
_fake_bridge.windows = types.SimpleNamespace(
	activate=_fake_activate_window,
	# The fast path only triggers when the foreground window already matches;
	# these tests exercise the path that actually raises something.
	foreground_title=lambda: "DeepSeek Harness",
	foreground_handle=lambda: 99,
	title_matches=lambda candidate, wanted, exact=False: False,
	object_for_handle=lambda handle: "window-object",
)

_BRIDGE_KEY = f"{_PACKAGE_NAME}.nvda_bridge"
_original_bridge = sys.modules.get(_BRIDGE_KEY)


def setUpModule():
	# Install the fake only while this module runs: other test modules import the
	# real submodule and would break on a plain module standing in for a package.
	sys.modules[_BRIDGE_KEY] = _fake_bridge
	setattr(_package, "nvda_bridge", _fake_bridge)


def tearDownModule():
	if _original_bridge is not None:
		sys.modules[_BRIDGE_KEY] = _original_bridge
		setattr(_package, "nvda_bridge", _original_bridge)
	else:
		sys.modules.pop(_BRIDGE_KEY, None)
		_package.__dict__.pop("nvda_bridge", None)


sys.modules["api"] = types.SimpleNamespace(getFocusObject=lambda: "focus-object")

handlers = importlib.import_module(f"{_PACKAGE_NAME}.tools.handlers")
registry_module = importlib.import_module(f"{_PACKAGE_NAME}.tools.registry")
output_types = importlib.import_module(f"{_PACKAGE_NAME}.validation.output_type_registry")
dataclass_validator = importlib.import_module(f"{_PACKAGE_NAME}.validation.dataclass_validator")
models = importlib.import_module(f"{_PACKAGE_NAME}.tools.models")
core = importlib.import_module(f"{_PACKAGE_NAME}.core")
settle_module = importlib.import_module(f"{_PACKAGE_NAME}.server.settle")

NEW_TOOLS = {
	"observe",
	"invoke",
	"set_value",
	"focus",
	"activate_window",
	"press",
	"read",
	"navigate",
	"scroll",
	"dismiss",
	"click_at",
	"wait_for_events",
	"wait_for_speech",
}


class FakeContext:
	def __init__(self, projections=None, wait_result=None):
		self.calls = []
		self.result = core.ResultMeta()
		self._projections = list(projections or [])
		self._wait_result = wait_result
		self._speech_access_result = (True, None)
		self.cancellation_event = None

	# -- main-thread operations -------------------------------------------
	def dispatch(self, operation):
		self.calls.append("dispatch")
		return operation()

	def resolve(self, ref):
		self.calls.append(("resolve", ref))
		return "resolved-object"

	def ref_kind(self, ref):
		return "window" if ref == "w" else None

	def register(self, obj):
		self.calls.append(("register", obj))
		return "n1"

	def observe(self, include, max_text):
		self.calls.append(("observe", include, max_text))
		return {"state_version": 4, "revision": 9, "focus": {"ref": "f", "name": "field"}}

	def wait_events(self, since, timeout_ms, kinds, limit=100, journal_id=None, cancellation_event=None):
		self.calls.append(("wait_events", since, timeout_ms, kinds))
		return self._wait_result or {
			"journal_id": "test-journal",
			"from_version": since,
			"to_version": since,
			"events": [],
		}

	def speech_version(self):
		return 88

	def speech_access(self):
		return self._speech_access_result

	def wait_speech(self, since, timeout_ms, limit=20, journal_id=None, cancellation_event=None):
		self.calls.append(("wait_speech", since, timeout_ms, limit, journal_id, cancellation_event))
		return self._wait_result or {
			"journal_id": "speech-journal",
			"from_version": since,
			"to_version": since,
			"earliest_available_version": 0,
			"dropped": 0,
			"events": [],
			"expired": False,
			"resync_required": False,
			"resync_reason": None,
			"resync_from": None,
			"history_complete": True,
			"truncated": False,
			"has_more": False,
			"cursor_status": "ok",
			"timed_out": True,
			"cancelled": False,
		}

	# -- settle ------------------------------------------------------------
	def settle(self, action_fn, expect=None, budget_ms=None):
		self.calls.append(("settle", expect))
		action_fn()
		projection = self._projections.pop(0) if self._projections else {"dialog": None}
		changed = bool(self._projections)
		outcome = settle_module.SettleOutcome(
			changed=changed,
			state_version=5,
			settled_ms=123.0,
			delta={"name": {"from": "a", "to": "b"}} if changed else {},
			projection=projection,
		)
		self.result.settle = outcome
		return outcome


class SurfaceTests(unittest.TestCase):
	def setUp(self):
		self.registry = registry_module.NVDA_TOOLS_REGISTRY

	def test_every_planned_tool_exists(self):
		names = {meta.name for meta in self.registry.list_tools()}
		self.assertEqual(NEW_TOOLS - names, set())

	def test_action_tools_are_coordinators(self):
		for name in NEW_TOOLS:
			tool = self.registry.get(name)
			self.assertEqual(
				tool.get_metadata().execution_mode,
				core.ExecutionMode.COORDINATOR,
				f"{name} must run on the worker thread so it can settle",
			)

	def test_permissions_are_narrow(self):
		expected = {
			"observe": "read_only",
			"read": "read_only",
			"wait_for_events": "read_only",
			"wait_for_speech": "speech_observation",
			"invoke": "input_control",
			"set_value": "input_control",
			"focus": "input_control",
			"press": "input_control",
			"navigate": "input_control",
			"scroll": "input_control",
			"click_at": "input_control",
			"dismiss": "dialog_control",
		}
		for name, permission in expected.items():
			self.assertEqual(self.registry.get(name).get_metadata().required_permission, permission, name)

	def test_dismiss_is_allowed_in_modal_and_menu_states(self):
		states = self.registry.get("dismiss").get_metadata().allowed_nvda_states
		self.assertIn("modal", states)
		self.assertIn("menu", states)

	def test_press_can_navigate_menus_and_answer_message_boxes(self):
		states = self.registry.get("press").get_metadata().allowed_nvda_states
		self.assertIn("menu", states)
		self.assertIn("modal", states)

	def test_retired_aliases_are_gone(self):
		# api_level 3 retired the v1 aliases outright: they are not deprecated
		# placeholders, they are not in the registry at all, so an agent never
		# pays context for a name it must not use.
		for name in (
			"get_current_focus",
			"get_navigator_object",
			"read_current_line",
			"move_focus",
			"send_keyboard",
			"get_desktop_object",
			"get_window_title",
		):
			with self.assertRaises(Exception, msg=name):
				self.registry.get(name)

	def test_nothing_is_deprecated_anymore(self):
		deprecated = [meta.name for meta in self.registry.list_tools() if meta.deprecated]
		self.assertEqual(deprecated, [], "a deprecated tool is a tool that should have been retired")

	def test_every_new_tool_publishes_an_output_type(self):
		types_registry = output_types.build_output_type_registry()
		for name in NEW_TOOLS:
			model_name = self.registry.get(name).get_output_schema().model.__name__
			self.assertIn(model_name, types_registry, name)

	def test_press_requires_keys_or_a_sequence(self):
		tool = self.registry.get("press")
		schema = tool.get_input_schema()
		# Either form is valid, so neither is individually required; the handler
		# rejects the call that supplies neither.
		self.assertNotIn("keys", schema.to_json_schema().get("required", []))
		with self.assertRaises(ValueError):
			handlers.press(keys=None, repeat=1, delay_ms=0, expect=None, context=FakeContext())

	def test_observe_rejects_unknown_include_values(self):
		schema = self.registry.get("observe").get_input_schema()
		self.assertEqual(schema.validate({"include": ["focus"]}).include, ["focus"])
		with self.assertRaises(core.ValidationError):
			schema.validate({"include": ["everything"]})

	def test_expect_is_an_optional_object(self):
		schema = self.registry.get("invoke").get_input_schema()
		self.assertIsNone(schema.validate({"ref": "f"}).expect)
		self.assertEqual(schema.validate({"ref": "f", "expect": {"focus.name": "x"}}).expect, {"focus.name": "x"})
		with self.assertRaises(core.ValidationError):
			schema.validate({"ref": "f", "expect": "focus.name"})


class HandlerTests(unittest.TestCase):
	def setUp(self):
		ACTION_CALLS.clear()
		_FakeActions.CANCEL_DIALOG_BUTTON = None
		VALUE_AFTER_WRITE["value"] = None
		global PLATFORM_WRITE_STICKS, KEYBOARD_WRITE_STICKS
		PLATFORM_WRITE_STICKS = False
		KEYBOARD_WRITE_STICKS = True

	def test_observe_dispatches_the_capture(self):
		context = FakeContext()
		result = handlers.observe(include=["focus", "dialog"], max_text=50, context=context)
		self.assertIn("dispatch", context.calls)
		self.assertEqual(result["state_version"], 4)
		self.assertIn(("observe", ["focus", "dialog"], 50), context.calls)

	def test_observe_field_projection_keeps_required_role(self):
		class ObserveContext:
			def observe(self, _include, _max_text):
				return {
					"state_version": 4,
					"revision": 9,
					"refs": {"f": "focus"},
					"focus": {"ref": "f", "role": "EDITABLETEXT", "name": "field"},
				}

		for fields in (["name"], ["ref"]):
			with self.subTest(fields=fields):
				result = handlers.observe(
					include=["focus"], max_text=50, context=ObserveContext(), detail="full", fields=fields
				)
				validated = dataclass_validator.validate(result, models.ObserveResult)
				registry_module.NVDA_TOOLS_REGISTRY.get("observe").get_output_schema().validate(validated)
				self.assertEqual(result["focus"]["role"], "EDITABLETEXT")

	def test_invoke_resolves_then_settles_and_reports_the_action(self):
		context = FakeContext()
		result = handlers.invoke(ref="f", action=None, expect={"focus.name": "b"}, context=context)
		self.assertIn(("resolve", "f"), context.calls)
		self.assertIn(("do_action", None), ACTION_CALLS)
		self.assertTrue(any(call[0] == "settle" and call[1] == {"focus.name": "b"} for call in context.calls))
		self.assertEqual(result["action_name"], "Press")
		self.assertEqual(result["index"], 1)
		self.assertTrue(result["invoked"])
		self.assertIsNotNone(context.result.settle)

	def test_press_reports_dispatch_and_settles(self):
		context = FakeContext()
		result = handlers.press(keys="tab", repeat=2, delay_ms=10, expect=None, context=context)
		self.assertTrue(result["dispatched"])
		self.assertEqual(result["sent_count"], 2)
		self.assertEqual(result["normalized_keys"], "tab")

	def test_read_does_not_settle(self):
		context = FakeContext()
		result = handlers.read(ref=None, scope="document", max_chars=10, context=context)
		self.assertEqual(result["text"], "hello")
		self.assertEqual(result["scope"], "document")
		self.assertFalse(any(call[0] == "settle" for call in context.calls if isinstance(call, tuple)))

	def test_navigate_returns_a_reference_to_the_target(self):
		context = FakeContext()
		result = handlers.navigate(direction="next", ref="f", expect=None, context=context)
		self.assertIn(("register", "target-object"), context.calls)
		self.assertEqual(result["ref"], "n1")
		self.assertTrue(result["focused"])

	def test_set_value_reports_which_mechanism_was_used(self):
		globals()["PLATFORM_WRITE_STICKS"] = True
		context = FakeContext()
		result = handlers.set_value(ref="f", value="abc", expect=None, context=context)
		self.assertEqual(result["mode"], "platform")
		self.assertEqual(result["value"], "abc")
		self.assertTrue(result["verified"])
		self.assertEqual(result["observed"], "")
		self.assertFalse(context.result.settle.uncertain)
		# The write landed first time, so no keyboard retry was attempted.
		self.assertFalse(any(call == ("set_value", "abc", True) for call in ACTION_CALLS))

	def test_set_value_retries_with_keystrokes_when_the_platform_write_does_not_stick(self):
		context = FakeContext()
		result = handlers.set_value(ref="f", value="abc", expect=None, context=context)
		self.assertTrue(result["verified"])
		self.assertEqual(result["mode"], "keyboard")
		self.assertIn(("set_value", "abc", True), ACTION_CALLS)

	def test_set_value_reports_a_write_that_did_not_land(self):
		# Neither path applies: the handler must say so instead of claiming success.
		globals()["KEYBOARD_WRITE_STICKS"] = False
		context = FakeContext()
		result = handlers.set_value(ref="f", value="bridge round-trip", expect=None, context=context)
		self.assertFalse(result["verified"])
		self.assertEqual(result["observed"], "")
		self.assertTrue(context.result.settle.uncertain)

	def test_set_value_reports_what_the_control_actually_contains(self):
		globals()["KEYBOARD_WRITE_STICKS"] = False
		VALUE_AFTER_WRITE["value"] = "previous text"
		context = FakeContext()
		result = handlers.set_value(ref="f", value="new", expect=None, context=context)
		self.assertFalse(result["verified"])
		self.assertEqual(result["observed"], "previous text")
		self.assertTrue(context.result.settle.uncertain)

	def test_set_value_append_uses_the_current_value(self):
		globals()["PLATFORM_WRITE_STICKS"] = True
		VALUE_AFTER_WRITE["value"] = "prefix-"
		context = FakeContext()

		result = handlers.set_value(ref="f", value="suffix", update="append", expect=None, context=context)

		self.assertTrue(result["verified"])
		self.assertEqual(result["value"], "prefix-suffix")
		self.assertEqual(result["previous"], "prefix-")
		self.assertIn(("set_value", "prefix-suffix", False), ACTION_CALLS)

	def test_set_value_append_aborts_when_initial_value_is_unavailable(self):
		original_read_value = _FakeTargets.read_value

		def unavailable(_obj):
			raise RuntimeError("value could not be read")

		context = FakeContext()
		_FakeTargets.read_value = staticmethod(unavailable)
		try:
			with self.assertRaises(ValueError):
				handlers.set_value(
					ref="f", value="suffix", update="append", expect=None, context=context
				)
		finally:
			_FakeTargets.read_value = staticmethod(original_read_value)

		self.assertFalse(any(call[0] == "set_value" for call in ACTION_CALLS))
		self.assertFalse(any(call[0] == "settle" for call in context.calls if isinstance(call, tuple)))

	def test_press_sequence_validates_all_gestures_before_sending_any(self):
		original_send_keyboard = _fake_bridge.send_keyboard
		original_resolve_gesture = _fake_bridge.ui.resolve_gesture
		sent = []

		def resolve(gesture):
			if gesture == "invalid":
				raise ValueError("invalid gesture")
			return gesture

		def send_keyboard(gesture, **kwargs):
			_fake_bridge.ui.resolve_gesture(gesture)
			sent.append(gesture)
			return original_send_keyboard(gesture, **kwargs)

		_fake_bridge.send_keyboard = send_keyboard
		_fake_bridge.ui.resolve_gesture = resolve
		try:
			with self.assertRaises(ValueError):
				handlers.press(
					keys=None,
					sequence=["tab", "invalid"],
					repeat=1,
					delay_ms=0,
					expect=None,
					context=FakeContext(),
				)
		finally:
			_fake_bridge.send_keyboard = original_send_keyboard
			_fake_bridge.ui.resolve_gesture = original_resolve_gesture

		self.assertEqual(sent, [])

	def test_set_value_rejects_an_unknown_update_mode(self):
		with self.assertRaises(ValueError):
			handlers.set_value(ref="f", value="x", update="insert", expect=None, context=FakeContext())

	def test_press_sequence_sends_every_gesture_in_one_settle(self):
		context = FakeContext()
		result = handlers.press(
			keys=None,
			sequence=["f6", "f6", "f6"],
			repeat=1,
			delay_ms=0,
			expect=None,
			context=context,
		)
		self.assertEqual(result["sent_count"], 3)
		self.assertEqual(result["sequence"], ["f6", "f6", "f6"])
		self.assertEqual(result["requested"], "f6, f6, f6")

	def test_press_rejects_both_keys_and_a_sequence(self):
		with self.assertRaises(ValueError):
			handlers.press(
				keys="tab",
				sequence=["tab"],
				repeat=1,
				delay_ms=0,
				expect=None,
				context=FakeContext(),
			)

	def test_activate_window_registers_the_window_it_raised(self):
		context = FakeContext()
		result = handlers.activate_window(
			title="Notepad",
			handle=0,
			exact=False,
			expect=None,
			context=context,
		)
		self.assertIn(("activate_window", "Notepad", 0, False), ACTION_CALLS)
		self.assertIn(("register", "window-object"), context.calls)
		self.assertTrue(result["activated"])
		self.assertEqual(result["ref"], "n1")
		self.assertEqual(result["method"], "foreground")
		self.assertEqual(result["matches"], 1)

	def test_activate_window_needs_a_title_or_a_handle(self):
		with self.assertRaises(ValueError):
			handlers.activate_window(title="", handle=0, exact=False, expect=None, context=FakeContext())

	def test_activate_window_short_circuits_when_already_in_front(self):
		# No settle round: the answer is known before doing anything.
		fake_windows = types.SimpleNamespace(
			activate=_fake_activate_window,
			foreground_title=lambda: "DeepSeek Harness — Microsoft Edge",
			foreground_handle=lambda: 657444,
			title_matches=lambda candidate, wanted, exact=False: "harness" in candidate.casefold(),
			is_minimized=lambda handle: False,
			object_for_handle=lambda handle: "window-object",
			focus_window=lambda: (657444, "DeepSeek Harness — Microsoft Edge"),
		)
		original = _fake_bridge.windows
		_fake_bridge.windows = fake_windows
		try:
			context = FakeContext()
			result = handlers.activate_window(
				title="Harness",
				handle=0,
				exact=False,
				expect=None,
				context=context,
			)
		finally:
			_fake_bridge.windows = original

		self.assertEqual(result["method"], "already-foreground")
		self.assertEqual(result["handle"], 657444)
		self.assertEqual(result["ref"], "n1")
		self.assertTrue(result["focus_in_window"])
		self.assertFalse(any(isinstance(call, tuple) and call[0] == "settle" for call in context.calls))

	def test_activate_window_checks_expectation_even_if_already_in_front(self):
		fake_windows = types.SimpleNamespace(
			activate=_fake_activate_window,
			foreground_title=lambda: "Notepad",
			foreground_handle=lambda: 4242,
			title_matches=lambda *args: True,
			is_minimized=lambda handle: False,
			object_for_handle=lambda handle: "window-object",
			focus_window=lambda: (4242, "Notepad"),
		)
		context = FakeContext()
		expect = {"focus.name": "Review input"}
		original = _fake_bridge.windows
		_fake_bridge.windows = fake_windows
		try:
			handlers.activate_window(title="Notepad", handle=0, exact=True, expect=expect, context=context)
		finally:
			_fake_bridge.windows = original
		self.assertIn(("settle", expect), context.calls)

	def test_activate_window_reports_when_the_focus_did_not_follow(self):
		"""A raised window is not the same fact as a screen reader reading it.

		Windows moved the foreground, the keyboard focus stayed in the previous
		window, and an agent that read "activated" as "we are there now" would go
		on to search the wrong window.
		"""

		def diverged(title="", handle=0, exact=False):
			return {
				"activated": True,
				"handle": 4242,
				"title": title or "Notepad",
				"previous_title": "DeepSeek Harness — Microsoft Edge",
				"method": "foreground",
				"restored": False,
				"waited_ms": 21.0,
				"matches": 1,
				"focus_in_window": False,
				"focus_window": "DeepSeek Harness — Microsoft Edge",
				"object": "window-object",
			}

		fake_windows = types.SimpleNamespace(
			activate=diverged,
			foreground_handle=lambda: 657444,
			foreground_title=lambda: "DeepSeek Harness — Microsoft Edge",
			title_matches=lambda candidate, wanted, exact=False: False,
			is_minimized=lambda handle: False,
			focus_window=lambda: (657444, "DeepSeek Harness — Microsoft Edge"),
		)
		original = _fake_bridge.windows
		_fake_bridge.windows = fake_windows
		try:
			result = handlers.activate_window(
				title="Notepad",
				handle=0,
				exact=False,
				expect=None,
				context=FakeContext(),
			)
		finally:
			_fake_bridge.windows = original

		self.assertTrue(result["activated"])
		self.assertFalse(result["focus_in_window"])
		self.assertEqual(result["focus_window"], "DeepSeek Harness — Microsoft Edge")

	def test_activate_window_uses_focus_after_settling(self):
		context = FakeContext()
		context._projections = [{"focus_window_handle": 4242, "top_window": {"title": "Notepad"}}]
		with patch.object(
			_fake_bridge.windows,
			"activate",
			side_effect=lambda **kwargs: {
				**_fake_activate_window(**kwargs),
				"focus_in_window": False,
				"focus_window": "Previous window",
			},
		):
			result = handlers.activate_window(title="Notepad", handle=4242, exact=True, expect=None, context=context)
		self.assertTrue(result["focus_in_window"])
		self.assertEqual(result["focus_window"], "")

	def test_dismiss_never_activates_a_positive_default_button(self):
		# Escape did not close the prompt and no safe cancel control was identified.
		context = FakeContext(projections=[{"dialog": {"name": "Add-on Installation"}}, {"dialog": {"name": "Add-on Installation"}}])
		result = handlers.dismiss(expect=None, context=context)
		self.assertFalse(result["dismissed"])
		self.assertIn(("gesture", "escape"), ACTION_CALLS)
		self.assertFalse(any(call[0] == "do_action" for call in ACTION_CALLS))

	def test_dismiss_describes_the_state_after_escape(self):
		context = FakeContext(projections=[{"dialog": None}])
		result = handlers.dismiss(expect=None, context=context)
		self.assertTrue(result["dismissed"])
		self.assertEqual(result["steps"], ["pressed Escape", "no dialog remains after Escape"])

	def test_dismiss_uses_an_explicit_cancel_control_when_escape_does_not_close(self):
		_FakeActions.CANCEL_DIALOG_BUTTON = "no-button"
		context = FakeContext(projections=[{"dialog": {"name": "Add-on Installation"}}, {"dialog": None}])
		result = handlers.dismiss(expect=None, context=context)
		self.assertTrue(result["dismissed"])
		self.assertTrue(any(call[0] == "do_action" for call in ACTION_CALLS))

	def test_dismiss_reports_a_dialog_that_survived(self):
		context = FakeContext(projections=[{"dialog": {"name": "Save"}}, {"dialog": {"name": "Save"}}])
		result = handlers.dismiss(expect=None, context=context)
		self.assertFalse(result["dismissed"])
		self.assertEqual(result["dialog_name"], "Save")

	def test_click_at_reports_the_hit_object(self):
		context = FakeContext()
		result = handlers.click_at(x=10, y=20, button="left", expect=None, context=context)
		self.assertEqual(result["hit"], {"name": "Run"})
		self.assertEqual((result["x"], result["y"]), (10, 20))

	def test_click_at_reports_pointer_position_and_passes_expect_to_settle(self):
		context = FakeContext()
		expect = {"focus.name": "Done"}
		with patch.object(_FakeActions, "click_at", return_value={"onTarget": False}):
			result = handlers.click_at(x=10, y=20, button="left", expect=expect, context=context)
		self.assertFalse(result["pointer_on_target"])
		self.assertEqual(result["pointer_on_target"], result["on_target"])
		self.assertIn(("settle", expect), context.calls)

	def test_wait_for_events_reports_a_timeout_when_nothing_arrived(self):
		context = FakeContext(wait_result={"from_version": 7, "to_version": 7, "events": []})
		result = handlers.wait_for_events(since=7, timeout_ms=10, kinds=None, context=context)
		self.assertTrue(result["timed_out"])
		self.assertEqual(result["events"], [])

	def test_wait_for_events_returns_the_transitions(self):
		context = FakeContext(
			wait_result={"from_version": 7, "to_version": 8, "events": [{"kind": "focus"}], "truncated": False}
		)
		result = handlers.wait_for_events(since=7, timeout_ms=10, kinds=["focus"], context=context)
		self.assertFalse(result["timed_out"])
		self.assertEqual(result["to_version"], 8)
		self.assertEqual(result["events"], [{"kind": "focus"}])

	def test_wait_for_speech_defaults_to_speech_head_without_replaying_history(self):
		class SpeechContext(FakeContext):
			def event_version(self):
				return 999

			def speech_version(self):
				return 4

		context = SpeechContext()
		result = handlers.wait_for_speech(since=None, timeout_ms=0, context=context)
		self.assertIn(("wait_speech", 4, 0, 20, None, None), context.calls)
		self.assertEqual(result["events"], [])

	def test_wait_for_speech_preserves_typed_cursor_and_resync_metadata(self):
		page = {
			"journal_id": "speech-journal",
			"from_version": 2,
			"to_version": 3,
			"earliest_available_version": 4,
			"dropped": 3,
			"events": [{"kind": "speech", "version": 3, "text": "hello"}],
			"expired": True,
			"resync_required": True,
			"resync_reason": "cursor_expired",
			"resync_from": {"journal_id": "speech-journal", "version": 3},
			"history_complete": False,
			"truncated": True,
			"has_more": True,
			"cursor_status": "stale",
			"timed_out": False,
			"cancelled": False,
		}
		result = handlers.wait_for_speech(
			since=2, journal_id="speech-journal", timeout_ms=100, limit=1,
			context=FakeContext(wait_result=page),
		)
		for key, value in page.items():
			self.assertEqual(result[key], value, key)

	def test_wait_for_speech_validates_direct_handler_arguments(self):
		invalid = (
			{"since": True}, {"since": -1}, {"journal_id": ""},
			{"timeout_ms": True}, {"timeout_ms": -1}, {"timeout_ms": 30001},
			{"limit": True}, {"limit": 0}, {"limit": 257},
		)
		for override in invalid:
			with self.subTest(override=override), self.assertRaises(ValueError):
				arguments = {"since": 5, "journal_id": None, "timeout_ms": 100, "limit": 20}
				arguments.update(override)
				handlers.wait_for_speech(context=FakeContext(), **arguments)

	def test_wait_for_speech_passes_cancellation_and_only_times_out_without_progress(self):
		cancel = threading.Event()
		context = FakeContext(wait_result={
			"journal_id": "speech-journal", "from_version": 5, "to_version": 6,
			"events": [{"kind": "speech", "text": "next"}], "timed_out": True,
		})
		context.cancellation_event = cancel
		advanced = handlers.wait_for_speech(since=5, timeout_ms=200, context=context)
		self.assertIn(("wait_speech", 5, 200, 20, None, cancel), context.calls)
		self.assertFalse(advanced["timed_out"])
		context = FakeContext(wait_result={
			"journal_id": "speech-journal", "from_version": 5, "to_version": 5,
			"events": [], "timed_out": True,
		})
		self.assertTrue(handlers.wait_for_speech(since=5, timeout_ms=200, context=context)["timed_out"])

	def test_wait_for_speech_fails_closed_when_shared_access_gate_denies(self):
		context = FakeContext()
		context._speech_access_result = (False, "protected focus")
		with self.assertRaises(core.ToolExecutionError) as caught:
			handlers.wait_for_speech(since=0, timeout_ms=0, context=context)
		self.assertEqual(caught.exception.protocol_code, 4003)
		self.assertFalse(any(call[0] == "wait_speech" for call in context.calls))

	def test_wait_for_speech_registry_declares_observation_access_and_output(self):
		tool = registry_module.NVDA_TOOLS_REGISTRY.get("wait_for_speech", "1.0.0")
		metadata = tool.get_metadata()
		self.assertEqual(metadata.category, "observation")
		self.assertEqual(metadata.required_permission, "speech_observation")
		self.assertEqual(metadata.allowed_nvda_states, ("normal",))
		self.assertTrue(metadata.read_only)
		self.assertEqual(tool.get_input_schema().model().limit, 20)
		self.assertEqual(tool.get_output_schema().model.__name__, "WaitSpeechResult")


class NewContractDefaultsTests(unittest.TestCase):
	def test_observe_defaults_to_focus_and_dialog(self):
		defaults = registry_module.NVDA_TOOLS_REGISTRY.get_input_schema("observe", "1.0.0").model()
		self.assertEqual(defaults.include, ["focus", "dialog"])
		self.assertEqual(defaults.detail, "summary")
		self.assertLessEqual(defaults.max_text, 120)

	def test_health_snapshot_defaults_to_lean(self):
		defaults = registry_module.NVDA_TOOLS_REGISTRY.get_input_schema("health_snapshot", "1.0.0").model()
		self.assertTrue(defaults.include_focus)
		self.assertFalse(defaults.include_addons)
		self.assertFalse(defaults.include_logs)

	def test_wait_for_events_defaults_to_the_event_journal_head(self):
		class EventContext(FakeContext):
			def event_version(self):
				return 99

		context = EventContext(wait_result={"from_version": 99, "to_version": 99, "events": []})
		handlers.wait_for_events(since=None, timeout_ms=1, kinds=None, context=context)
		self.assertIn(("wait_events", 99, 1, None), context.calls)

	def test_wait_for_events_defaults_to_the_current_state_version(self):
		class StatefulContext(FakeContext):
			def state_version(self):
				return 42

		context = StatefulContext(wait_result={"from_version": 42, "to_version": 42, "events": []})
		handlers.wait_for_events(since=None, timeout_ms=1, kinds=None, context=context)
		self.assertIn(("wait_events", 42, 1, None), context.calls)

	def test_wait_for_events_can_return_counts_without_event_objects(self):
		context = FakeContext(
			wait_result={
				"journal_id": "j",
				"from_version": 1,
				"to_version": 2,
				"events": [{"kind": "focus"}, {"kind": "focus"}],
			}
		)
		result = handlers.wait_for_events(
			since=1, timeout_ms=10, kinds=None, context=context, include_events=False
		)
		self.assertEqual(result["events"], [])
		self.assertEqual(result["event_kinds"], {"focus": 2})

	def test_within_rejects_a_window_title(self):
		with self.assertRaises(ValueError) as caught:
			handlers.locate("BUTTON", None, False, "AI Assistant Settings", 10, 400, 500, FakeContext())
		self.assertIn("reference", str(caught.exception))

	def test_reading_rejects_a_window_ref_as_within(self):
		context = FakeContext()
		with self.assertRaises(ValueError) as caught:
			handlers.list_reading_targets("heading", 5, "w", 1000, context)
		self.assertIn("document or control", str(caught.exception))
		self.assertNotIn(("resolve", "w"), context.calls)


if __name__ == "__main__":
	unittest.main()
