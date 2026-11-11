"""Unified error model for tool execution framework.

All tool failures conform to a single error schema
to prevent silent failures and inconsistent error handling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorCode(Enum):
	"""Standard error codes for tool execution failures."""

	# Input validation errors
	INVALID_INPUT_SCHEMA = "invalid_input_schema"
	INVALID_INPUT_TYPE = "invalid_input_type"
	MISSING_REQUIRED_FIELD = "missing_required_field"
	EXTRA_FIELD_NOT_ALLOWED = "extra_field_not_allowed"

	# Tool registration errors
	TOOL_NOT_FOUND = "tool_not_found"
	VERSION_NOT_FOUND = "version_not_found"
	TOOL_REGISTRATION_CONFLICT = "tool_registration_conflict"
	INCOMPLETE_TOOL_CONTRACT = "incomplete_tool_contract"

	# Output validation errors
	INVALID_OUTPUT_TYPE = "invalid_output_type"
	INVALID_OUTPUT_SCHEMA = "invalid_output_schema"
	MISSING_REQUIRED_OUTPUT_FIELD = "missing_required_output_field"

	# Handler errors
	HANDLER_EXECUTION_FAILED = "handler_execution_failed"
	HANDLER_RAISED_EXCEPTION = "handler_raised_exception"
	HANDLER_RETURNED_NONE = "handler_returned_none"

	# Registry errors
	REGISTRY_FROZEN = "registry_frozen"
	SCHEMA_MUTATION_ATTEMPTED = "schema_mutation_attempted"

	# Plugin errors
	PLUGIN_LOAD_FAILED = "plugin_load_failed"
	PLUGIN_CONTRACT_VIOLATION = "plugin_contract_violation"

	# Engine errors
	EXECUTION_PIPELINE_FAILED = "execution_pipeline_failed"
	SERIALIZATION_FAILED = "serialization_failed"


@dataclass
class ToolExecutionError(Exception):
	"""Standard error response for all tool execution failures.

	Never caught silently. Always propagated with full context.
	Handlers raise domain exceptions; engine wraps them in this model.
	"""

	error_code: ErrorCode
	tool_name: str
	version: str | None = None
	message: str = ""
	details: dict[str, Any] = field(default_factory=dict)
	root_cause: Exception | None = field(default=None, repr=False)
	protocol_code: int | None = None
	"""Explicit wire error code, when the failure has a precise protocol meaning.

	Left unset, the transport maps ``error_code`` through ``PROTOCOL_CODE_FOR``.
	Set by code that knows better than a generic classification — for example a
	domain rule ("no such object") or a cancellation versus a timeout.
	"""

	def __str__(self) -> str:
		"""Format error for logging/display."""
		tool_id = f"{self.tool_name}:{self.version}" if self.version else self.tool_name
		return (
			f"[{self.error_code.value}] {tool_id}: {self.message}"
			+ (f"\n  Details: {self.details}" if self.details else "")
			+ (f"\n  Cause: {self.root_cause}" if self.root_cause else "")
		)

	def as_dict(self) -> dict[str, Any]:
		"""Serialize to transport format (JSON-safe)."""
		payload: dict[str, Any] = {
			"error_code": self.error_code.value,
			"tool_name": self.tool_name,
			"version": self.version,
			"message": self.message,
			"details": self.details,
		}
		if self.protocol_code is not None:
			payload["protocol_code"] = self.protocol_code
		return payload


class ValidationError(ToolExecutionError):
	"""Input validation failed."""

	def __init__(
		self,
		tool_name: str,
		version: str | None,
		message: str,
		error_code: ErrorCode = ErrorCode.INVALID_INPUT_SCHEMA,
		details: dict[str, Any] | None = None,
	) -> None:
		super().__init__(
			error_code=error_code,
			tool_name=tool_name,
			version=version,
			message=message,
			details=details or {},
		)


class HandlerError(ToolExecutionError):
	"""Handler execution failed."""

	def __init__(
		self,
		tool_name: str,
		version: str | None,
		message: str,
		root_cause: Exception | None = None,
		error_code: ErrorCode = ErrorCode.HANDLER_EXECUTION_FAILED,
		details: dict[str, Any] | None = None,
	) -> None:
		super().__init__(
			error_code=error_code,
			tool_name=tool_name,
			version=version,
			message=message,
			details=details or {},
			root_cause=root_cause,
			protocol_code=getattr(root_cause, "protocol_code", None),
		)


class RegistryError(ToolExecutionError):
	"""Tool registry operation failed."""

	def __init__(
		self,
		tool_name: str,
		version: str | None,
		message: str,
		error_code: ErrorCode = ErrorCode.TOOL_NOT_FOUND,
		details: dict[str, Any] | None = None,
	) -> None:
		super().__init__(
			error_code=error_code,
			tool_name=tool_name,
			version=version,
			message=message,
			details=details or {},
		)


#: Maps the internal error vocabulary onto bridge protocol codes.
#:
#: This is the single place where a tool failure becomes a wire code. Anything
#: not listed falls back to BRIDGE_INTERNAL_ERROR (5303), which callers should
#: treat as "the bridge is broken", not "my request was wrong".
PROTOCOL_CODE_FOR: dict[ErrorCode, int] = {
	# Caller corrected: bad, missing or extra input.
	ErrorCode.INVALID_INPUT_SCHEMA: 4103,
	ErrorCode.INVALID_INPUT_TYPE: 4103,
	ErrorCode.MISSING_REQUIRED_FIELD: 4103,
	ErrorCode.EXTRA_FIELD_NOT_ALLOWED: 4103,
	# Caller corrected: no such tool.
	ErrorCode.TOOL_NOT_FOUND: 4102,
	ErrorCode.VERSION_NOT_FOUND: 4102,
	# Handler contract violation: the add-on is at fault, not the caller.
	ErrorCode.INVALID_OUTPUT_TYPE: 5003,
	ErrorCode.INVALID_OUTPUT_SCHEMA: 5003,
	ErrorCode.MISSING_REQUIRED_OUTPUT_FIELD: 5003,
	ErrorCode.HANDLER_RETURNED_NONE: 5003,
}


def protocol_code_for(error: ToolExecutionError | ErrorCode) -> int:
	"""Return the protocol error code for an error instance or internal code."""
	if isinstance(error, ToolExecutionError):
		if error.protocol_code is not None:
			return error.protocol_code
		return PROTOCOL_CODE_FOR.get(error.error_code, 5303)
	return PROTOCOL_CODE_FOR.get(error, 5303)


def protocol_code_for_serialized(payload: dict[str, Any]) -> int:
	"""Return the protocol error code from a serialized error dictionary.

	Honours an explicit ``protocol_code`` set by the code that raised, then maps
	the serialized ``error_code`` name through PROTOCOL_CODE_FOR, and finally
	falls back to BRIDGE_INTERNAL_ERROR.
	"""
	explicit = payload.get("protocol_code")
	if isinstance(explicit, int) and not isinstance(explicit, bool):
		return explicit

	raw_code = payload.get("error_code")
	if isinstance(raw_code, str):
		try:
			return PROTOCOL_CODE_FOR.get(ErrorCode(raw_code), 5303)
		except ValueError:
			return 5303
	return 5303
