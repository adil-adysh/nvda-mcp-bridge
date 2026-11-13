# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Reference identity and mutation safety (API level 1).

References are identity-based locators, not projection-version leases. These
tests pin the consequences: an unrelated ``state_version`` change does not
invalidate a live reference, an object or window that changed does, and a
mutating tool never searches for a replacement target.
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

# The handlers import the NVDA-side package lazily; importing it needs the NVDA
# modules it depends on to exist. These stubs are enough for a pure-Python test.
for _name in ("api", "textInfos", "speech", "ui", "winUser", "winKernel", "NVDAObjects"):
	if _name not in sys.modules:
		sys.modules[_name] = types.ModuleType(_name)
for _attr in ("POSITION_CARET", "POSITION_SELECTION", "POSITION_ALL", "POSITION_FIRST", "POSITION_LAST"):
	if not hasattr(sys.modules["textInfos"], _attr):
		setattr(sys.modules["textInfos"], _attr, 1)

_PACKAGE_NAME = "nvdaMcpBridge"
if _PACKAGE_NAME not in sys.modules:
	_package = types.ModuleType(_PACKAGE_NAME)
	_package.__path__ = [_ADDON_DIR]
	sys.modules[_PACKAGE_NAME] = _package

refs = importlib.import_module(f"{_PACKAGE_NAME}.server.refs")
handlers = importlib.import_module(f"{_PACKAGE_NAME}.tools.handlers")


class FakeObject:
	def __init__(self, role="BUTTON", name="Run", windowClassName="Chrome_WidgetWin_1", windowControlID=7,
				 windowHandle=4242, processID=100, runtime_id=None):
		self.role = role
		self.name = name
		self.windowClassName = windowClassName
		self.windowControlID = windowControlID
		self.windowHandle = windowHandle
		self.processID = processID
		self.runtime_id = runtime_id
		self.setFocus = lambda: True


def identity_of(obj):
	return {
		"role": obj.role,
		"name": obj.name,
		"windowClassName": obj.windowClassName,
		"windowControlID": obj.windowControlID,
		"windowHandle": obj.windowHandle,
		"processID": obj.processID,
	}


class Clock:
	def __init__(self):
		self.now = 1000.0

	def __call__(self):
		return self.now


