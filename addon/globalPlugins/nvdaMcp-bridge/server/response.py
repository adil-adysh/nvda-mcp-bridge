"""Build bridge response envelopes and error payloads."""

from typing import Any, Dict, Optional

from .protocol import API_LEVEL, BRIDGE_VERSION, ERROR_CODES


#: Stable recovery actions. A hint must never tell a caller to replay a
#: mutating operation automatically; ``observe`` is the remedy for a reference
#: that can no longer be trusted.
RECOVERY_FOR: Dict[int, Dict[str, str]] = {
	4001: {
		"action": "configure_token",
		"hint": "Read the bridge token from NVDA's settings and retry with the X-NVDA-Bridge-Token header.",
	},
	4003: {
		"action": "request_permission",
		"hint": "The capability is not granted; enable it in the NVDA MCP Bridge settings panel.",
	},
	4101: {
		"action": "fix_arguments",
		"hint": "The request body is not valid JSON; send one JSON object.",
	},
	4102: {
		"action": "fix_arguments",
		"hint": "Use a tool name returned by GET /tools.",
	},
	4103: {
		"action": "fix_arguments",
		"hint": "Correct the named parameters; no UI action was performed.",
	},
	4201: {
		"action": "wait",
		"hint": "The secure desktop is active; read-only diagnostics remain available.",
	},
	4202: {
		"action": "wait",
		"hint": "A modal dialog is blocking UI actions; dismiss it before retrying.",
	},
	4203: {
		"action": "wait",
		"hint": "A menu is open and blocking this category; close it or use an allowed navigation tool.",
	},
	4204: {
		"action": "none",
		"hint": "The request was cancelled before completion; re-observe before deciding what happened.",
	},
	4205: {
		"action": "observe",
		"hint": "The target is unavailable; acquire a fresh reference rather than reusing the old one.",
	},
	4206: {
		"action": "observe",
		"hint": "The reference is stale; call observe and use a ref from its refs map. Do not replay a mutation automatically.",
	},
	5001: {
		"action": "retry_later",
		"hint": "An NVDA COM call timed out; re-observe before retrying any mutating action.",
	},
	5003: {
		"action": "read_diagnostics",
		"hint": "The tool raised; read the action trace for the bounded failure details.",
	},
	5004: {
		"action": "read_diagnostics",
		"hint": "The execution deadline expired; whether the effect occurred is unknown, so re-observe.",
	},
	5303: {
		"action": "read_diagnostics",
		"hint": "The bridge reported an internal failure; read the diagnostic trace before continuing.",
	},
}


def build_error_object(code: int, message: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
	if code not in ERROR_CODES:
		# Never invent a wire code: an unknown one is a bridge failure.
		code = 5303
	spec = ERROR_CODES[code]
	error: Dict[str, Any] = {
		"code": code,
		"name": spec["name"],
		"message": message,
		"category": spec["category"],
		"retry_policy": spec["retry_policy"],
	}
	retry_after_ms = spec.get("retry_after_ms")
	if retry_after_ms is not None:
		error["retry_after_ms"] = retry_after_ms
	recovery = RECOVERY_FOR.get(code) or RECOVERY_FOR[5303]
	if isinstance(details, dict):
		# A domain error may carry a more precise recovery; it wins.
		explicit = details.get("recovery")
		if isinstance(explicit, dict):
			recovery = {**recovery, **explicit}
			details = {key: value for key, value in details.items() if key != "recovery"}
		error["details"] = details
	error["recovery"] = recovery
	return error


def build_error_envelope(
	request_id: str,
	action: str,
	code: int,
	message: str,
	nvda_status_snapshot: Dict[str, Any],
	duration_ms: float = 0.0,
	details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
	return {
		"request_id": request_id,
		"success": False,
		"action": action,
		"duration_ms": round(duration_ms, 2),
		"bridge": {
			"bridge_version": BRIDGE_VERSION,
			"api_level": API_LEVEL,
		},
		"nvda_status_snapshot": nvda_status_snapshot,
		"error": build_error_object(code=code, message=message, details=details),
	}


def build_action_error(
	request_id: str,
	action_name: str,
	duration_ms: float,
	code: int,
	message: str,
	nvda_status_snapshot: Dict[str, Any],
	details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
	return build_error_envelope(
		request_id=request_id,
		action=action_name,
		code=code,
		message=message,
		nvda_status_snapshot=nvda_status_snapshot,
		duration_ms=duration_ms,
		details=details,
	)
