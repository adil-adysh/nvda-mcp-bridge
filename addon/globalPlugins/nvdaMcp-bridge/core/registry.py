"""Versioned, immutable tool registry.

Central registry for tools.
- Register by (name, version) tuple
- Prevent duplicate registrations
- Freeze schemas after registration
- Immutable after build
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from types import MappingProxyType

from .base import Tool, ToolMetadata
from .error import RegistryError, ErrorCode


class ToolRegistryBuilder:
	"""Builder for tool registry - allows mutations during build phase."""

	def __init__(self) -> None:
		"""Initialize builder."""
		self._tools: Dict[Tuple[str, str], Tool] = {}

	def register(self, tool: Tool) -> ToolRegistryBuilder:
		"""Register a tool.

		Args:
			tool: Tool to register

		Returns:
			Self for fluent interface

		Raises:
			RegistryError: If tool contract is incomplete
			RegistryError: If tool is already registered
		"""
		metadata = tool.get_metadata()

		# Validate tool contract
		try:
			input_schema = tool.get_input_schema()
			output_schema = tool.get_output_schema()
		except Exception as e:
			raise RegistryError(
				tool_name=metadata.name,
				version=metadata.version,
				message=f"Tool contract incomplete: {e}",
				error_code=ErrorCode.INCOMPLETE_TOOL_CONTRACT,
			) from e

		# Check for duplicates
		key = (metadata.name, metadata.version)
		if key in self._tools:
			raise RegistryError(
				tool_name=metadata.name,
				version=metadata.version,
				message="Tool already registered",
				error_code=ErrorCode.TOOL_REGISTRATION_CONFLICT,
			)

		# Freeze schemas to prevent mutations
		input_schema.freeze()
		output_schema.freeze()

		self._tools[key] = tool
		return self

	def register_many(self, tools: list[Tool]) -> ToolRegistryBuilder:
		"""Register multiple tools.

		Args:
			tools: Tools to register

		Returns:
			Self for fluent interface
		"""
		for tool in tools:
			self.register(tool)
		return self

	def build(self) -> ToolRegistry:
		"""Build immutable registry.

		Returns:
			Frozen registry
		"""
		return ToolRegistry(dict(self._tools))


class ToolRegistry:
	"""Immutable, versioned tool registry.

	Once built, cannot be modified. All lookups are O(1).
	Supports multiple versions of the same tool simultaneously.
	"""

	def __init__(self, tools: Dict[Tuple[str, str], Tool]) -> None:
		"""Initialize registry (internal use only - use ToolRegistryBuilder).

		Args:
			tools: Dict of (name, version) -> Tool
		"""
		self._tools = MappingProxyType(tools)
		self._by_name: Dict[str, list[Tool]] = {}

		# Build reverse index by name
		for (name, version), tool in tools.items():
			if name not in self._by_name:
				self._by_name[name] = []
			self._by_name[name].append(tool)

		# Sort versions by descending semver for "latest" resolution
		for tool_list in self._by_name.values():
			tool_list.sort(
				key=lambda t: tuple(map(int, t.get_metadata().version.split("."))),
				reverse=True,
			)

	def get(self, name: str, version: Optional[str] = None) -> Tool:
		"""Get tool by name and optional version.

		Args:
			name: Tool name
			version: Specific version (optional)

		Returns:
			Tool instance

		Raises:
			RegistryError: If tool not found
		"""
		if version is None:
			# Return latest version
			if name not in self._by_name:
				raise RegistryError(
					tool_name=name,
					version=None,
					message=f"Tool not found: {name}",
					error_code=ErrorCode.TOOL_NOT_FOUND,
				)
			return self._by_name[name][0]  # Already sorted by version
		else:
			# Return specific version
			key = (name, version)
			if key not in self._tools:
				raise RegistryError(
					tool_name=name,
					version=version,
					message=f"Tool version not found: {name}:{version}",
					error_code=ErrorCode.VERSION_NOT_FOUND,
				)
			return self._tools[key]

	def get_input_schema(self, name: str, version: Optional[str] = None) -> Any:
		"""Get input schema for a tool (frozen).

		Args:
			name: Tool name
			version: Specific version (optional)

		Returns:
			InputSchema instance (frozen, immutable)

		Raises:
			RegistryError: If tool not found
		"""
		tool = self.get(name, version)
		schema = tool.get_input_schema()
		if not schema.is_frozen():
			raise RuntimeError(f"Schema for {name} is not frozen")
		return schema

	def get_output_schema(self, name: str, version: Optional[str] = None) -> Any:
		"""Get output schema for a tool (frozen).

		Args:
			name: Tool name
			version: Specific version (optional)

		Returns:
			OutputSchema instance (frozen, immutable)

		Raises:
			RegistryError: If tool not found
		"""
		tool = self.get(name, version)
		schema = tool.get_output_schema()
		if not schema.is_frozen():
			raise RuntimeError(f"Schema for {name} is not frozen")
		return schema

	def list_tools(self) -> list[ToolMetadata]:
		"""List all registered tools.

		Returns:
			List of tool metadata
		"""
		seen = set()
		result = []
		for tool in self._tools.values():
			key = (tool.get_metadata().name, tool.get_metadata().version)
			if key not in seen:
				result.append(tool.get_metadata())
				seen.add(key)
		return sorted(result, key=lambda m: (m.name, m.version))

	def list_versions(self, name: str) -> list[str]:
		"""List all versions of a tool.

		Args:
			name: Tool name

		Returns:
			List of version strings (sorted descending)
		"""
		if name not in self._by_name:
			raise RegistryError(
				tool_name=name,
				version=None,
				message=f"Tool not found: {name}",
				error_code=ErrorCode.TOOL_NOT_FOUND,
			)
		return [t.get_metadata().version for t in self._by_name[name]]

	def count(self) -> int:
		"""Get total number of tool versions registered."""
		return len(self._tools)

	def count_unique_tools(self) -> int:
		"""Get total number of unique tools (ignoring versions)."""
		return len(self._by_name)

	def __len__(self) -> int:
		"""Get total number of tool versions registered (for len() support)."""
		return len(self._tools)

	def __repr__(self) -> str:
		return f"<ToolRegistry tools={self.count_unique_tools()} " f"versions={self.count()}>"

	def __contains__(self, name: str) -> bool:
		"""Check if tool exists."""
		return name in self._by_name
