from __future__ import annotations

from typing import Any, Literal


def get_current_focus() -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.get_current_focus()


def move_focus(direction: Literal["next", "previous"]) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.move_focus(direction=direction)


def get_navigator_object() -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.get_navigator_object()


def get_desktop_object() -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.get_desktop_object()


def read_current_line() -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.read_current_line()


def speak_text(text: str) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.speak_text(text=text)


def show_message(message: str) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.show_message(message=message)


def send_keyboard(gesture: str, repeat: int = 1, delay_ms: int = 0) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.send_keyboard(gesture=gesture, repeat=repeat, delay_ms=delay_ms)


def get_window_title() -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.get_window_title()


def inspect_object(
	target: Literal["focus", "navigator", "desktop"] = "focus",
	include_relations: bool = True,
) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.inspect_object(target=target, include_relations=include_relations)


def walk_object_tree(
	target: Literal["focus", "navigator", "desktop"] = "focus",
	max_depth: int = 2,
	max_children: int = 20,
) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.walk_object_tree(target=target, max_depth=max_depth, max_children=max_children)


def get_focus_lineage(max_ancestors: int = 15) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.get_focus_lineage(max_ancestors=max_ancestors)


def inspect_app_module(source: Literal["focus", "navigator", "desktop"] = "focus") -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.inspect_app_module(source=source)


def list_loaded_addons(scope: Literal["running", "available", "all"] = "running") -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.list_loaded_addons(scope=scope)


def health_snapshot(
	include_logs: bool = True,
	log_lines: int = 30,
	include_tree: bool = False,
	tree_depth: int = 1,
	tree_max_children: int = 10,
	addon_scope: Literal["running", "available", "all"] = "running",
) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.health_snapshot(
		include_logs=include_logs,
		log_lines=log_lines,
		include_tree=include_tree,
		tree_depth=tree_depth,
		tree_max_children=tree_max_children,
		addon_scope=addon_scope,
	)


def restart_nvda(disable_addons: bool = False, debug_logging: bool = False) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.restart_nvda(disable_addons=disable_addons, debug_logging=debug_logging)


def reload_plugins() -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.reload_plugins()


def get_logs(
	max_lines: int = 100,
	filter_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None,
	use_tail: bool = True,
) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.get_logs(max_lines=max_lines, filter_level=filter_level, use_tail=use_tail)


def execute_python(
	code: str,
	snapshot_vars: bool = False,
	capture_vars: list[str] | None = None,
) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.execute_python(code=code, snapshot_vars=snapshot_vars, capture_vars=capture_vars)
