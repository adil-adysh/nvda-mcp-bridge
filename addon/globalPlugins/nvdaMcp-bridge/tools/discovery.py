"""Compact, on-demand discovery over the bridge's own tool registry.

The full catalog is still what an MCP client asks for by default; this module
gives an agent a cheap way to *choose* from it without reading every schema.
``find_tools`` returns summaries, and ``get_tool_schema`` returns one schema at
a time.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from ..core import ExecutionContext
from .models import (
	FindToolsInput,
	FindToolsResult,
	GetToolSchemaInput,
	GetToolSchemaResult,
	ToolSummary,
)


_CATEGORY_LABELS = {
	"action": "Action",
	"developer": "Developer",
	"diagnostics": "Diagnostics",
	"discovery": "Discovery",
	"input": "Input",
	"observation": "Observation",
	"speech": "Speech",
	"system": "System",
	"ui": "UI",
}

_ACRONYMS = {"id", "mcp", "nvda", "ui", "uia"}


def human_title(name: str) -> str:
	"""Turn ``get_focus_lineage`` into ``Get Focus Lineage``."""

	parts = []
	for part in str(name or "").split("_"):
		if not part:
			continue
		if part.lower() in _ACRONYMS:
			parts.append(part.upper())
		else:
			parts.append(part[0].upper() + part[1:])
	return " ".join(parts)


def category_label(category: str) -> str:
	"""A short, readable group name for a bridge category."""

	value = str(category or "").strip()
	return _CATEGORY_LABELS.get(value.lower(), human_title(value))


def grouped_title(category: str, name: str) -> str:
	"""Prefix a tool title with its group so lists show the namespace.

	Tool names stay stable; only the display title changes. MCP clients that
	show titles (approval lists, tool pickers) get a visible grouping without a
	new required protocol field.
	"""

	label = category_label(category)
	title = human_title(name)
	return f"{label} · {title}" if label else title


def _bounded_description(value: str, limit: int = 220) -> str:
	text = " ".join(str(value or "").split())
	return text[:limit]


def _registry(context: ExecutionContext | None) -> Any:
	registry = getattr(context, "tool_registry", None)
	if registry is not None:
		return registry
	# Direct unit-test and diagnostic calls may not carry a bridge context.
	from .registry import NVDA_TOOLS_REGISTRY

	return NVDA_TOOLS_REGISTRY


def _developer_mode(context: ExecutionContext | None) -> bool:
	return bool(getattr(context, "developer_mode", False))


def _visible_metadata(
	registry: Any,
	*,
	developer_mode: bool,
	include_deprecated: bool = True,
) -> list[Any]:
	result = []
	for meta in registry.list_tools():
		if getattr(meta, "required_permission", "") == "developer" and not developer_mode:
			continue
		if getattr(meta, "deprecated", False) and not include_deprecated:
			continue
		result.append(meta)
	return result


def _required_parameters(registry: Any, meta: Any) -> list[str]:
	try:
		schema = registry.get_input_schema(meta.name, meta.version).to_json_schema()
	except Exception:
		return []
	required = schema.get("required") if isinstance(schema, dict) else None
	return [str(item) for item in required] if isinstance(required, list) else []


def _word_hit(word: str, text: str) -> bool:
	"""Match a query word at a word start, so ``log`` does not hit ``dialog``."""

	return re.search(r"(?<![a-z0-9])" + re.escape(word), text) is not None


def _match_score(query: str, meta: Any, title: str, summary: str) -> int | None:
	"""Return a lower-is-better rank, or ``None`` when the query misses."""

	words = [word for word in query.casefold().split() if word]
	if not words:
		return 0
	name = meta.name.casefold()
	category = str(meta.category).casefold()
	title_text = str(title).casefold()
	tags = " ".join(str(tag) for tag in getattr(meta, "tags", ())).casefold()
	haystack = " ".join((name, title_text, category, summary.casefold(), tags))
	if not all(_word_hit(word, haystack) for word in words):
		return None
	if query.casefold() == name:
		return 0
	if all(_word_hit(word, name) for word in words):
		return 1
	if _word_hit(words[0], name):
		return 2
	if all(_word_hit(word, title_text) for word in words):
		return 3
	if all(_word_hit(word, category) for word in words):
		return 4
	return 5


def find_tools(input_obj: FindToolsInput, context: ExecutionContext | None = None) -> FindToolsResult:
	"""Search compact tool summaries by text, category, and safety."""

	registry = _registry(context)
	query = str(input_obj.query or "").strip()
	category = str(input_obj.category or "").strip().casefold()
	metadata = _visible_metadata(
		registry,
		developer_mode=_developer_mode(context),
		include_deprecated=bool(input_obj.include_deprecated),
	)

	ranked: list[tuple[int, str, Any, str, str]] = []
	for meta in metadata:
		if category and str(meta.category).casefold() != category:
			continue
		if input_obj.read_only is not None and bool(meta.read_only) != bool(input_obj.read_only):
			continue
		title = grouped_title(meta.category, meta.name)
		summary = _bounded_description(meta.description)
		# Match the whole description; the 220-character bound is for the
		# returned summary, not for deciding whether the tool is relevant.
		score = _match_score(query, meta, title, str(meta.description))
		if score is None:
			continue
		ranked.append((score, meta.name, meta, title, summary))

	if query:
		ranked.sort(key=lambda item: (item[0], item[1]))
	else:
		# The empty query is the catalog view: group it so the first ten
		# results are not an arbitrary alphabetic slice across categories.
		ranked.sort(key=lambda item: (str(item[2].category), item[1]))
	total = len(ranked)
	selected = ranked[: max(1, min(int(input_obj.limit), 50))]
	tools = [
		ToolSummary(
			name=meta.name,
			title=title,
			category=meta.category,
			summary=summary,
			read_only=bool(meta.read_only),
			destructive=bool(meta.destructive),
			required_permission=str(meta.required_permission),
			required_parameters=_required_parameters(registry, meta),
			tags=[str(tag) for tag in meta.tags],
		)
		for _score, _name, meta, title, summary in selected
	]
	return FindToolsResult(
		tools=tools,
		total_matches=total,
		returned=len(tools),
		available_categories=sorted({str(meta.category) for meta in metadata}),
		query=query,
		category=input_obj.category,
	)


def _resolve_tool(registry: Any, name: str, *, developer_mode: bool) -> Any:
	requested = str(name or "").strip()
	if not requested:
		raise ValueError("Tool name is required; call find_tools to search the catalog.")
	wanted = requested.casefold()
	for meta in _visible_metadata(registry, developer_mode=developer_mode):
		if meta.name.casefold() == wanted:
			return meta
	raise ValueError(f"Unknown tool {requested!r}. Call find_tools with a query to list available tools.")


def _companion_output_schema(data_schema: dict[str, Any]) -> dict[str, Any]:
	"""Describe tool data and companion metadata as returned by MCP."""
	schema = copy.deepcopy(data_schema)
	properties = schema.get("properties")
	if not isinstance(properties, dict):
		properties = {}
		schema["properties"] = properties
	if "result" not in properties:
		properties["result"] = {}
	if "trace_id" not in properties:
		properties["trace_id"] = {"type": "string"}
	properties["_meta"] = {
		"type": "object",
		"properties": {"result": {}, "trace_id": {"type": "string"}},
		"additionalProperties": False,
	}
	required = schema.get("required")
	if not isinstance(required, list):
		required = []
		schema["required"] = required
	if "_meta" not in required:
		required.append("_meta")
	return schema


def get_tool_schema(
	input_obj: GetToolSchemaInput,
	context: ExecutionContext | None = None,
) -> GetToolSchemaResult:
	"""Return one visible tool's full input schema, and optionally its output."""

	registry = _registry(context)
	meta = _resolve_tool(
		registry,
		input_obj.name,
		developer_mode=_developer_mode(context),
	)
	input_schema = registry.get_input_schema(meta.name, meta.version).to_json_schema()
	output_schema = None
	if input_obj.include_output_schema:
		output_schema = _companion_output_schema(
			registry.get_output_schema(meta.name, meta.version).to_json_schema()
		)
	required = input_schema.get("required") if isinstance(input_schema, dict) else None
	return GetToolSchemaResult(
		name=meta.name,
		title=grouped_title(meta.category, meta.name),
		category=meta.category,
		description=str(meta.description),
		read_only=bool(meta.read_only),
		destructive=bool(meta.destructive),
		required_permission=str(meta.required_permission),
		required_parameters=[str(item) for item in required] if isinstance(required, list) else [],
		input_schema=input_schema,
		output_schema=output_schema,
		tags=[str(tag) for tag in meta.tags],
	)
