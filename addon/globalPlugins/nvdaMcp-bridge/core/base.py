"""Generic tool base class and contracts.

Defines the Tool[InputType, OutputType] interface that all tools must conform to.
Enforces that InputType == input_schema and OutputType == output_schema.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, is_dataclass
from enum import Enum
import threading
from typing import Any, Callable, TypeVar, Generic, Type, Protocol

from .schema import InputSchema, OutputSchema

InputType = TypeVar("InputType")
OutputType = TypeVar("OutputType")


@dataclass(frozen=True)
class EmptyInput:
	"""Marker type for tools with no input."""
	pass


class ExecutionMode(str, Enum):
	"""Where a tool's handler body runs.

	MAIN_THREAD tools are fast NVDA API calls; the engine runs the whole handler
	on NVDA's main thread. COORDINATOR tools own a sequence (pacing, retries,
	waiting for a UI change) and therefore run on the bridge worker thread,
	dispatching individual main-thread operations through the execution context.
	"""

	MAIN_THREAD = "main_thread"
	COORDINATOR = "coordinator"


@dataclass
class ResultMeta:
	"""Mutable per-request result metadata, filled in by the coordinator handler.

	Lives beside the typed output rather than inside it because the §4.1 result
	block is uniform across every tool: making each of the twenty-odd output
	dataclasses declare ``changed``/``delta``/``settled_ms`` would duplicate the
	same fields, force every published output schema to change, and still leave
	reads without anything to put in them.
	"""

	settle: Any = None
	state_version: int | None = None


@dataclass(frozen=True)
class ExecutionContext:
	"""Runtime services handed to COORDINATOR handlers.

	Nothing here may be called from the main thread for anything that blocks:
	``dispatch`` marshals one operation onto NVDA's main thread and waits, so a
	coordinator can pace between operations without freezing the UI.
	"""

	dispatch: Callable[[Any], Any] | None = None
	"""Run a single callable on NVDA's main thread and return its result."""

	cancellation_event: threading.Event | None = None
	"""Set when the caller cancelled or the request deadline expired."""

	timeout: float | None = None
	"""Seconds remaining for the whole tool call, when a deadline is known."""

	settle: Callable[..., Any] | None = None
	"""Run a settle round around an action; see ``server/settle.py``."""

	resolve: Callable[[str], Any] | None = None
	"""Resolve a ``ref`` to an NVDA object. Main thread only."""

	register: Callable[[Any], str] | None = None
	"""Capture an NVDA object under a fresh ``ref``. Main thread only."""

	observe: Callable[..., dict] | None = None
	"""Capture a projection with references. Main thread only."""

	wait_events: Callable[..., dict] | None = None
	"""Block on the transition journal. Worker thread only."""

	result: ResultMeta | None = None
	"""Per-request result metadata; the bridge serialises it into the envelope."""

	trace_store: Any = None
	"""Retained action traces, so ``get_action_trace`` can read one back.

	Set by the bridge that owns the store. A read-only diagnostic surface, not
	an action path: nothing here may mutate NVDA state.
	"""


@dataclass(frozen=True)
class ToolMetadata:
	"""Tool metadata - immutable after creation."""

	name: str
	"""Unique tool name (lowercase, alphanumeric + underscore)."""

	version: str
	"""Semantic version (e.g., '1.0.0')."""

	description: str
	"""Human-readable description of tool function."""

	category: str
	"""Tool category (e.g., 'focus', 'text', 'system')."""

	author: str | None = None
	"""Tool author/maintainer."""

	tags: tuple[str, ...] = ()
	"""Optional tags for filtering/discovery."""

	read_only: bool = False
	"""Whether the tool is read-only (observes without mutating state)."""

	destructive: bool = False
	"""Whether calling the tool may perform destructive modifications."""

	idempotent: bool = False
	"""Whether repeated calls with identical parameters produce identical results."""

	side_effects: bool = False
	"""Whether the tool has side effects (speech, audio, clipboard, OS focus)."""

	required_permission: str = "read_only"
	"""Permission capability required to execute this tool."""

	allowed_nvda_states: tuple[str, ...] = ("normal",)
	"""NVDA runtime states in which this tool is permitted (e.g., normal, modal, menu, secure_desktop)."""

	expected_duration_ms: int = 50
	"""Typical handler duration in milliseconds, excluding a settle round.

	Internal metadata, not published in ``/tools``: nothing times a call with
	it. It exists so a tool's cost is declared next to its code, and the reading
	tools' measurements (0.8 s for a 50-target list on a 2,000-link page) are why
	theirs is not a two-digit number.
	"""

	cancellation_supported: bool = False
	"""Whether long-running executions can be cancelled."""

	retry_safe: bool = False
	"""Whether safe to automatically retry upon transport failure."""

	execution_mode: ExecutionMode = ExecutionMode.MAIN_THREAD
	"""Where the handler body runs; see ExecutionMode."""

	deprecated: bool = False
	"""Whether the tool is superseded and kept only for compatibility."""

	superseded_by: str | None = None
	"""Name of the tool that replaces this one, when deprecated."""


	def __post_init__(self) -> None:
		"""Validate metadata."""
		if not self.name or not self.name.replace("_", "").isalnum():
			raise ValueError(f"Invalid tool name: {self.name}")
		if not self.version:
			raise ValueError("Version cannot be empty")
		if not self.category:
			raise ValueError("Category cannot be empty")

	def __str__(self) -> str:
		return f"{self.name}:{self.version}"


