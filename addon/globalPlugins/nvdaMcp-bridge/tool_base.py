"""
Tool base abstractions for NVDA MCP Bridge.

Tools are self-contained units of NVDA behavior with declarative metadata.
They are protocol-agnostic and return pure Python values or raise exceptions.
"""

from dataclasses import dataclass, field
from abc import ABC, abstractmethod
from typing import Any, cast


@dataclass(frozen=True)
class ToolMetadata:
	"""Describes a discoverable bridge tool."""

	name: str
	description: str
	required_params: list[str] = field(default_factory=lambda: [])
	optional_params: list[str] = field(default_factory=lambda: [])
	version: str = "1.0.0"
	category: str = "general"

	def __post_init__(self) -> None:
		"""Validate and normalize metadata fields."""
		raw_name: Any = self.name
		raw_description: Any = self.description
		raw_version: Any = self.version
		raw_category: Any = self.category

		name = raw_name.strip() if isinstance(raw_name, str) else ""
		description = raw_description.strip() if isinstance(raw_description, str) else ""
		version = raw_version.strip() if isinstance(raw_version, str) else ""
		category = raw_category.strip() if isinstance(raw_category, str) else ""

		if not name:
			raise ValueError("Tool name cannot be empty")

		if not description:
			raise ValueError(f"Tool '{name}' description cannot be empty")

		if not version:
			raise ValueError(f"Tool '{name}' version cannot be empty")

		if not category:
			raise ValueError(f"Tool '{name}' category cannot be empty")

		required = self._validate_param_names(name, self.required_params, "required")
		optional = self._validate_param_names(name, self.optional_params, "optional")

		overlap = set(required) & set(optional)
		if overlap:
			raise ValueError(
				f"Tool '{name}' has parameters defined as both required and optional: "
				+ ", ".join(sorted(overlap))
			)

		object.__setattr__(self, "name", name)
		object.__setattr__(self, "description", description)
		object.__setattr__(self, "version", version)
		object.__setattr__(self, "category", category)
		object.__setattr__(self, "required_params", required)
		object.__setattr__(self, "optional_params", optional)

	@staticmethod
	def _validate_param_names(tool_name: str, values: Any, label: str) -> list[str]:
		if not isinstance(values, list):
			raise ValueError(f"Tool '{tool_name}' {label}_params must be a list")

		raw_values = cast(list[Any], values)

		normalized: list[str] = []
		for param in raw_values:
			if not isinstance(param, str):
				raise ValueError(f"Tool '{tool_name}' {label} parameter names must be strings")

			name = param.strip()
			if not name:
				raise ValueError(f"Tool '{tool_name}' has empty {label} parameter name")

			normalized.append(name)

		if len(normalized) != len(set(normalized)):
			raise ValueError(f"Tool '{tool_name}' has duplicate {label} parameter names")

		return normalized


class ToolBase(ABC):
	"""Abstract base class for all discoverable NVDA bridge tools."""

	# Explicit marker used by discovery to skip template classes
	_is_template: bool = False

	@abstractmethod
	def metadata(self) -> ToolMetadata:
		"""Return immutable tool metadata."""
		raise NotImplementedError

	@abstractmethod
	def execute(self, **params: Any) -> Any:
		"""Execute the tool and return a pure result or raise an exception."""
		raise NotImplementedError

	def validate_params(self, params: Any) -> None:
		"""Validate required/optional parameters for this tool."""
		if not isinstance(params, dict):
			raise ValueError("Parameters must be a dictionary")

		provided = cast(dict[str, Any], params)

		metadata = self.metadata()

		for required in metadata.required_params:
			if required not in provided:
				raise ValueError(f"Missing required parameter: {required}")

		all_params = set(metadata.required_params + metadata.optional_params)
		if all_params:
			unknown = set(provided.keys()) - all_params
			if unknown:
				raise ValueError(
					f"Unknown parameter(s) for tool '{metadata.name}': {', '.join(sorted(unknown))}"
				)
