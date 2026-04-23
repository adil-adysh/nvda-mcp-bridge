"""Named output type registry for nvda-bridge-protocol v1."""

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
					"code": {"type": "string"},
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
	"""Build all named output types exposed by GET /tools."""
	return {
		"ToolResponse": _tool_response_schema(
			{"type": "object", "properties": {}, "additionalProperties": True}
		),
		"ObjectInfo": _tool_response_schema(
			{
				"type": "object",
				"properties": {
					"name": {"type": "string"},
					"role": {"type": ["integer", "null"]},
					"roleText": {"type": "string"},
					"value": {"type": "string"},
					"description": {"type": "string"},
					"states": {"type": "string"},
				},
				"required": ["name", "role", "roleText", "value", "description", "states"],
				"additionalProperties": False,
			}
		),
		"WindowInfo": _tool_response_schema(
			{
				"type": "object",
				"properties": {
					"title": {"type": "string"},
					"handle": {"type": "integer"},
					"className": {"type": "string"},
				},
				"required": ["title", "handle", "className"],
				"additionalProperties": False,
			}
		),
		"TextLineInfo": _tool_response_schema(
			{
				"type": "object",
				"properties": {
					"text": {"type": "string"},
					"lineNumber": {"type": ["integer", "null"]},
					"note": {"type": "string"},
				},
				"required": ["text"],
				"additionalProperties": True,
			}
		),
		"SpeechResult": _tool_response_schema(
			{
				"type": "object",
				"properties": {
					"spoken": {"type": "string"},
					"status": {"type": "string"},
				},
				"required": ["spoken"],
				"additionalProperties": False,
			}
		),
		"MessageResult": _tool_response_schema(
			{
				"type": "object",
				"properties": {
					"message": {"type": "string"},
					"status": {"type": "string"},
				},
				"required": ["message"],
				"additionalProperties": False,
			}
		),
		"KeyboardActionResult": _tool_response_schema(
			{
				"type": "object",
				"properties": {
					"gesture": {"type": "string"},
					"repeat": {"type": "integer"},
					"delay_ms": {"type": "integer"},
					"sent": {"type": "boolean"},
				},
				"required": ["gesture", "repeat", "delay_ms", "sent"],
				"additionalProperties": False,
			}
		),
		"MoveFocusResult": _tool_response_schema(
			{
				"type": "object",
				"properties": {
					"direction": {"type": "string", "enum": ["next", "previous"]},
					"pendingFocus": {"type": "boolean"},
					"newFocus": {
						"type": "object",
						"properties": {
							"name": {"type": "string"},
							"role": {"type": ["integer", "null"]},
							"roleText": {"type": "string"},
						},
						"required": ["name", "role", "roleText"],
						"additionalProperties": False,
					},
				},
				"required": ["direction", "pendingFocus", "newFocus"],
				"additionalProperties": False,
			}
		),
		"ObjectInspection": _tool_response_schema(
			{
				"type": "object",
				"properties": {
					"target": {"type": "string", "enum": ["focus", "navigator", "desktop"]},
					"object": {"type": "object", "additionalProperties": True},
				},
				"required": ["target", "object"],
				"additionalProperties": False,
			}
		),
		"ObjectTreeSnapshot": _tool_response_schema(
			{
				"type": "object",
				"properties": {
					"target": {"type": "string", "enum": ["focus", "navigator", "desktop"]},
					"max_depth": {"type": "integer"},
					"max_children": {"type": "integer"},
					"tree": {"type": "object", "additionalProperties": True},
				},
				"required": ["target", "max_depth", "max_children", "tree"],
				"additionalProperties": False,
			}
		),
		"FocusLineage": _tool_response_schema(
			{
				"type": "object",
				"properties": {
					"focus": {"type": "object", "additionalProperties": True},
					"ancestorCount": {"type": "integer"},
					"truncated": {"type": "boolean"},
					"ancestors": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
				},
				"required": ["focus", "ancestorCount", "truncated", "ancestors"],
				"additionalProperties": False,
			}
		),
		"AppModuleInfo": _tool_response_schema(
			{
				"type": "object",
				"properties": {
					"source": {"type": "string", "enum": ["focus", "navigator", "desktop"]},
					"object": {"type": "object", "additionalProperties": True},
					"appModule": {"type": "object", "additionalProperties": True},
				},
				"required": ["source", "object", "appModule"],
				"additionalProperties": False,
			}
		),
		"AddonMetadataList": _tool_response_schema(
			{
				"type": "object",
				"properties": {
					"scope": {"type": "string", "enum": ["running", "available", "all"]},
					"count": {"type": "integer"},
					"runningCount": {"type": "integer"},
					"availableCount": {"type": "integer"},
					"addons": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
				},
				"required": ["scope", "count", "runningCount", "availableCount", "addons"],
				"additionalProperties": False,
			}
		),
		"HealthSnapshot": _tool_response_schema(
			{
				"type": "object",
				"properties": {
					"timestamp": {"type": "integer"},
					"options": {
						"type": "object",
						"properties": {
							"include_logs": {"type": "boolean"},
							"log_lines": {"type": "integer"},
							"include_tree": {"type": "boolean"},
							"tree_depth": {"type": "integer"},
							"tree_max_children": {"type": "integer"},
							"addon_scope": {"type": "string"},
						},
						"additionalProperties": True,
					},
					"errors": {"type": "object", "additionalProperties": True},
					"ok": {"type": "boolean"},
					"errorCount": {"type": "integer"},
				},
				"required": ["timestamp", "options", "errors", "ok", "errorCount"],
				"additionalProperties": True,
			}
		),
		"NvdaRestartResult": _tool_response_schema(
			{
				"type": "object",
				"properties": {"status": {"type": "string"}},
				"required": ["status"],
				"additionalProperties": True,
			}
		),
		"LogEntries": _tool_response_schema(
			{
				"type": "object",
				"properties": {
					"log_file": {"type": "string"},
					"returned_lines": {"type": "integer"},
					"max_lines": {"type": "integer"},
					"filter_level": {"type": ["string", "null"]},
					"lines": {"type": "array", "items": {"type": "string"}},
					"file_size": {"type": "integer"},
				},
				"required": ["log_file", "returned_lines", "max_lines", "lines", "file_size"],
				"additionalProperties": True,
			}
		),
		"PythonExecutionResult": _tool_response_schema(
			{
				"anyOf": [
					{
						"type": "object",
						"properties": {
							"output": {"type": "string"},
							"result": {"type": ["string", "null"]},
							"vars": {"type": "object", "additionalProperties": True},
						},
						"required": ["output", "result"],
						"additionalProperties": False,
					},
					{
						"type": "object",
						"properties": {
							"stdout": {"type": "string"},
							"stderr": {"type": "string"},
							"result": {},
							"captured_variables": {"type": "object", "additionalProperties": True},
							"execution_error": {
								"type": ["object", "null"],
								"properties": {
									"type": {"type": "string"},
									"message": {"type": "string"},
									"traceback": {"type": "string"},
								},
								"required": ["type", "message"],
								"additionalProperties": False,
							},
						},
						"required": ["stdout", "stderr"],
						"additionalProperties": False,
					},
				],
			}
		),
	}


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