class HandlerProtocol(Protocol[InputType, OutputType]):
	"""Protocol that handler functions must conform to.

	Handlers receive validated typed input and return typed output.
	No raw dicts, no serialization, no validation logic.
	"""

	def __call__(self, input_obj: InputType) -> OutputType:
		"""Execute tool logic on validated input.

		Args:
			input_obj: Validated typed input (never raw dict)

		Returns:
			Typed output (never raw dict)

		Raises:
			Exception: Any domain-specific exception (caught by engine)
		"""
		...


class Tool(ABC, Generic[InputType, OutputType]):
	"""Generic tool base class.

	Every tool MUST:
	1. Declare InputType and OutputType
	2. Implement get_metadata()
	3. Implement _create_input_schema() returning InputSchema[InputType]
	4. Implement _create_output_schema() returning OutputSchema[OutputType]
	5. Implement execute(input: InputType) -> OutputType

	The execution engine enforces:
	- Input validation before execute() is called
	- Output validation after execute() returns
	- Exceptions are caught and wrapped in ToolExecutionError
	"""

	def __init__(self) -> None:
		"""Initialize tool with schema caching."""
		self._input_schema_cache: InputSchema[InputType] | None = None
		self._output_schema_cache: OutputSchema[OutputType] | None = None

	@abstractmethod
	def get_metadata(self) -> ToolMetadata:
		"""Return tool metadata.

		Must return frozen/immutable metadata.
		"""

	@abstractmethod
	def _create_input_schema(self) -> InputSchema[InputType]:
		"""Create input schema validator (called once, then cached).

		Must enforce:
		- All fields explicitly typed
		- No implicit coercion
		- No extra fields (unless explicitly allowed)
		- All required fields declared
		"""

	@abstractmethod
	def _create_output_schema(self) -> OutputSchema[OutputType]:
		"""Create output schema validator (called once, then cached).

		Must enforce:
		- Exact type matching (handler returns must be OutputType instance)
		- No None returns
		- No partial output
		- All required fields present
		"""

	def get_input_schema(self) -> InputSchema[InputType]:
		"""Return cached input schema validator."""
		if self._input_schema_cache is None:
			self._input_schema_cache = self._create_input_schema()
		return self._input_schema_cache

	def get_output_schema(self) -> OutputSchema[OutputType]:
		"""Return cached output schema validator."""
		if self._output_schema_cache is None:
			self._output_schema_cache = self._create_output_schema()
		return self._output_schema_cache

	@abstractmethod
	def execute(self, input_obj: InputType, context: ExecutionContext | None = None) -> OutputType:
		"""Execute tool logic on validated input.

		Preconditions (guaranteed by execution engine):
		- input_obj is an instance of InputType
		- All required input fields are present
		- No extra fields in input

		Postconditions (checked by execution engine):
		- Return value must be instance of OutputType
		- All required output fields must be present
		- No None return allowed

		Args:
			input_obj: Validated typed input (NEVER raw dict)
			context: Runtime services for COORDINATOR tools; None otherwise

		Returns:
			Typed output (NEVER raw dict), never None

		Raises:
			Exception: Any domain-specific exception
				(handled by execution engine as HandlerError)
		"""

	def __repr__(self) -> str:
		meta = self.get_metadata()
		return f"<Tool {meta}>"


