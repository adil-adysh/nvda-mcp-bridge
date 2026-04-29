"""Build bridge response envelopes and error payloads."""

from typing import Any, Dict, Optional

from .protocol import API_LEVEL, BRIDGE_VERSION, ERROR_CODES


def build_error_object(code: int, message: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
	spec = ERROR_CODES.get(code, ERROR_CODES[5303])
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
	if details is not None:
		error["details"] = details
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
