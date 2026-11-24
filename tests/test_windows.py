# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Tests for `activate_window`: title matching, and the failure it reports.

Activation itself cannot be exercised here — it needs a real desktop and the
foreground rules — but the part that decides *which* window to raise is pure
logic and is where a wrong answer is most expensive: raising the wrong window
moves the user's screen and looks like a successful tool call.

The match rules mirror what people mean by a title: exact beats prefix beats
substring, and whitespace/case do not count.
"""

import importlib
import os
import sys
import types
import unittest
from unittest.mock import patch

_ADDON_DIR = os.path.normpath(
	os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "addon", "globalPlugins", "nvdaMcp-bridge")
)
if _ADDON_DIR not in sys.path:
	sys.path.insert(0, _ADDON_DIR)

_PACKAGE_NAME = "nvdaMcpBridge"
if _PACKAGE_NAME not in sys.modules:
	_package = types.ModuleType(_PACKAGE_NAME)
	_package.__path__ = [_ADDON_DIR]
	sys.modules[_PACKAGE_NAME] = _package

# `nvda_bridge/__init__.py` pulls in NVDA itself. Register a stand-in parent that
# carries the real directory as its search path, so `nvda_bridge.windows` and its
# sibling `errors` load straight from disk without the NVDA-facing imports.
_BRIDGE_KEY = f"{_PACKAGE_NAME}.nvda_bridge"
_bridge_package = types.ModuleType(_BRIDGE_KEY)
_bridge_package.__path__ = [os.path.join(_ADDON_DIR, "nvda_bridge")]
sys.modules[_BRIDGE_KEY] = _bridge_package

windows = importlib.import_module(f"{_BRIDGE_KEY}.windows")
errors = importlib.import_module(f"{_BRIDGE_KEY}.errors")


def _window(title, handle):
	return {"object": None, "handle": handle, "title": title}


WINDOWS = [
	_window("DeepSeek Harness — Microsoft Edge", 10),
	_window("*abc 77777 - Notepad", 20),
	_window("Notepad", 21),
	_window("Untitled - Notepad", 22),
	_window("Settings", 30),
]


class MatchTests(unittest.TestCase):
	def test_an_exact_title_wins_over_a_longer_one(self):
		chosen, matched = windows.match_window("Notepad", exact=False, windows=WINDOWS)
		self.assertEqual(chosen["handle"], 21)
		self.assertEqual(matched, 3)

	def test_exact_refuses_a_substring(self):
		chosen, matched = windows.match_window("Notepad", exact=True, windows=WINDOWS)
		self.assertEqual(chosen["handle"], 21)
		self.assertEqual(matched, 1)

	def test_a_prefix_beats_a_substring(self):
		chosen, _matched = windows.match_window(
			"*abc 77777",
			exact=False,
			windows=[_window("Saved *abc 77777 notes", 1), _window("*abc 77777 - Notepad", 2)],
		)
		self.assertEqual(chosen["handle"], 2)

	def test_matching_ignores_case_and_extra_spaces(self):
		chosen, _matched = windows.match_window(
			"  deepseek   HARNESS ",
			exact=False,
			windows=[_window("DeepSeek Harness — Microsoft Edge", 10)],
		)
		self.assertEqual(chosen["handle"], 10)

	def test_matching_ignores_zero_width_characters(self):
		# Edge puts a zero-width space in its own tab title; a query typed the way
		# a person says it ("Microsoft Edge") must still find the window.
		chosen, _matched = windows.match_window(
			"Microsoft Edge",
			exact=False,
			windows=[_window("Configure NVDA MCP in dsh - Personal - Microsoft\u200b Edge", 10)],
		)
		self.assertEqual(chosen["handle"], 10)

	def test_a_title_with_no_match_lists_what_is_available(self):
		with self.assertRaises(errors.DomainUnavailableError) as caught:
			windows.match_window("Photoshop", exact=False, windows=WINDOWS)
		self.assertEqual(caught.exception.protocol_code, 4205)
		self.assertIn("available", caught.exception.details)
		self.assertIn("Notepad", " ".join(caught.exception.details["available"]))

	def test_an_empty_title_matches_nothing(self):
		with self.assertRaises(errors.DomainUnavailableError):
			windows.match_window("   ", exact=False, windows=WINDOWS)


class ActivateArgumentTests(unittest.TestCase):
	def test_activation_needs_a_title_or_a_handle(self):
		with self.assertRaises(ValueError):
			windows.activate()

	def test_a_missing_handle_is_a_domain_error(self):
		with self.assertRaises(errors.DomainUnavailableError) as caught:
			windows.activate(handle=0x7FFFFFF0)
		self.assertEqual(caught.exception.protocol_code, 4205)


class FocusWindowTests(unittest.TestCase):
	"""Windows can move the foreground without moving the keyboard focus.

	``activate`` proves the first, because that is what it can change. This is
	the second answer, so a caller is never told the screen reader is somewhere
	it is not.
	"""

	def install(self, focus):
		previous = sys.modules.get("api")
		sys.modules["api"] = types.SimpleNamespace(getFocusObject=focus)
		self.addCleanup(self.restore, previous)

	def restore(self, previous):
		if previous is None:
			sys.modules.pop("api", None)
		else:
			sys.modules["api"] = previous

	def test_the_focused_control_reports_the_window_that_owns_it(self):
		window = types.SimpleNamespace(role="WINDOW", windowHandle=4242)
		control = types.SimpleNamespace(role="BUTTON", parent=window)
		self.install(lambda: control)

		self.assertEqual(windows.focus_window()[0], 4242)

	def test_a_child_window_role_does_not_mask_the_native_root(self):
		form = types.SimpleNamespace(role="DIALOG", windowHandle=4242)
		frame = types.SimpleNamespace(role="WINDOW", windowHandle=1234, parent=form)
		edit = types.SimpleNamespace(role="EDITABLETEXT", windowHandle=1234, parent=frame)
		self.install(lambda: edit)
		with patch.object(windows._user32, "GetAncestor", return_value=4242):
			self.assertEqual(windows.focus_window()[0], 4242)

	def test_a_dialog_reports_its_own_window(self):
		window = types.SimpleNamespace(role="WINDOW", parent=None, windowHandle=99)
		dialog = types.SimpleNamespace(role="DIALOG", windowHandle=77, parent=window)
		control = types.SimpleNamespace(role="BUTTON", parent=dialog)
		self.install(lambda: control)

		self.assertEqual(windows.focus_window()[0], 77)

	def test_an_object_model_that_fails_is_no_answer_rather_than_an_error(self):
		def explode():
			raise RuntimeError("no desktop")

		self.install(explode)
		self.assertEqual(windows.focus_window(), (0, ""))

	def test_a_focus_with_no_window_ancestor_is_no_answer(self):
		self.install(lambda: types.SimpleNamespace(role="DOCUMENT", parent=None))
		self.assertEqual(windows.focus_window(), (0, ""))

	def test_a_dead_object_stops_the_walk(self):
		class Exploding:
			role = "DOCUMENT"

			@property
			def parent(self):
				raise RuntimeError("gone")

		self.install(lambda: Exploding())
		self.assertEqual(windows.focus_window(), (0, ""))


class ObservationWindowTests(unittest.TestCase):
	def setUp(self):
		spec = importlib.util.spec_from_file_location(
			f"{_BRIDGE_KEY}.observation", os.path.join(_ADDON_DIR, "nvda_bridge", "observation.py")
		)
		self.observation = importlib.util.module_from_spec(spec)
		with patch.dict(sys.modules, {"api": types.ModuleType("api"), "logHandler": types.ModuleType("logHandler")}):
			spec.loader.exec_module(self.observation)

	def test_top_window_is_stable_across_controls_with_child_handles(self):
		form = types.SimpleNamespace(role="DIALOG", windowHandle=4242, name="Review form")
		with patch.object(windows._user32, "GetAncestor", return_value=4242):
			for child_handle in (1234, 5678):
				child = types.SimpleNamespace(role="EDITABLETEXT", windowHandle=child_handle)
				wrapper = types.SimpleNamespace(role="WINDOW", windowHandle=child_handle, name="Child")
				window, target = self.observation._window(child, [wrapper, form])
				self.assertIs(target, form)
				self.assertEqual(window["handle"], 4242)
				self.assertEqual(window["title"], "Review form")

	def test_dialog_takes_precedence_over_the_focused_buttons_window_wrapper(self):
		ok = types.SimpleNamespace(role="BUTTON", name="OK", windowHandle=1234)
		cancel = types.SimpleNamespace(role="BUTTON", name="Cancel", windowHandle=1235)
		wrapper = types.SimpleNamespace(role="WINDOW", name="OK", windowHandle=1234)
		dialog = types.SimpleNamespace(role="DIALOG", name="Confirmation", windowHandle=4242, children=[ok, cancel])
		frame = types.SimpleNamespace(role="WINDOW", name="Confirmation", windowHandle=4242)
		found = self.observation._find_dialog(ok, [wrapper, dialog, frame])
		self.assertIs(found, dialog)
		self.assertEqual(self.observation.dialog_children(found), [ok, cancel])

	def test_child_window_and_same_handle_wrappers_are_not_dialogs(self):
		focus = types.SimpleNamespace(role="BUTTON", name="Open", windowHandle=1234)
		child = types.SimpleNamespace(role="WINDOW", name="Open", windowHandle=1234)
		wrapper = types.SimpleNamespace(role="WINDOW", name="Pane", windowHandle=4242)
		frame = types.SimpleNamespace(role="WINDOW", name="Application", windowHandle=4242)
		with patch.object(windows._user32, "GetAncestor", return_value=4242):
			self.assertIsNone(self.observation._find_dialog(focus, [child, wrapper, frame]))

	def test_native_owned_popup_is_detected_without_an_explicit_dialog_role(self):
		focus = types.SimpleNamespace(role="EDITABLETEXT", windowHandle=1234)
		popup = types.SimpleNamespace(role="WINDOW", name="Pop-up", windowHandle=4242)
		frame = types.SimpleNamespace(role="WINDOW", name="Application", windowHandle=5050)
		with (
			patch.object(windows._user32, "GetAncestor", return_value=4242),
			patch.object(windows._user32, "GetWindow", return_value=5050) as get_window,
		):
			self.assertIs(self.observation._find_dialog(focus, [popup, frame]), popup)
		get_window.assert_called_once_with(4242, 4)


if __name__ == "__main__":
	unittest.main()
