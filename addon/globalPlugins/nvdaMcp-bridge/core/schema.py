"""Schema definition and validation framework.

Defines how tool input and output schemas are enforced.
No dynamic schema construction. All schemas declared explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar, Generic, Type

try:
	from pydantic import BaseModel, ValidationError as PydanticValidationError

	PYDANTIC_AVAILABLE = True
except ImportError:
	PYDANTIC_AVAILABLE = False


T = TypeVar("T")


@dataclass(frozen=True)
class SchemaVersion:
	"""Semantic version for a schema.

	Breaking changes to input/output require major version bump.
	"""

	major: int
	minor: int = 0
	patch: int = 0

	def __str__(self) -> str:
		return f"{self.major}.{self.minor}.{self.patch}"

	def __lt__(self, other: SchemaVersion) -> bool:
		return (self.major, self.minor, self.patch) < (
			other.major,
			other.minor,
			other.patch,
		)

	@classmethod
	def from_string(cls, version: str) -> SchemaVersion:
		"""Parse semantic version string."""
		parts = version.split(".")
		if len(parts) < 1 or len(parts) > 3:
			raise ValueError(f"Invalid version format: {version}")
		try:
			major = int(parts[0])
			minor = int(parts[1]) if len(parts) > 1 else 0
			patch = int(parts[2]) if len(parts) > 2 else 0
			return cls(major, minor, patch)
		except ValueError as e:
			raise ValueError(f"Invalid version format: {version}") from e


class InputSchema(Generic[T]):
	"""Input schema validator - enforces strict input typing.

	Rules:
	- All fields explicitly typed (no dynamic types)
	- No implicit coercion
	- No extra fields allowed
	- All required fields declared
	"""

	def __init__(
		self,
		model: Type[T],
		version: SchemaVersion | str = "1.0.0",
		allow_extra_fields: bool = False,
		description: str = "",
	) -> None:
		"""Initialize input schema.

		Args:
			model: Pydantic BaseModel class or TypedDict class
			version: Schema version
			allow_extra_fields: Whether extra fields are allowed (default: False)
			description: Human-readable description
		"""
		self.model = model
		self.version = SchemaVersion.from_string(version) if isinstance(version, str) else version
		self.allow_extra_fields = allow_extra_fields
		self.description = description
		self._frozen = False

	def validate(self, data: Any) -> T:
		"""Validate raw input against schema.

		Args:
			data: Raw input data (typically from JSON)

		Returns:
			Validated typed instance

		Raises:
			ValidationError: If data doesn't match schema
		"""
		if not isinstance(data, dict):
			from .error import ValidationError, ErrorCode

			raise ValidationError(
				tool_name="<unknown>",
				version=str(self.version),
				message=f"Input must be dict, got {type(data).__name__}",
				error_code=ErrorCode.INVALID_INPUT_TYPE,
			)

		# Check for extra fields
		if not self.allow_extra_fields and PYDANTIC_AVAILABLE:
			if hasattr(self.model, "__fields__"):
				# Pydantic v1 uses __fields__
				allowed_keys = set(self.model.__fields__.keys())
				extra_keys = set(data.keys()) - allowed_keys
				if extra_keys:
					from .error import ValidationError, ErrorCode

					raise ValidationError(
						tool_name="<unknown>",
						version=str(self.version),
						message=f"Extra fields not allowed: {extra_keys}",
						error_code=ErrorCode.EXTRA_FIELD_NOT_ALLOWED,
						details={"extra_fields": list(extra_keys)},
					)

		# Validate with Pydantic
		if PYDANTIC_AVAILABLE and issubclass(self.model, BaseModel):
			try:
				# Pydantic v1 uses parse_obj()
				return self.model.parse_obj(data)
			except PydanticValidationError as e:
				from .error import ValidationError, ErrorCode

				raise ValidationError(
					tool_name="<unknown>",
					version=str(self.version),
					message=str(e),
					error_code=ErrorCode.INVALID_INPUT_SCHEMA,
					details={"pydantic_errors": e.errors()},
				) from e

		# Fallback: basic type checking without Pydantic
		try:
			return self.model(**data)
		except (TypeError, ValueError) as e:
			from .error import ValidationError, ErrorCode

			raise ValidationError(
				tool_name="<unknown>",
				version=str(self.version),
				message=str(e),
				error_code=ErrorCode.INVALID_INPUT_SCHEMA,
			) from e

	def freeze(self) -> None:
		"""Freeze schema - no mutations allowed after this."""
		self._frozen = True

	def is_frozen(self) -> bool:
		"""Check if schema is frozen."""
		return self._frozen

	def to_json_schema(self) -> dict[str, Any]:
		"""Convert dataclass model to JSON Schema dict.

		Returns:
			JSON Schema dictionary compatible with JSON Schema Draft 7
		"""
		import dataclasses
		import typing
		from types import UnionType

		if not dataclasses.is_dataclass(self.model):
			# Empty schema for non-dataclass models
			return {
				"$schema": "http://json-schema.org/draft-07/schema#",
				"type": "object",
				"properties": {},
				"additionalProperties": self.allow_extra_fields,
			}

		properties = {}
		required = []

		type_hints = typing.get_type_hints(self.model)

		for f in dataclasses.fields(self.model):
			# Get the type hint
			field_type = type_hints.get(f.name, f.type)

			# Handle Optional types
			is_optional = False
			origin = typing.get_origin(field_type)
			if origin in (typing.Union, UnionType):
				args = typing.get_args(field_type)
				if type(None) in args:
					is_optional = True
					# Get the non-None type
					field_type = next((arg for arg in args if arg is not type(None)), str)
					origin = typing.get_origin(field_type)

			# Map Python types to JSON schema types
			if field_type == str:
				properties[f.name] = {"type": "string"}
			elif field_type == int:
				properties[f.name] = {"type": "integer"}
			elif field_type == bool:
				properties[f.name] = {"type": "boolean"}
			elif origin is list:
				properties[f.name] = {"type": "array", "items": {"type": "string"}}
			else:
				properties[f.name] = {"type": "object"}

			# Add to required if field has no default and is not optional
			if f.default == f.default_factory == dataclasses.MISSING and not is_optional:
				required.append(f.name)

		schema = {
			"$schema": "http://json-schema.org/draft-07/schema#",
			"type": "object",
			"properties": properties,
			"additionalProperties": self.allow_extra_fields,
		}
		if required:
			schema["required"] = required
		return schema

	def assert_not_frozen(self) -> None:
		"""Raise if schema is frozen."""
		if self._frozen:
			from .error import ToolExecutionError, ErrorCode

			raise ToolExecutionError(
				error_code=ErrorCode.SCHEMA_MUTATION_ATTEMPTED,
				tool_name="<registry>",
				version=None,
				message="Cannot mutate frozen schema",
			)


class OutputSchema(Generic[T]):
	"""Output schema validator - enforces strict output typing.

	Rules:
	- Explicit return structure (no generic "result" fields)
	- Domain-specific field names only
	- No partial output allowed
	- Handler output must exactly match schema
	"""

	def __init__(
		self,
		model: Type[T],
		version: SchemaVersion | str = "1.0.0",
		description: str = "",
	) -> None:
		"""Initialize output schema.

		Args:
			model: Pydantic BaseModel class or TypedDict class
			version: Schema version
			description: Human-readable description
		"""
		self.model = model
		self.version = SchemaVersion.from_string(version) if isinstance(version, str) else version
		self.description = description
		self._frozen = False

	def validate(self, obj: Any) -> T:
		"""Validate handler output against schema.

		Args:
			obj: Object returned from handler

		Returns:
			Validated typed instance

		Raises:
			ValidationError: If object doesn't match schema
		"""
		# Strict type checking - no implicit casting
		if obj is None:
			from .error import ValidationError, ErrorCode

			raise ValidationError(
				tool_name="<unknown>",
				version=str(self.version),
				message="Output cannot be None",
				error_code=ErrorCode.HANDLER_RETURNED_NONE,
			)

		if not isinstance(obj, self.model):
			from .error import ValidationError, ErrorCode

			raise ValidationError(
				tool_name="<unknown>",
				version=str(self.version),
				message=f"Output must be {self.model.__name__}, got {type(obj).__name__}",
				error_code=ErrorCode.INVALID_OUTPUT_TYPE,
				details={
					"expected_type": self.model.__name__,
					"actual_type": type(obj).__name__,
				},
			)

		# Validate with Pydantic if available
		if PYDANTIC_AVAILABLE and issubclass(self.model, BaseModel):
			try:
				# Ensure all required fields are present
				self.model.model_validate(obj.model_dump())
			except PydanticValidationError as e:
				from .error import ValidationError, ErrorCode

				raise ValidationError(
					tool_name="<unknown>",
					version=str(self.version),
					message=str(e),
					error_code=ErrorCode.INVALID_OUTPUT_SCHEMA,
					details={"pydantic_errors": e.errors()},
				) from e

		return obj

	def freeze(self) -> None:
		"""Freeze schema - no mutations allowed after this."""
		self._frozen = True

	def is_frozen(self) -> bool:
		"""Check if schema is frozen."""
		return self._frozen

	def assert_not_frozen(self) -> None:
		"""Raise if schema is frozen."""
		if self._frozen:
			from .error import ToolExecutionError, ErrorCode

			raise ToolExecutionError(
				error_code=ErrorCode.SCHEMA_MUTATION_ATTEMPTED,
				tool_name="<registry>",
				version=None,
				message="Cannot mutate frozen schema",
			)
