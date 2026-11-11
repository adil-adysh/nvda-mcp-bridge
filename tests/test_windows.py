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


if __name__ == "__main__":
	unittest.main()