class TypedToolImpl(Tool[InputType, OutputType]):
	"""Concrete implementation helper for tools using Pydantic models.

	Simplifies tool implementation by delegating schema management.
	"""

	def __init__(
		self,
		metadata: ToolMetadata,
		input_model: Type[InputType],
		output_model: Type[OutputType],
		handler: HandlerProtocol[InputType, OutputType],
		input_schema_version: str = "1.0.0",
		output_schema_version: str = "1.0.0",
	) -> None:
		"""Initialize typed tool.

		Args:
			metadata: Tool metadata
			input_model: Pydantic BaseModel or TypedDict for input
			output_model: Pydantic BaseModel or TypedDict for output
			handler: Function(InputType) -> OutputType
			input_schema_version: Input schema version
			output_schema_version: Output schema version
		"""
		super().__init__()
		self.metadata = metadata
		self.input_model = input_model
		self.output_model = output_model
		self.handler = handler
		self._accepts_context = False
		self._input_schema = InputSchema(
			input_model,
			version=input_schema_version,
			allow_extra_fields=False,
		)
		self._output_schema = OutputSchema(
			output_model,
			version=output_schema_version,
		)

	def get_metadata(self) -> ToolMetadata:
		"""Return tool metadata."""
		return self.metadata

	def _create_input_schema(self) -> InputSchema[InputType]:
		"""Return input schema."""
		return self._input_schema

	def _create_output_schema(self) -> OutputSchema[OutputType]:
		"""Return output schema."""
		return self._output_schema

	def execute(self, input_obj: InputType, context: ExecutionContext | None = None) -> OutputType:
		"""Execute handler on validated input."""
		if self._accepts_context:
			return self.handler(input_obj, context)
		return self.handler(input_obj)

	@staticmethod
	def _coerce_output(output_model: Type[OutputType], output: Any) -> OutputType:
		"""Ensure handler return value is an output model instance.

		Undefined keys are an error, not something to silently drop: a handler
		that reports a field its output model does not declare is a contract
		bug, and dropping it here would hide exactly the drift that broke
		``send_keyboard``.
		"""
		if isinstance(output, output_model):
			return output

		if hasattr(output_model, "model_validate"):
			return output_model.model_validate(output)  # Pydantic v2

		if hasattr(output_model, "parse_obj"):
			return output_model.parse_obj(output)  # Pydantic v1

		if is_dataclass(output_model) and isinstance(output, dict):
			from ..validation.dataclass_validator import ValidationError as DataclassValidationError
			from ..validation.dataclass_validator import validate_and_map
			from .error import ErrorCode, ToolExecutionError

			try:
				return validate_and_map(output, output_model, strict=True)
			except DataclassValidationError as e:
				raise ToolExecutionError(
					error_code=ErrorCode.INVALID_OUTPUT_SCHEMA,
					tool_name="<output-coercion>",
					message=f"Handler output does not match {output_model.__name__}: {e}",
					details={"field_errors": e.errors},
					root_cause=e,
				) from e

		return output_model(**output)

	@classmethod
	def from_inputless(
		cls,
		metadata: ToolMetadata,
		output_model: Type[OutputType],
		handler: Callable[[], Any],
		input_model: Type[InputType] = EmptyInput,
		input_schema_version: str = "1.0.0",
		output_schema_version: str = "1.0.0",
	) -> "TypedToolImpl[InputType, OutputType]":
		"""Create a typed tool with no input schema."""

		def _wrapped(_: InputType) -> OutputType:
			return cls._coerce_output(output_model, handler())

		return cls(
			metadata,
			input_model,
			output_model,
			_wrapped,
			input_schema_version=input_schema_version,
			output_schema_version=output_schema_version,
		)

	@classmethod
	def from_handler(
		cls,
		metadata: ToolMetadata,
		input_model: Type[InputType],
		output_model: Type[OutputType],
		handler: Callable[[InputType], Any],
		input_schema_version: str = "1.0.0",
		output_schema_version: str = "1.0.0",
	) -> "TypedToolImpl[InputType, OutputType]":
		"""Create a typed tool from a handler returning either raw dict or typed output."""

		def _wrapped(input_obj: InputType) -> OutputType:
			return cls._coerce_output(output_model, handler(input_obj))

		return cls(
			metadata,
			input_model,
			output_model,
			_wrapped,
			input_schema_version=input_schema_version,
			output_schema_version=output_schema_version,
		)

	@classmethod
	def from_coordinator(
		cls,
		metadata: ToolMetadata,
		input_model: Type[InputType],
		output_model: Type[OutputType],
		handler: Callable[[InputType, ExecutionContext | None], Any],
		input_schema_version: str = "1.0.0",
		output_schema_version: str = "1.0.0",
	) -> "TypedToolImpl[InputType, OutputType]":
		"""Create a coordinator-mode tool whose handler receives an execution context.

		Coordinator tools run on the bridge worker thread so they can pace
		between operations; ``context.dispatch`` moves a single operation onto
		NVDA's main thread. This keeps one execution pipeline for sequenced work
		instead of a special case inside the HTTP bridge.
		"""
		if metadata.execution_mode is not ExecutionMode.COORDINATOR:
			raise ValueError(
				f"Tool '{metadata.name}' must declare ExecutionMode.COORDINATOR to use from_coordinator()",
			)

		def _wrapped(input_obj: InputType, context: ExecutionContext | None) -> OutputType:
			return cls._coerce_output(output_model, handler(input_obj, context))

		tool = cls(
			metadata,
			input_model,
			output_model,
			_wrapped,
			input_schema_version=input_schema_version,
			output_schema_version=output_schema_version,
		)
		tool._accepts_context = True
		return tool
