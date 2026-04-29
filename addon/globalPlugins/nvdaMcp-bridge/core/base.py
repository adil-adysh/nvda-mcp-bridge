"""Generic tool base class and contracts.

Defines the Tool[InputType, OutputType] interface that all tools must conform to.
Enforces that InputType == input_schema and OutputType == output_schema.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import dataclasses
from dataclasses import dataclass, is_dataclass
from typing import Any, Callable, TypeVar, Generic, Type, Protocol

from .schema import InputSchema, OutputSchema

InputType = TypeVar("InputType")
OutputType = TypeVar("OutputType")


@dataclass(frozen=True)
class EmptyInput:
	"""Marker type for tools with no input."""
	pass


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
	def execute(self, input_obj: InputType) -> OutputType:
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

	def execute(self, input_obj: InputType) -> OutputType:
		"""Execute handler on validated input."""
		return self.handler(input_obj)

	@staticmethod
	def _coerce_output(output_model: Type[OutputType], output: Any) -> OutputType:
		"""Ensure handler return value is an output model instance."""
		if isinstance(output, output_model):
			return output

		if isinstance(output, dict) and is_dataclass(output_model):
			allowed_fields = {field.name for field in dataclasses.fields(output_model)}
			output = {key: value for key, value in output.items() if key in allowed_fields}

		if hasattr(output_model, "model_validate"):
			return output_model.model_validate(output)  # Pydantic v2

		if hasattr(output_model, "parse_obj"):
			return output_model.parse_obj(output)  # Pydantic v1

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
