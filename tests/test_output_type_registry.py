# pyright: reportMissingImports=false, reportUnusedCallResult=false

import importlib
import unittest

import sys
import os
import types

# The output type registry derives its schemas from the tool registry, so these
# tests load the add-on as a package (as NVDA does) rather than importing the
# validation module standalone.
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

_registry_module = importlib.import_module(f"{_PACKAGE_NAME}.validation.output_type_registry")
build_output_type_registry = _registry_module.build_output_type_registry
validate_output_type_registry = _registry_module.validate_output_type_registry


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

	def test_read_result_carries_text_and_source(self):
		registry = build_output_type_registry()
		read_data = registry["ReadResult"]["properties"]["data"]

		self.assertIn("text", read_data["properties"])
		self.assertIn("source", read_data["properties"])

	def test_health_snapshot_is_lean_by_default(self):
		registry = build_output_type_registry()
		health_data = registry["HealthSnapshot"]["properties"]["data"]

		self.assertIn("error_count", health_data["properties"])
		self.assertIn("nvda", health_data["properties"])
		self.assertNotIn("options", health_data["properties"])

	def test_press_result_schema_exists(self):
		registry = build_output_type_registry()
		press_data = registry["PressResult"]["properties"]["data"]

		self.assertEqual(press_data["properties"]["requested"]["type"], "string")
		self.assertEqual(press_data["properties"]["normalized_keys"]["type"], "string")
		self.assertEqual(press_data["properties"]["repeat"]["type"], "integer")
		self.assertEqual(press_data["properties"]["delay_ms"]["type"], "integer")
		self.assertEqual(press_data["properties"]["sent"]["type"], "boolean")


if __name__ == "__main__":
	_ = unittest.main()
