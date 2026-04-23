# pyright: reportMissingImports=false, reportUnusedCallResult=false

import unittest

import sys
import os

# Add addon directory to path for imports
_ADDON_DIR = os.path.join(
	os.path.dirname(os.path.abspath(__file__)), "..", "addon", "globalPlugins", "nvdaMcp-bridge"
)
sys.path.insert(0, _ADDON_DIR)

from validation.output_type_registry import (
	build_output_type_registry,
	validate_output_type_registry,
)


class OutputTypeRegistryTests(unittest.TestCase):
	def test_registry_is_non_empty_and_valid(self):
		registry = build_output_type_registry()

		self.assertTrue(registry)
		validate_output_type_registry(registry)

	def test_registry_rejects_non_pascal_case_name(self):
		with self.assertRaises(ValueError):
			validate_output_type_registry(
				{
					"not_pascal": {
						"$schema": "http://json-schema.org/draft-07/schema#",
						"type": "object",
						"properties": {},
					}
				}
			)

	def test_log_entries_schema_matches_bridge_shape(self):
		registry = build_output_type_registry()
		log_entries_data = registry["LogEntries"]["properties"]["data"]

		self.assertIn("log_file", log_entries_data["properties"])
		self.assertIn("lines", log_entries_data["properties"])
		self.assertEqual(log_entries_data["properties"]["lines"]["items"]["type"], "string")

	def test_text_line_info_accepts_line_number_and_note(self):
		registry = build_output_type_registry()
		text_line_data = registry["TextLineInfo"]["properties"]["data"]

		self.assertIn("lineNumber", text_line_data["properties"])
		self.assertIn("note", text_line_data["properties"])

	def test_health_snapshot_options_allow_expected_fields(self):
		registry = build_output_type_registry()
		health_data = registry["HealthSnapshot"]["properties"]["data"]
		options = health_data["properties"]["options"]

		self.assertIn("include_logs", options["properties"])
		self.assertIn("tree_max_children", options["properties"])

	def test_keyboard_action_result_schema_exists(self):
		registry = build_output_type_registry()
		keyboard_data = registry["KeyboardActionResult"]["properties"]["data"]

		self.assertEqual(keyboard_data["properties"]["gesture"]["type"], "string")
		self.assertEqual(keyboard_data["properties"]["repeat"]["type"], "integer")
		self.assertEqual(keyboard_data["properties"]["delay_ms"]["type"], "integer")
		self.assertEqual(keyboard_data["properties"]["sent"]["type"], "boolean")


if __name__ == "__main__":
	_ = unittest.main()
