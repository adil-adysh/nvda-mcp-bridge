#!/usr/bin/env python3
"""Generate NVDA MCP Bridge tool discovery schema."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import types
from pathlib import Path

PACKAGE_NAME = "nvdaMcpBridge"


def bootstrap_addon_package(addon_root: Path, package_name: str = PACKAGE_NAME) -> None:
    """Create a synthetic package alias for the addon and stub NVDA dependencies."""
    package_name = package_name.replace("-", "_")

    if package_name in sys.modules:
        del sys.modules[package_name]

    package = types.ModuleType(package_name)
    package.__path__ = [str(addon_root)]
    package.__package__ = package_name
    sys.modules[package_name] = package

    log_handler = types.ModuleType("logHandler")
    log_handler.log = types.SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )
    sys.modules["logHandler"] = log_handler

    queue_handler = types.ModuleType("queueHandler")
    queue_handler.eventQueue = None
    queue_handler.queueFunction = lambda queue, func, *args, **kwargs: func(*args, **kwargs)
    sys.modules["queueHandler"] = queue_handler


def generate_schema(addon_root: Path, package_name: str = PACKAGE_NAME) -> dict[str, object]:
    bootstrap_addon_package(addon_root, package_name)

    tools_registry = importlib.import_module(f"{package_name}.tools.registry")
    output_types = importlib.import_module(
        f"{package_name}.validation.output_type_registry"
    )
    protocol = importlib.import_module(f"{package_name}.server.protocol")

    registry = tools_registry.NVDA_TOOLS_REGISTRY
    built_types = output_types.build_output_type_registry()

    tools = []
    for meta in registry.list_tools():
        input_schema = registry.get_input_schema(meta.name, meta.version)
        output_schema = registry.get_output_schema(meta.name, meta.version)

        if hasattr(input_schema.model, "schema"):
            input_schema_json = input_schema.model.schema()
        else:
            input_schema_json = input_schema.to_json_schema()

        output_type_name = getattr(output_schema.model, "__name__", None)
        if output_type_name is None:
            output_type_name = str(output_schema.model)

        tools.append(
            {
                "name": meta.name,
                "version": meta.version,
                "description": meta.description,
                "category": meta.category,
                "inputSchema": input_schema_json,
                "outputType": output_type_name,
            }
        )

    return {
        "protocol": protocol.PROTOCOL,
        "protocol_version": protocol.PROTOCOL_VERSION,
        "bridge_version": protocol.BRIDGE_VERSION,
        "api_level": protocol.API_LEVEL,
        "types": built_types,
        "tools": tools,
        "count": len(tools),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate NVDA MCP Bridge tool discovery JSON schema."
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Write generated schema JSON to this file. Defaults to stdout.",
    )
    return parser.parse_args()


def main() -> int:
    repo_root = Path(__file__).resolve().parent
    addon_root = repo_root / ".." / "addon" / "globalPlugins" / "nvdaMcp-bridge"
    schema_payload = generate_schema(addon_root.resolve())
    output = json.dumps(schema_payload, indent=2)

    args = parse_args()
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
