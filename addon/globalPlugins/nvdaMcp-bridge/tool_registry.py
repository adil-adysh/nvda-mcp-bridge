"""
Tool Registry for NVDA Bridge Actions

This module provides a clean, extensible pattern for registering and executing
bridge actions. Each action is self-contained with its own validation and execution logic.

Architecture:
- Tool: Self-contained action with name, handler, and optional schema
- ToolRegistry: Central registry for all available tools
- Execution: Thread-safe dispatch through main thread queue

Adding a new tool:
1. Create a tool handler function in nvda_bridge.py (or new module)
2. Register it using @register_tool decorator or ToolRegistry.register()
3. That's it - no changes to routing logic needed
"""

from typing import Dict, Any, Callable, Optional, List
from dataclasses import dataclass
import logHandler


@dataclass
class Tool:
	"""
	Represents a single bridge action/tool.

	Attributes:
		name: Unique identifier for the tool
		handler: Function that implements the tool logic
		description: Human-readable description
		required_params: List of required parameter names
		optional_params: List of optional parameter names
	"""

	name: str
	handler: Callable
	description: str = ""
	required_params: List[str] = None
	optional_params: List[str] = None

	def __post_init__(self):
		"""Initialize default values for lists"""
		if self.required_params is None:
			self.required_params = []
		if self.optional_params is None:
			self.optional_params = []

	def validate_params(self, params: Dict[str, Any]) -> None:
		"""
		Validate parameters for this tool.

		Args:
			params: Parameters to validate

		Raises:
			ValueError: If validation fails
		"""
		if params is None:
			params = {}

		# Check required parameters
		for required in self.required_params:
			if required not in params:
				raise ValueError(f"Missing required parameter: {required}")

		# Check for unknown parameters
		all_params = set(self.required_params + self.optional_params)
		provided_params = set(params.keys())
		unknown = provided_params - all_params

		if unknown and all_params:  # Only warn if we have a schema
			logHandler.log.warning(f"Tool {self.name} received unknown parameters: {unknown}")

	def execute(self, params: Dict[str, Any]) -> Any:
		"""
		Execute the tool with given parameters.

		This method validates parameters and calls the handler.
		Exceptions are propagated to the caller for proper error handling.

		Args:
			params: Parameters to pass to the handler

		Returns:
			Pure result from the handler (not wrapped in envelope)

		Raises:
			ValueError: If parameter validation fails
			Exception: Any exception raised by the handler
		"""
		# Validate parameters
		self.validate_params(params)

		# Execute handler - let exceptions propagate
		if params:
			return self.handler(**params)
		else:
			return self.handler()


class ToolRegistry:
	"""
	Central registry for all available bridge tools.

	This provides a clean way to register, discover, and execute tools
	without modifying core routing logic.
	"""

	def __init__(self):
		"""Initialize empty registry"""
		self._tools: Dict[str, Tool] = {}
		self._frozen: bool = False

	def freeze(self) -> None:
		"""
		Freeze the registry to prevent further modifications.
		This improves safety by preventing accidental runtime tool registration.

		Once frozen, attempts to register new tools will raise RuntimeError.
		"""
		self._frozen = True
		logHandler.log.info(f"Tool registry frozen with {len(self._tools)} tools")

	def is_frozen(self) -> bool:
		"""
		Check if registry is frozen.

		Returns:
			True if registry is frozen
		"""
		return self._frozen

	def register(
		self,
		name: str,
		handler: Callable,
		description: str = "",
		required_params: List[str] = None,
		optional_params: List[str] = None,
	) -> Tool:
		"""
		Register a new tool in the registry.

		Args:
			name: Unique tool name
			handler: Function that implements the tool
			description: Human-readable description
			required_params: List of required parameter names
			optional_params: List of optional parameter names

		Returns:
			The registered Tool instance

		Raises:
			ValueError: If tool name already exists
			RuntimeError: If registry is frozen
		"""
		if self._frozen:
			raise RuntimeError("Cannot register tools in frozen registry")

		if name in self._tools:
			raise ValueError(f"Tool '{name}' is already registered")

		tool = Tool(
			name=name,
			handler=handler,
			description=description,
			required_params=required_params or [],
			optional_params=optional_params or [],
		)

		self._tools[name] = tool
		logHandler.log.debug(f"Registered tool: {name}")
		return tool

	def get(self, name: str) -> Optional[Tool]:
		"""
		Get a tool by name.

		Args:
			name: Tool name

		Returns:
			Tool instance or None if not found
		"""
		return self._tools.get(name)

	def has(self, name: str) -> bool:
		"""
		Check if a tool exists.

		Args:
			name: Tool name

		Returns:
			True if tool is registered
		"""
		return name in self._tools

	def list_tools(self) -> List[Dict[str, Any]]:
		"""
		Get list of all registered tools with metadata.

		Returns:
			List of tool information dictionaries
		"""
		return [
			{
				"name": tool.name,
				"description": tool.description,
				"required_params": tool.required_params,
				"optional_params": tool.optional_params,
			}
			for tool in self._tools.values()
		]

	def execute(self, name: str, params: Dict[str, Any]) -> Dict[str, Any]:
		"""
		Execute a tool by name.

		Args:
			name: Tool name
			params: Parameters to pass to the tool

		Returns:
			Result dictionary from the tool

		Raises:
			ValueError: If tool is not found
		"""
		if not isinstance(name, str):
			raise ValueError("Tool name must be a string")

		tool = self.get(name)
		if not tool:
			raise ValueError(f"Unknown tool: {name}")

		return tool.execute(params)

	def __len__(self) -> int:
		"""Get number of registered tools"""
		return len(self._tools)

	def __contains__(self, name: str) -> bool:
		"""Support 'in' operator"""
		return self.has(name)


def register_tool(
	name: str,
	description: str = "",
	required_params: List[str] = None,
	optional_params: List[str] = None,
):
	"""
	Decorator for registering a tool with the global registry.

	Example:
		@register_tool("my_action", description="Does something")
		def my_action() -> Dict[str, Any]:
			return {"result": "success"}

	Args:
		name: Unique tool name
		description: Human-readable description
		required_params: List of required parameter names
		optional_params: List of optional parameter names
	"""

	def decorator(func: Callable) -> Callable:
		# Note: Actual registration happens in create_default_registry()
		# This decorator just marks the function with metadata
		func._tool_name = name
		func._tool_description = description
		func._tool_required_params = required_params or []
		func._tool_optional_params = optional_params or []
		return func

	return decorator
