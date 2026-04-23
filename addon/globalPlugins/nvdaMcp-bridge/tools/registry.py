"""Build immutable tool registry using new architecture.

Replaces old default_tools.py and registry_builder.py.
"""

from ..core import ToolRegistryBuilder
from .nvda_tools import ALL_NVDA_TOOLS


def create_nvda_tools_registry():
	"""Build the immutable NVDA tools registry.

	Returns:
		Immutable ToolRegistry with all 19 NVDA tools.
	"""
	builder = ToolRegistryBuilder()

	for tool in ALL_NVDA_TOOLS:
		builder.register(tool)

	return builder.build()


# Build at module load time
NVDA_TOOLS_REGISTRY = create_nvda_tools_registry()


def get_tools_discovery():
	"""Get tool discovery information for MCP protocol.

	Returns tools list with metadata and schemas.
	"""
	tools = []
	for meta in NVDA_TOOLS_REGISTRY.list_tools():
		# Build schema info
		input_schema = NVDA_TOOLS_REGISTRY.get_input_schema(meta.name, meta.version)
		output_schema = NVDA_TOOLS_REGISTRY.get_output_schema(meta.name, meta.version)

		tools.append(
			{
				"name": meta.name,
				"version": meta.version,
				"description": meta.description,
				"category": meta.category,
				"author": meta.author,
				"tags": list(meta.tags),
				"input_schema": input_schema.model.schema() if hasattr(input_schema.model, "schema") else {},
				"output_schema": output_schema.model.schema()
				if hasattr(output_schema.model, "schema")
				else {},
			}
		)

	return tools
