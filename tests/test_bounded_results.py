# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Bounded observation projections (API level 1)."""

import importlib
import json
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

handlers = importlib.import_module(f"{_PACKAGE_NAME}.tools.handlers")


class Context:
	def __init__(self, payload):
		self.payload = payload

	def observe(self, include, max_text):
		return json.loads(json.dumps(self.payload))


def _payload(count=40):
	return {
		"state_version": 9,
		"revision": 3,
		"refs": {"f": "focus", "c0": "child"},
		"focus": {"ref": "f", "role": "EDITABLETEXT", "name": "Search", "value": "query", "states": ["FOCUSED"]},
		"ancestors": [
			{"ref": f"a{index}", "role": "GROUPING", "name": f"ancestor {index}"} for index in range(count)
		],
		"children": [
			{"ref": f"c{index}", "role": "BUTTON", "name": f"button {index}", "value": "x" * 100}
			for index in range(count)
		],
		"dialog": None,
		"top_window": {"ref": "w", "title": "Notepad", "handle": 12, "class_name": "Notepad"},
		"selection": "selected text",
	}


class ObservationBoundTests(unittest.TestCase):
	def test_zero_means_no_bound(self):
		result = handlers.observe(["focus", "children"], 120, Context(_payload()), detail="summary", max_bytes=0)
		self.assertFalse(result.get("truncated", False))
		self.assertEqual(len(result["children"]), 40)

	def test_a_small_bound_drops_optional_sections_and_says_so(self):
		result = handlers.observe(["focus", "children"], 120, Context(_payload()), detail="summary", max_bytes=2500)
		self.assertTrue(result["truncated"])
		self.assertTrue(result["truncated_sections"])
		self.assertTrue(result["more_available"])
		self.assertIn("max_bytes", result["truncation_hint"])
		self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), 2500)
		# Identity survives: the caller can still act or re-observe precisely.
		self.assertEqual(result["focus"]["ref"], "f")
		self.assertEqual(result["refs"]["f"], "focus")

	def test_a_bound_that_already_fits_reports_no_truncation(self):
		result = handlers.observe(["focus"], 120, Context(_payload(count=0)), detail="summary", max_bytes=100000)
		self.assertFalse(result["truncated"])
		self.assertEqual(result["truncated_sections"], [])

	def test_summary_still_removes_states_before_bounding(self):
		result = handlers.observe(["focus"], 120, Context(_payload(count=0)), detail="summary", max_bytes=100000)
		self.assertNotIn("states", result["focus"])


if __name__ == "__main__":
	unittest.main()