class ReferenceIdentityTests(unittest.TestCase):
	def setUp(self):
		self.clock = Clock()
		self.table = refs.ReferenceTable(ttl_s=60, clock=self.clock, instance_id="bridge-1")

	def test_an_unrelated_state_version_change_does_not_invalidate_a_reference(self):
		obj = FakeObject()
		self.table.register({"f": obj}, state_version=1, identity_of=identity_of)
		# The screen moved on; the object did not.
		entry = self.table.get("f", identity_of=identity_of)
		self.assertIs(entry.obj, obj)
		self.assertEqual(entry.state_version, 1, "the captured version stays advisory metadata")

	def test_ttl_expiry_is_reported_as_ttl(self):
		self.table.register({"f": FakeObject()}, state_version=1, identity_of=identity_of)
		self.clock.now += 61
		with self.assertRaises(refs.StaleReference) as caught:
			self.table.get("f", identity_of=identity_of)
		self.assertEqual(caught.exception.reason_code, refs.REASON_TTL)
		self.assertEqual(caught.exception.protocol_code, 4206)

	def test_object_identity_mismatch_is_reported_as_identity(self):
		self.table.register({"f": FakeObject()}, state_version=1, identity_of=identity_of)
		self.table._refs["f"].obj.windowControlID = 8
		with self.assertRaises(refs.StaleReference) as caught:
			self.table.get("f", identity_of=identity_of)
		self.assertEqual(caught.exception.reason_code, refs.REASON_IDENTITY)
		self.assertIn("windowControlID", caught.exception.reason)

	def test_a_replaced_window_is_reported_as_window(self):
		self.table.register({"f": FakeObject()}, state_version=1, identity_of=identity_of)
		self.table._refs["f"].obj.windowHandle = 9999
		with self.assertRaises(refs.StaleReference) as caught:
			self.table.get("f", identity_of=identity_of)
		self.assertEqual(caught.exception.reason_code, refs.REASON_WINDOW)
		self.assertIn("windowHandle", caught.exception.reason)

	def test_a_reused_handle_in_another_process_is_not_the_same_window(self):
		self.table.register({"f": FakeObject(windowHandle=100)}, state_version=1, identity_of=identity_of)
		self.table._refs["f"].obj.processID = 200
		with self.assertRaises(refs.StaleReference) as caught:
			self.table.get("f", identity_of=identity_of)
		self.assertEqual(caught.exception.reason_code, refs.REASON_WINDOW)

	def test_missing_strong_identity_fails_safe(self):
		class Anonymous:
			role = "GROUPING"
			name = "Panel"
			windowClassName = ""
			windowControlID = 0
			windowHandle = 0
			processID = 0

		weak_identity = lambda obj: {  # noqa: E731
			"role": obj.role,
			"name": obj.name,
			"controlType": 50026,
		}
		self.table.register({"n1": Anonymous()}, state_version=1, identity_of=weak_identity)
		with self.assertRaises(refs.StaleReference) as caught:
			self.table.get("n1", identity_of=weak_identity)
		self.assertEqual(caught.exception.reason_code, refs.REASON_IDENTITY)
		self.assertIn("unavailable", caught.exception.reason)

	def test_a_bridge_instance_replacement_clears_cached_references(self):
		self.table.register({"f": FakeObject()}, state_version=1, identity_of=identity_of)
		self.assertTrue(self.table.bind_instance("bridge-2"))
		self.assertEqual(len(self.table), 0, "a reference cannot outlive its Bridge")
		with self.assertRaises(refs.StaleReference) as caught:
			self.table.get("f")
		self.assertEqual(caught.exception.reason_code, refs.REASON_UNAVAILABLE)
		self.assertFalse(self.table.bind_instance("bridge-2"), "same instance is not a replacement")

	def test_the_error_reports_scope_and_expected_form(self):
		self.table.register({"f": FakeObject()}, state_version=1, identity_of=identity_of)
		self.clock.now += 61
		with self.assertRaises(refs.StaleReference) as caught:
			self.table.get("f", identity_of=identity_of, scope="Notepad — notes.txt")
		details = caught.exception.to_details()
		self.assertEqual(details["name"], "STALE_REFERENCE")
		self.assertEqual(details["scope"], "Notepad — notes.txt")
		self.assertIn("observe", details["expected_reference"])
		self.assertEqual(details["recovery"]["action"], "observe")
		self.assertIn("does not end when state_version advances", details["recovery"]["hint"])

	def test_a_window_title_is_an_argument_error_not_a_stale_reference(self):
		with self.assertRaises(ValueError) as caught:
			handlers._validate_ref_argument("Notepad — notes.txt")
		self.assertIn("reference returned by observe", str(caught.exception))
		self.assertIsNone(handlers._validate_ref_argument(None))
		self.assertEqual(handlers._validate_ref_argument("n3"), "n3")


class MutationSafetyTests(unittest.TestCase):
	"""A stale target must stop the mutation; nothing may be substituted."""

	def setUp(self):
		self.clock = Clock()
		self.table = refs.ReferenceTable(ttl_s=60, clock=self.clock, instance_id="bridge-1")
		self.dispatched = []
		self.resolved = []
		self.context = self._context()

	def _context(self):
		table = self.table
		test = self

		class Context:
			def resolve(self, ref):
				test.resolved.append(ref)
				entry = table.get(ref, identity_of=identity_of)
				return entry.obj

			def dispatch(self, operation):
				return operation()

			def register(self, obj):
				return "n9"

			def settle(self, action_fn, expect=None, budget_ms=None):
				action_fn()
				return types.SimpleNamespace(uncertain=False, observed=True)

		context = Context()
		return context

	def test_a_stale_reference_never_dispatches_and_is_never_retried(self):
		self.table.register({"f": FakeObject()}, state_version=1, identity_of=identity_of)
		self.clock.now += 61
		with self.assertRaises(refs.StaleReference):
			handlers.invoke(ref="f", action=None, expect=None, context=self.context)
		self.assertEqual(self.resolved, ["f"], "exactly the submitted reference was attempted")
		self.assertEqual(self.dispatched, [], "no UI action ran")

	def test_no_alternative_object_is_selected_for_a_stale_reference(self):
		# A same-looking object exists, but nothing may look it up.
		self.table.register({"f": FakeObject()}, state_version=1, identity_of=identity_of)
		self.table.discard("f")
		with self.assertRaises(refs.StaleReference):
			handlers.focus_object(ref="f", expect=None, context=self.context)
		self.assertEqual(self.resolved, ["f"])
		self.assertEqual(self.table.list_refs(), [])

	def test_a_valid_reference_executes_the_intended_operation(self):
		obj = FakeObject()
		self.table.register({"f": obj}, state_version=1, identity_of=identity_of)
		result = handlers.focus_object(ref="f", expect=None, context=self.context)
		self.assertEqual(result, {"ref": "f", "requested": True})
		self.assertEqual(self.resolved, ["f"])


if __name__ == "__main__":
	unittest.main()
