# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Guards the generated schema artifact and the derived output type registry.

``schema.json`` is consumed by the Rust companion to validate bridge responses.
It is generated from the tool registry, so a stale copy means the companion
validates against a contract the add-on no longer produces — which is exactly
how a working tool can start failing after an add-on upgrade.
"""

import importlib
import json
import os
import sys
import types
import unittest

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_ADDON_DIR = os.path.join(_REPO_ROOT, "addon", "globalPlugins", "nvdaMcp-bridge")
_SCHEMA_PATH = os.path.join(_REPO_ROOT, "schema.json")

if _ADDON_DIR not in sys.path:
	sys.path.insert(0, _ADDON_DIR)

_PACKAGE_NAME = "nvdaMcpBridge"
if _PACKAGE_NAME not in sys.modules:
	_package = types.ModuleType(_PACKAGE_NAME)
	_package.__path__ = [_ADDON_DIR]
	sys.modules[_PACKAGE_NAME] = _package

_registry_module = importlib.import_module(f"{_PACKAGE_NAME}.validation.output_type_registry")
_tools_module = importlib.import_module(f"{_PACKAGE_NAME}.tools.registry")


class GeneratedSchemaTests(unittest.TestCase):
	def setUp(self):
		with open(_SCHEMA_PATH, encoding="utf-8") as schema_file:
			self.committed = json.load(schema_file)

	def test_committed_schema_matches_generated_output(self):
		"""schema.json must be regenerated whenever a tool model changes."""
		types_registry = _registry_module.build_output_type_registry()
		tools = _tools_module.NVDA_TOOLS_REGISTRY

		expected_tools = []
		for metadata in tools.list_tools():
			tool = tools.get(metadata.name, metadata.version)
			expected_tools.append(
				{
					"name": metadata.name,
					"version": metadata.version,
					"description": metadata.description,
					"category": metadata.category,
					"inputSchema": tool.get_input_schema().to_json_schema(),
					"outputType": tool.get_output_schema().model.__name__,
				}
			)

		committed_tools = {
			(entry["name"], entry["version"]): entry for entry in self.committed["tools"]
		}
		for expected in expected_tools:
			key = (expected["name"], expected["version"])
			self.assertIn(key, committed_tools, f"{key} missing from schema.json; regenerate it")
			committed = committed_tools[key]
			self.assertEqual(committed["outputType"], expected["outputType"])
			self.assertEqual(committed["inputSchema"], expected["inputSchema"])

		self.assertEqual(self.committed["types"], types_registry)

	def test_every_tool_output_type_is_published(self):
		types_registry = _registry_module.build_output_type_registry()
		tools = _tools_module.NVDA_TOOLS_REGISTRY

		for metadata in tools.list_tools():
			tool = tools.get(metadata.name, metadata.version)
			model_name = tool.get_output_schema().model.__name__
			self.assertIn(
				model_name,
				types_registry,
				f"tool '{metadata.name}' declares outputType '{model_name}' with no published schema",
			)

	def test_published_types_are_derived_from_models(self):
		"""No hand-written schema can drift from the dataclass it describes."""
		types_registry = _registry_module.build_output_type_registry()
		tools = _tools_module.NVDA_TOOLS_REGISTRY

		for metadata in tools.list_tools():
			tool = tools.get(metadata.name, metadata.version)
			model = tool.get_output_schema().model
			published = types_registry[model.__name__]["properties"]["data"]
			self.assertEqual(
				published,
				tool.get_output_schema().to_json_schema(),
				f"published schema for {model.__name__} is not the model's own schema",
			)


if __name__ == "__main__":
	_ = unittest.main()
