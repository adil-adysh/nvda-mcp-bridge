"""
Default Tool Definitions for NVDA Bridge

This module builds the default ToolRegistry using auto-discovery.
Adding a new tool is as simple as adding a new ToolBase subclass in tools/.
"""

from .tool_registry import ToolRegistry
from . import tools


def create_default_registry() -> ToolRegistry:
	"""
	Create and populate the default tool registry with auto-discovered tools.

	Returns:
		ToolRegistry instance with all default tools registered
	"""
	registry = ToolRegistry()
	registry.auto_discover(tools)

	return registry
