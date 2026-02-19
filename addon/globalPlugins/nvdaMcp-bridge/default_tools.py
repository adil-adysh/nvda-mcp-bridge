"""
Default Tool Definitions for NVDA Bridge

This module registers all built-in bridge tools with proper metadata.
Adding a new tool is as simple as importing the handler and calling registry.register().
"""

from .tool_registry import ToolRegistry
from . import nvda_bridge


def create_default_registry() -> ToolRegistry:
	"""
	Create and populate the default tool registry with all built-in tools.

	Returns:
		ToolRegistry instance with all default tools registered
	"""
	registry = ToolRegistry()

	# Focus and navigation tools
	registry.register(
		name="get_current_focus",
		handler=nvda_bridge.get_current_focus,
		description="Get information about the currently focused UI element",
		required_params=[],
		optional_params=[],
	)

	registry.register(
		name="move_focus",
		handler=nvda_bridge.move_focus,
		description="Move focus to next or previous object",
		required_params=["direction"],
		optional_params=[],
	)

	registry.register(
		name="get_navigator_object",
		handler=nvda_bridge.get_navigator_object,
		description="Get information about the current navigator object",
		required_params=[],
		optional_params=[],
	)

	registry.register(
		name="get_desktop_object",
		handler=nvda_bridge.get_desktop_object,
		description="Get information about the desktop object",
		required_params=[],
		optional_params=[],
	)

	# Text reading tools
	registry.register(
		name="read_current_line",
		handler=nvda_bridge.read_current_line,
		description="Read the current line from the focused object",
		required_params=[],
		optional_params=[],
	)

	# Speech tools
	registry.register(
		name="speak_text",
		handler=nvda_bridge.speak_text,
		description="Speak the given text using NVDA's speech system",
		required_params=["text"],
		optional_params=[],
	)

	# UI tools
	registry.register(
		name="show_message",
		handler=nvda_bridge.show_message,
		description="Show a UI message to the user",
		required_params=["message"],
		optional_params=[],
	)

	# Window tools
	registry.register(
		name="get_window_title",
		handler=nvda_bridge.get_window_title,
		description="Get the title and details of the current foreground window",
		required_params=[],
		optional_params=[],
	)

	return registry
