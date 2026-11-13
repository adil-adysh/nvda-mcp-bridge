"""Tests for compact catalog search and schema-on-demand."""

from __future__ import annotations

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

for name in ("logHandler",):
	if name not in sys.modules:
		sys.modules[name] = types.SimpleNamespace(
			log=types.SimpleNamespace(
				debug=lambda *a, **k: None,
				info=lambda *a, **k: None,
				warning=lambda *a, **k: None,
				error=lambda *a, **k: None,
			)
		)

_PACKAGE = "nvdaMcpBridge"
if _PACKAGE not in sys.modules:
	_pkg = types.ModuleType(_PACKAGE)
	_pkg.__path__ = [_ADDON_DIR]
	sys.modules[_PACKAGE] = _pkg

core = importlib.import_module(f"{_PACKAGE}.core")
registry_module = importlib.import_module(f"{_PACKAGE}.tools.registry")


class ToolDiscoveryTests(unittest.TestCase):
	def setUp(self) -> None:
		self.registry = registry_module.NVDA_TOOLS_REGISTRY
		self.executor = core.ToolExecutor()
		self.context = core.ExecutionContext(tool_registry=self.registry, developer_mode=False)

	def _run(self, name: str, arguments: dict, *, developer_mode: bool = False):
		context = core.ExecutionContext(
			tool_registry=self.registry,
			developer_mode=developer_mode,
		)
		result = self.executor.execute(self.registry.get(name), arguments, context)
		self.assertTrue(result.success, getattr(result.error, "details", result.error))
		return result.output

	def test_discovery_tools_are_in_the_catalog(self) -> None:
		names = {meta.name for meta in self.registry.list_tools()}
		self.assertIn("find_tools", names)
		self.assertIn("get_tool_schema", names)
		self.assertEqual(len(names), 34)

	def test_search_uses_word_starts_not_substrings(self) -> None:
		# "dialog" only contains "log" as a substring. A naive search returned
		# dismiss for a query about logs, which made the index feel arbitrary.
		result = self._run("find_tools", {"query": "log", "limit": 50})
		names = {tool.name for tool in result.tools}
		self.assertNotIn("dismiss", names)
		self.assertIn("search_logs", names)
		self.assertIn("get_logs", names)

	def test_search_reads_the_whole_description_not_just_the_summary(self) -> None:
		# "retention" appears near the end of get_action_trace's long description,
		# after the 220-character summary bound.
		result = self._run("find_tools", {"query": "retention", "limit": 50})
		self.assertIn("get_action_trace", {tool.name for tool in result.tools})

	def test_search_ranks_name_matches_and_honours_limit(self) -> None:
		result = self._run("find_tools", {"query": "search logs", "limit": 3})
		self.assertTrue(result.tools)
		self.assertEqual(result.tools[0].name, "search_logs")
		self.assertLessEqual(result.returned, 3)
		self.assertGreaterEqual(result.total_matches, result.returned)

	def test_category_and_read_only_filters(self) -> None:
		result = self._run(
			"find_tools",
			{"category": "diagnostics", "read_only": True, "limit": 50},
		)
		self.assertTrue(result.tools)
		for tool in result.tools:
			self.assertEqual(tool.category, "diagnostics")
			self.assertTrue(tool.read_only)
		self.assertIn("diagnostics", result.available_categories)

	def test_developer_tools_are_hidden_until_the_bridge_allows_them(self) -> None:
		hidden = self._run("find_tools", {"query": "python", "limit": 50})
		self.assertNotIn("execute_python", {tool.name for tool in hidden.tools})

		visible = self._run("find_tools", {"query": "python", "limit": 50}, developer_mode=True)
		self.assertIn("execute_python", {tool.name for tool in visible.tools})

	def test_empty_query_lists_by_group(self) -> None:
		result = self._run("find_tools", {"limit": 50})
		categories = [tool.category for tool in result.tools]
		self.assertEqual(categories, sorted(categories))

	def test_titles_show_the_group(self) -> None:
		result = self._run("find_tools", {"query": "health_snapshot", "limit": 1})
		self.assertTrue(result.tools[0].title.startswith("Diagnostics · "))
		self.assertEqual(result.tools[0].required_parameters, [])

	def test_schema_on_demand_returns_one_input_schema(self) -> None:
		brief = self._run("get_tool_schema", {"name": "health_snapshot"})
		self.assertEqual(brief.name, "health_snapshot")
		self.assertIn("properties", brief.input_schema)
		self.assertIsNone(brief.output_schema)

		full = self._run(
			"get_tool_schema",
			{"name": "health_snapshot", "include_output_schema": True},
		)
		self.assertIsNotNone(full.output_schema)
		self.assertIn("properties", full.output_schema)

	def test_unknown_schema_points_back_to_find_tools(self) -> None:
		result = self.executor.execute(
			self.registry.get("get_tool_schema"),
			{"name": "does_not_exist"},
			self.context,
		)
		self.assertFalse(result.success)
		self.assertIn("find_tools", result.error.message)


if __name__ == "__main__":
	_ = unittest.main()
