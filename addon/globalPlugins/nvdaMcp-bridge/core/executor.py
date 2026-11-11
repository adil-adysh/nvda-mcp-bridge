"""Tool execution engine and validation pipeline.

Enforces strict execution lifecycle:
1. Resolve tool by name + version
2. Validate raw input against input_schema
3. Instantiate typed input model
4. Call handler.execute()
5. Validate returned object against output_schema
6. Serialize result

Fails fast on any mismatch.
"""

from __future__ import annotations

import dataclasses
from typing import Any, TypeVar, Generic, TYPE_CHECKING

from .base import Tool

if TYPE_CHECKING:
	from .base import ExecutionContext

from .error import (
	ToolExecutionError,
	ValidationError,
	HandlerError,
	ErrorCode,
)

T = TypeVar("T")


class ExecutionResult(Generic[T]):
	"""Result of a tool execution."""

	def __init__(
		self,
		success: bool,
		output: T | None = None,
		error: ToolExecutionError | None = None,
	) -> None:
		"""Initialize execution result.

		Args:
			success: Whether execution succeeded
			output: Tool output (if success=True)
			error: Error details (if success=False)
		"""
		if success and error is not None:
			raise ValueError("Cannot have success=True with error set")
		if not success and output is not None:
			raise ValueError("Cannot have success=False with output set")
		if not success and error is None:
			raise ValueError("Cannot have success=False without error set")

		self.success = success
		self.output = output
		self.error = error

	def __repr__(self) -> str:
		if self.success:
			return f"<ExecutionResult success={type(self.output).__name__}>"
		return f"<ExecutionResult error={self.error.error_code.value}>"

	def as_dict(self) -> dict[str, Any]:
		"""Serialize to transport format.

		Optional dataclass fields that were left at their default of ``None``
		are omitted from the payload. A handler that returns ``{"stdout": ...}``
		should not surface spurious ``captured_variables: null`` / ``execution_error:
		null`` keys, which would otherwise fail JSON Schema validation on the
		consumer (Rust) side.
		"""
		if self.success:
			# For Pydantic models, use model_dump()
			if hasattr(self.output, "model_dump"):
				return {
					"success": True,
					"output": self.output.model_dump(),
				}
			# For dataclasses, serialize field-by-field and drop unset optional
			# fields (those with a default) whose value is None.
			if dataclasses.is_dataclass(self.output) and not isinstance(self.output, type):
				from ..validation.dataclass_validator import to_json_dict

				data: dict[str, Any] = {}
				for field in dataclasses.fields(self.output):
					value = getattr(self.output, field.name)
					if value is None and field.default is not dataclasses.MISSING:
						continue
					# Nested output dataclasses must become plain dicts, otherwise
					# the transport cannot serialise them.
					data[field.name] = to_json_dict(value)
				return {
					"success": True,
					"output": data,
				}
			# For regular objects, use __dict__
			if hasattr(self.output, "__dict__"):
				return {
					"success": True,
					"output": self.output.__dict__,
				}
			# Fallback
			return {
				"success": True,
				"output": self.output,
			}
		else:
			return {
				"success": False,
				"error": self.error.as_dict(),
			}


class ToolExecutor:
	"""Executes tools with strict validation and error handling.

	Pipeline:
	1. Input validation: raw dict -> typed input
	2. Handler execution: typed input -> typed output
	3. Output validation: assert output matches schema
	4. Serialization: typed output -> transport format
	"""

	def execute(
		self,
		tool: Tool,
		raw_input: dict[str, Any],
		context: "ExecutionContext | None" = None,
	) -> ExecutionResult:
		"""Execute tool with full validation pipeline.

		Args:
			tool: Tool to execute
			raw_input: Raw input (typically from JSON)
			context: Runtime services for COORDINATOR tools

		Returns:
			ExecutionResult (always succeeds at this level)
			Check result.success to determine tool execution outcome
		"""
		metadata = tool.get_metadata()
		input_schema = tool.get_input_schema()
		output_schema = tool.get_output_schema()

		# Phase 1: INPUT VALIDATION
		try:
			validated_input = input_schema.validate(raw_input)
		except ValidationError as e:
			# Update error with tool context
			e.tool_name = metadata.name
			e.version = metadata.version
			return ExecutionResult(success=False, error=e)
		except Exception as e:
			# Wrap unexpected validation errors
			error = ValidationError(
				tool_name=metadata.name,
				version=metadata.version,
				message=str(e),
				error_code=ErrorCode.INVALID_INPUT_SCHEMA,
			)
			error.root_cause = e
			return ExecutionResult(success=False, error=error)

		# Phase 2: HANDLER EXECUTION
		try:
			output = tool.execute(validated_input, context)
		except ToolExecutionError as e:
			# Already structured (e.g. output coercion reported a contract bug);
			# keep its code instead of flattening it to a generic handler error.
			e.tool_name = metadata.name
			e.version = metadata.version
			return ExecutionResult(success=False, error=e)
		except Exception as e:
			# Handler raised domain exception. Preserve any structured detail the
			# exception carries (partial dispatch counts, for example) so a caller
			# can tell how far a cancelled sequence got.
			details: dict[str, Any] = {}
			to_details = getattr(e, "to_details", None)
			if callable(to_details):
				try:
					collected = to_details()
					if isinstance(collected, dict):
						details = collected
				except Exception:
					details = {}
			error = HandlerError(
				tool_name=metadata.name,
				version=metadata.version,
				message=f"{type(e).__name__}: {str(e)}",
				root_cause=e,
				details=details,
			)
			return ExecutionResult(success=False, error=error)

		# Phase 3: OUTPUT VALIDATION
		try:
			validated_output = output_schema.validate(output)
		except ValidationError as e:
			# Update error with tool context
			e.tool_name = metadata.name
			e.version = metadata.version
			return ExecutionResult(success=False, error=e)
		except Exception as e:
			# Wrap unexpected validation errors
			error = ValidationError(
				tool_name=metadata.name,
				version=metadata.version,
				message=str(e),
				error_code=ErrorCode.INVALID_OUTPUT_SCHEMA,
			)
			error.root_cause = e
			return ExecutionResult(success=False, error=error)

		# Phase 4: SUCCESS
		return ExecutionResult(success=True, output=validated_output)

	def execute_and_serialize(
		self,
		tool: Tool,
		raw_input: dict[str, Any],
		context: "ExecutionContext | None" = None,
	) -> dict[str, Any]:
		"""Execute tool and serialize result to transport format.

		This is the primary integration point for HTTP/MCP bridges.

		Args:
			tool: Tool to execute
			raw_input: Raw input (from JSON)
			context: Runtime services for COORDINATOR tools

		Returns:
			Dict with either {"success": True, "output": ...}
			or {"success": False, "error": {...}}
		"""
		result = self.execute(tool, raw_input, context)
		return result.as_dict()
