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
_package = sys.modules[_PACKAGE_NAME]

# ---------------------------------------------------------------------------
# NVDA-facing modules are replaced before the handlers import them. The point of
# these tests is the orchestration, not NVDA's API.
# ---------------------------------------------------------------------------

ACTION_CALLS = []


class _FakeActions:
	@staticmethod
	def do_action(obj, action=None):
		ACTION_CALLS.append(("do_action", action))
		return {"index": 1, "actionName": "Press", "available": ["Press", "Focus"]}

	@staticmethod
	def set_value(obj, value):
		ACTION_CALLS.append(("set_value", value))
		return {"mode": "platform", "value": value}

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


def _fake_send_keyboard(gesture, repeat=1, delay_ms=0, **kwargs):
	return {
		"gesture": gesture,
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


_fake_bridge.targets = _FakeTargets

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


sys.modules.setdefault("api", types.SimpleNamespace(getFocusObject=lambda: "focus-object"))

handlers = importlib.import_module(f"{_PACKAGE_NAME}.tools.handlers")
registry_module = importlib.import_module(f"{_PACKAGE_NAME}.tools.registry")
output_types = importlib.import_module(f"{_PACKAGE_NAME}.validation.output_type_registry")
core = importlib.import_module(f"{_PACKAGE_NAME}.core")
settle_module = importlib.import_module(f"{_PACKAGE_NAME}.server.settle")

NEW_TOOLS = {
	"observe",
	"invoke",
	"set_value",
	"focus",
	"press",
	"read",
	"navigate",
	"scroll",
	"dismiss",
	"click_at",
	"wait_for_events",
}


class FakeContext:
	def __init__(self, projections=None, wait_result=None):
		self.calls = []
		self.result = core.ResultMeta()
		self._projections = list(projections or [])
		self._wait_result = wait_result

	# -- main-thread operations -------------------------------------------
	def dispatch(self, operation):
		self.calls.append("dispatch")
		return operation()

	def resolve(self, ref):
		self.calls.append(("resolve", ref))
		return "resolved-object"

	def register(self, obj):
		self.calls.append(("register", obj))
		return "n1"

	def observe(self, include, max_text):
		self.calls.append(("observe", include, max_text))
		return {"state_version": 4, "revision": 9, "focus": {"ref": "f", "name": "field"}}

	def wait_events(self, since, timeout_ms, kinds):
		self.calls.append(("wait_events", since, timeout_ms, kinds))
		return self._wait_result or {"from_version": since, "to_version": since, "events": []}

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

	def test_superseded_tools_are_marked_deprecated(self):
		expected = {
			"get_current_focus": "observe",
			"get_navigator_object": "observe",
			"read_current_line": "read",
			"move_focus": "navigate",
			"send_keyboard": "press",
		}
		for name, replacement in expected.items():
			metadata = self.registry.get(name).get_metadata()
			self.assertTrue(metadata.deprecated, f"{name} should be deprecated")
			self.assertEqual(metadata.superseded_by, replacement)

	def test_every_new_tool_publishes_an_output_type(self):
		types_registry = output_types.build_output_type_registry()
		for name in NEW_TOOLS:
			model_name = self.registry.get(name).get_output_schema().model.__name__
			self.assertIn(model_name, types_registry, name)

	def test_press_requires_keys(self):
		tool = self.registry.get("press")
		schema = tool.get_input_schema()
		self.assertIn("keys", schema.to_json_schema()["required"])
		with self.assertRaises(core.ValidationError):
			schema.validate({"repeat": 2})

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

	def test_observe_dispatches_the_capture(self):
		context = FakeContext()
		result = handlers.observe(include=["focus", "dialog"], max_text=50, context=context)
		self.assertIn("dispatch", context.calls)
		self.assertEqual(result["state_version"], 4)
		self.assertIn(("observe", ["focus", "dialog"], 50), context.calls)

	def test_invoke_resolves_then_settles_and_reports_the_action(self):
		context = FakeContext()
		result = handlers.invoke(ref="f", action=None, expect={"focus.name": "b"}, context=context)
		self.assertIn(("resolve", "f"), context.calls)
		self.assertIn(("do_action", None), ACTION_CALLS)
		self.assertTrue(any(call[0] == "settle" and call[1] == {"focus.name": "b"} for call in context.calls))
		self.assertEqual(result["actionName"], "Press")
		self.assertEqual(result["index"], 1)
		self.assertTrue(result["invoked"])
		self.assertIsNotNone(context.result.settle)

	def test_press_reports_dispatch_and_settles(self):
		context = FakeContext()
		result = handlers.press(keys="tab", repeat=2, delay_ms=10, expect=None, context=context)
		self.assertTrue(result["dispatched"])
		self.assertEqual(result["sent_count"], 2)
		self.assertEqual(result["gesture"], "tab")

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
		context = FakeContext()
		result = handlers.set_value(ref="f", value="abc", expect=None, context=context)
		self.assertEqual(result["mode"], "platform")
		self.assertEqual(result["value"], "abc")

	def test_dismiss_tries_escape_then_the_default_button(self):
		# First settle leaves a dialog, second one removes it.
		context = FakeContext(projections=[{"dialog": {"name": "Replace"}}, {"dialog": None}])
		result = handlers.dismiss(expect=None, context=context)
		self.assertTrue(result["dismissed"])
		self.assertIn(("gesture", "escape"), ACTION_CALLS)
		self.assertTrue(any(step.startswith("activated button") for step in result["steps"]))

	def test_dismiss_reports_a_dialog_that_survived(self):
		context = FakeContext(projections=[{"dialog": {"name": "Save"}}, {"dialog": {"name": "Save"}}])
		result = handlers.dismiss(expect=None, context=context)
		self.assertFalse(result["dismissed"])
		self.assertEqual(result["dialogName"], "Save")

	def test_click_at_reports_the_hit_object(self):
		context = FakeContext()
		result = handlers.click_at(x=10, y=20, button="left", expect=None, context=context)
		self.assertEqual(result["hit"], {"name": "Run"})
		self.assertEqual((result["x"], result["y"]), (10, 20))

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


if __name__ == "__main__":
	unittest.main()
