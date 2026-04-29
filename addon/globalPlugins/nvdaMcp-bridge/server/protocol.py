"""Bridge protocol constants and validation helpers."""

from typing import Any, Dict

DEFAULT_BRIDGE_TOKEN = "nvda-bridge-secret-change-me"
TOKEN_HEADER = "X-NVDA-Bridge-Token"
BRIDGE_VERSION = "1.0.0"
PROTOCOL = "nvda-bridge-protocol"
PROTOCOL_VERSION = "1.0.0"
API_LEVEL = 1

ERROR_CODES: Dict[int, Dict[str, Any]] = {
	4001: {"name": "AUTH_FAILURE", "category": "auth", "retry_policy": "never"},
	4101: {"name": "INVALID_JSON", "category": "validation", "retry_policy": "never"},
	4102: {"name": "INVALID_ACTION_NAME", "category": "validation", "retry_policy": "never"},
	4103: {"name": "INVALID_PARAMS_SCHEMA", "category": "validation", "retry_policy": "never"},
	4201: {"name": "SECURE_SCREEN_ACTIVE", "category": "nvda_state", "retry_policy": "after_state_change"},
	4202: {
		"name": "MODAL_DIALOG_BLOCKING",
		"category": "nvda_state",
		"retry_policy": "after_delay",
		"retry_after_ms": 500,
	},
	4203: {
		"name": "MENU_OPEN_BLOCKING",
		"category": "nvda_state",
		"retry_policy": "after_delay",
		"retry_after_ms": 500,
	},
	5001: {
		"name": "NVDA_COM_TIMEOUT",
		"category": "runtime",
		"retry_policy": "after_delay",
		"retry_after_ms": 1000,
	},
	5003: {
		"name": "TOOL_EXECUTION_EXCEPTION",
		"category": "runtime",
		"retry_policy": "after_delay",
		"retry_after_ms": 1000,
	},
	5303: {
		"name": "BRIDGE_INTERNAL_ERROR",
		"category": "internal",
		"retry_policy": "after_delay",
		"retry_after_ms": 1000,
	},
}

SECURE_SCREEN_ALLOWED_CATEGORIES = {"diagnostics", "developer"}
MODAL_BLOCKING_BLOCKED_CATEGORIES = {"focus", "navigation", "ui", "window", "text"}
MENU_OPEN_BLOCKED_CATEGORIES = {"focus", "navigation", "ui", "window"}
MAX_REQUEST_BODY_SIZE = 1024 * 1024
MAX_REQUEST_ID_LENGTH = 128


def http_status_from_error_code(code: int) -> int:
	if code == 4001:
		return 401
	if 4100 <= code < 4200:
		return 400
	if 4200 <= code < 4300:
		return 409
	if 5000 <= code < 6000:
		return 500
	return 500


def validate_tools_discovery_payload(payload: Dict[str, Any]) -> None:
	required_top = {
		"protocol",
		"protocol_version",
		"bridge_version",
		"api_level",
		"types",
		"tools",
		"count",
	}
	missing_top = required_top - set(payload.keys())
	if missing_top:
		raise RuntimeError(f"tools payload missing required fields: {', '.join(sorted(missing_top))}")

	if payload.get("protocol") != PROTOCOL:
		raise RuntimeError("tools payload has invalid protocol")
	if payload.get("protocol_version") != PROTOCOL_VERSION:
		raise RuntimeError("tools payload has invalid protocol_version")
	if payload.get("api_level") != API_LEVEL:
		raise RuntimeError("tools payload has invalid api_level")

	types = payload.get("types")
	tools = payload.get("tools")
	count = payload.get("count")
	if not isinstance(types, dict) or not types:
		raise RuntimeError("tools payload has invalid or empty types registry")
	if not isinstance(tools, list):
		raise RuntimeError("tools payload has invalid tools list")
	if not isinstance(count, int) or count != len(tools):
		raise RuntimeError("tools payload count mismatch")

	for type_name in types.keys():
		if not isinstance(type_name, str) or not type_name or not type_name[0].isupper():
			raise RuntimeError(f"invalid output type name in types registry: {type_name!r}")

	used_types = set()
	for tool in tools:
		if not isinstance(tool, dict):
			raise RuntimeError("tool entry must be an object")
		for key in ("name", "description", "inputSchema", "outputType", "version", "category"):
			if key not in tool:
				raise RuntimeError(f"tool entry missing required field: {key}")
		if "outputSchema" in tool:
			raise RuntimeError("outputSchema is forbidden in protocol v1")

		output_type = tool.get("outputType")
		if not isinstance(output_type, str) or not output_type:
			raise RuntimeError("tool outputType must be a non-empty string")
		if output_type not in types:
			raise RuntimeError(f"tool references unknown outputType: {output_type}")
		used_types.add(output_type)

	unused_types = set(types.keys()) - used_types
	if unused_types:
		raise RuntimeError("unused output types are not allowed")


def build_tools_payload(tools: list[Dict[str, Any]], types: Dict[str, Any]) -> Dict[str, Any]:
	return {
		"protocol": PROTOCOL,
		"protocol_version": PROTOCOL_VERSION,
		"bridge_version": BRIDGE_VERSION,
		"api_level": API_LEVEL,
		"types": types,
		"tools": tools,
		"count": len(tools),
	}
