"""Tool registry with ToolBase auto-discovery support."""

import importlib
import inspect
import pkgutil
from types import ModuleType
from typing import Dict, Any, Optional, List

import logHandler

from .tool_base import ToolBase


class ToolRegistry:
	"""
	Central registry for all available bridge tools.

	This provides a clean way to register, discover, and execute tools
	without modifying core routing logic.
	"""

	def __init__(self):
		"""Initialize empty registry"""
		self._tools: Dict[str, ToolBase] = {}
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

	def register_tool(self, tool: ToolBase) -> ToolBase:
		"""
		Register a new ToolBase implementation in the registry.

		Args:
			tool: ToolBase instance to register

		Returns:
			The registered ToolBase instance

		Raises:
			ValueError: If tool name already exists or metadata is invalid
			RuntimeError: If registry is frozen
		"""
		if self._frozen:
			raise RuntimeError("Cannot register tools in frozen registry")

		if not isinstance(tool, ToolBase):
			raise ValueError("Only ToolBase instances can be registered")

		metadata = tool.metadata()
		if not metadata.name:
			raise ValueError("Tool name cannot be empty")

		if metadata.name in self._tools:
			raise ValueError(f"Tool '{metadata.name}' is already registered")

		self._tools[metadata.name] = tool
		logHandler.log.debug(
			f"tool.registered name={metadata.name} "
			f"category={metadata.category} version={metadata.version}"
		)
		return tool

	def auto_discover(self, tools_package: ModuleType) -> int:
		"""
		Auto-discover ToolBase classes in all modules of a package.

		Any non-abstract subclass of ToolBase found in a `.py` module under
		`tools_package` is instantiated with no arguments and registered.

		Args:
			tools_package: Imported package module containing tool modules

		Returns:
			Number of tools registered
		"""
		if self._frozen:
			raise RuntimeError("Cannot auto-discover tools in frozen registry")

		if not hasattr(tools_package, "__path__"):
			raise ValueError("tools_package must be a package module")

		registered = 0

		for module_info in pkgutil.iter_modules(tools_package.__path__):
			if module_info.ispkg:
				continue

			module_name = f"{tools_package.__name__}.{module_info.name}"
			module = importlib.import_module(module_name)

			for _, klass in inspect.getmembers(module, inspect.isclass):
				if klass.__module__ != module.__name__:
					continue
				if klass is ToolBase:
					continue
				if not issubclass(klass, ToolBase):
					continue
				if inspect.isabstract(klass):
					logHandler.log.debug(f"tool.discovery.skip class={klass.__name__} reason=abstract_class")
					continue
				# Template marker is class-local on purpose. This allows concrete
				# subclasses of template base classes to be discovered by default.
				if bool(klass.__dict__.get("_is_template", False)):
					logHandler.log.debug(f"tool.discovery.skip class={klass.__name__} reason=template_class")
					continue

				try:
					tool = klass()
				except Exception as error:
					raise RuntimeError(
						f"Failed to instantiate tool class {klass.__name__} in {module_name}: {error}"
					) from error

				try:
					metadata = tool.metadata()
				except Exception as error:
					raise RuntimeError(
						f"Failed to load metadata for tool class {klass.__name__} in {module_name}: {error}"
					) from error

				if not metadata.name:
					logHandler.log.debug(f"tool.discovery.skip class={klass.__name__} reason=empty_name")
					continue
				self.register_tool(tool)
				registered += 1

		logHandler.log.info(f"tool.discovery.completed package={tools_package.__name__} tools={registered}")
		return registered

	def get(self, name: str) -> Optional[ToolBase]:
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
		# Call metadata() exactly once per tool: for NVDAFunctionTool each call
		# allocates a new frozen dataclass and runs __post_init__ validation.
		result: List[Dict[str, Any]] = []
		for tool in self._tools.values():
			m = tool.metadata()
			result.append(
				{
					"name": m.name,
					"description": m.description,
					"required_params": m.required_params,
					"optional_params": m.optional_params,
					"version": m.version,
					"category": m.category,
				}
			)
		return result

	def execute(self, name: str, params: Any) -> Any:
		"""
		Execute a tool by name.

		Args:
			name: Tool name
			params: Parameters to pass to the tool

		Returns:
			Pure result from the tool

		Raises:
			ValueError: If tool is not found
		"""
		if not isinstance(name, str):
			raise ValueError("Tool name must be a string")

		tool = self.get(name)
		if not tool:
			raise ValueError(f"Unknown tool: {name}")

		if params is not None and not isinstance(params, dict):
			raise ValueError("Parameters must be a dictionary")

		normalized_params = params or {}
		tool.validate_params(normalized_params)
		return tool.execute(**normalized_params)

	def __len__(self) -> int:
		"""Get number of registered tools"""
		return len(self._tools)

	def __contains__(self, name: str) -> bool:
		"""Support 'in' operator"""
		return self.has(name)
