"""Named output type registry for nvda-bridge-protocol v1.

Schemas are *derived* from each tool's output dataclass rather than transcribed
by hand. A hand-written copy is a second source of truth: it drifts from the
dataclass, and the drift is only discovered at runtime when a client rejects a
payload that the bridge considered valid (see the `completed`/`uncertain`
regression in KeyboardActionResult).

Deriving means one declaration — the dataclass in ``tools/models.py`` — drives
validation, ``GET /tools`` discovery, and the generated ``schema.json``.
"""

from __future__ import annotations

import re
from typing import Any

JSON_SCHEMA_V7_URI = "http://json-schema.org/draft-07/schema#"


_PASCAL_CASE_PATTERN = re.compile(r"^[A-Z][A-Za-z0-9]*$")


def _tool_response_schema(data_schema: dict[str, Any]) -> dict[str, Any]:
	return {
		"$schema": JSON_SCHEMA_V7_URI,
		"type": "object",
		"properties": {
			"success": {"type": "boolean"},
			"data": data_schema,
			"error": {
				"type": "object",
				"properties": {
					"code": {"type": "integer"},
					"message": {"type": "string"},
				},
				"required": ["code", "message"],
				"additionalProperties": False,
			},
		},
		"required": ["success"],
		"additionalProperties": False,
	}


def build_output_type_registry() -> dict[str, dict[str, Any]]:
	"""Build all named output types exposed by GET /tools.

	Each type name is the output dataclass's own name and its ``data`` schema is
	generated from that dataclass. The import of the tool registry is deferred to
	keep ``core`` free of a dependency on ``tools`` at module import time.
	"""
	from ..tools.registry import NVDA_TOOLS_REGISTRY

	types: dict[str, dict[str, Any]] = {}
	for metadata in NVDA_TOOLS_REGISTRY.list_tools():
		tool = NVDA_TOOLS_REGISTRY.get(metadata.name, metadata.version)
		output_schema = tool.get_output_schema()
		model = output_schema.model
		type_name = getattr(model, "__name__", "")
		schema = _tool_response_schema(output_schema.to_json_schema())

		# Several tools may legitimately share one output model (three focus
		# tools return ObjectInfo). Only a name mapping to two different shapes
		# is ambiguous, because discovery keys types by name.
		existing = types.get(type_name)
		if existing is not None and existing != schema:
			raise ValueError(
				f"Output type name '{type_name}' maps to two different schemas; "
				"output models must have unique names for discovery to be unambiguous",
			)
		types[type_name] = schema

	return types


def validate_output_type_registry(type_registry: dict[str, dict[str, Any]]) -> None:
	"""Fail-fast validation for the named output type registry."""
	if not type_registry:
		raise ValueError("types registry cannot be empty")

	for type_name, schema in type_registry.items():
		if not _PASCAL_CASE_PATTERN.match(type_name):
			raise ValueError(f"Invalid output type name: {type_name!r}. Must be PascalCase")
		if schema.get("$schema") != JSON_SCHEMA_V7_URI:
			raise ValueError(f"Output type '{type_name}' must declare draft-07 $schema")
		if schema.get("type") != "object":
			raise ValueError(f"Output type '{type_name}' root schema type must be 'object'")
		if not isinstance(schema.get("properties"), dict):
			raise ValueError(f"Output type '{type_name}' must define object properties")
