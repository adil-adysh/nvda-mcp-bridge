"""Schema definition and validation framework.

Defines how tool input and output schemas are enforced.
No dynamic schema construction. All schemas declared explicitly.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, TypeVar, Generic, Type, Literal

try:
	from pydantic import BaseModel, ValidationError as PydanticValidationError

	PYDANTIC_AVAILABLE = True
except ImportError:
	PYDANTIC_AVAILABLE = False


T = TypeVar("T")

#: Dataclass field metadata that is copied verbatim into the published schema.
_SCHEMA_METADATA_KEYS = frozenset(
	{
		"description",
		"title",
		"examples",
		"default",
		"minimum",
		"maximum",
		"exclusiveMinimum",
		"exclusiveMaximum",
		"multipleOf",
		"minLength",
		"maxLength",
		"pattern",
		"minItems",
		"maxItems",
		"uniqueItems",
		"deprecated",
	}
)


def _hint_to_json_schema(field_type: Any) -> dict[str, Any]:
	"""Map a Python type hint to a JSON Schema Draft 7 fragment.

	Handles ``str``, ``int``, ``bool``, ``float``, ``list[...]``,
	``dict[str, ...]``, ``Literal[...]`` (emitted as an ``enum``), dataclasses,
	and non-optional ``Union``/``|`` members (emitted as ``anyOf``).
	Unsupported hints fall back to a permissive ``object``.
	"""
	import typing
	from types import UnionType

	origin = typing.get_origin(field_type)
	args = typing.get_args(field_type)

	# Any accepts anything, so emit an unconstrained schema.
	if field_type is Any:
		return {}

	# Union[...] -> {"anyOf": [...]} after Optional has been unwrapped by the
	# caller.
	if origin in (typing.Union, UnionType):
		return {"anyOf": [_hint_to_json_schema(arg) for arg in args]}

	# Literal[...] -> {"type": "string", "enum": [...]}
	if origin is Literal or str(origin) == "typing.Literal":
		values = [arg for arg in args]
		# Infer the JSON type from the literal values.
		if all(isinstance(arg, bool) for arg in values):
			return {"type": "boolean", "enum": values}
		if all(isinstance(arg, int) and not isinstance(arg, bool) for arg in values):
			return {"type": "integer", "enum": values}
		return {"type": "string", "enum": [str(arg) for arg in values]}

	if origin is list:
		item_type = args[0] if args else str
		return {"type": "array", "items": _hint_to_json_schema(item_type)}

	if origin is dict:
		value_type = args[1] if len(args) > 1 else Any
		value_schema = _hint_to_json_schema(value_type)
		return {"type": "object", "additionalProperties": value_schema}

	# Nested dataclasses are described in full instead of degrading to a
	# permissive object, so the published schema stays as strict as the model.
	if dataclasses.is_dataclass(field_type):
		nested = dataclass_to_json_schema(field_type)
		# A nested $schema would declare a separate schema resource; only the
		# top-level type carries it.
		nested.pop("$schema", None)
		return nested

	if field_type is str:
		return {"type": "string"}
	if field_type is int:
		return {"type": "integer"}
	if field_type is bool:
		return {"type": "boolean"}
	if field_type is float:
		return {"type": "number"}

	return {"type": "object"}


def dataclass_to_json_schema(model: Any, *, allow_extra_fields: bool = False) -> dict[str, Any]:
	"""Build a Draft 7 object schema from a dataclass model.

	Shared by input and output schemas so that a single declaration — the
	dataclass — drives validation, discovery and the generated ``schema.json``.
	"""
	import dataclasses
	import typing
	from types import UnionType

	if not dataclasses.is_dataclass(model):
		# Empty schema for non-dataclass models
		return {
			"$schema": "http://json-schema.org/draft-07/schema#",
			"type": "object",
			"properties": {},
			"additionalProperties": allow_extra_fields,
		}

	properties: dict[str, Any] = {}
	required: list[str] = []

	type_hints = typing.get_type_hints(model)

	for f in dataclasses.fields(model):
		field_type = type_hints.get(f.name, f.type)

		# Unwrap Optional[...] so we can emit a nullable schema.
		is_optional = False
		origin = typing.get_origin(field_type)
		if origin in (typing.Union, UnionType):
			args = typing.get_args(field_type)
			if type(None) in args:
				is_optional = True
				field_type = next((arg for arg in args if arg is not type(None)), str)

		field_schema = _hint_to_json_schema(field_type)

		# Field metadata is the one place bounds and descriptions live; carrying
		# it into the published schema is what makes discovery match runtime
		# validation instead of merely resembling it.
		for key, value in (f.metadata or {}).items():
			if key in _SCHEMA_METADATA_KEYS:
				field_schema[key] = value

		if is_optional:
			# Emit an unambiguous nullable schema. Using {"type": [base, "null"]}
			# alongside an "enum" is misleading (the enum would still reject null),
			# so wrap with anyOf instead.
			properties[f.name] = {
				"anyOf": [field_schema, {"type": "null"}],
			}
		else:
			properties[f.name] = field_schema

		# Add to required if the field has no default and is not optional.
		if f.default == f.default_factory == dataclasses.MISSING and not is_optional:
			required.append(f.name)

	schema = {
		"$schema": "http://json-schema.org/draft-07/schema#",
		"type": "object",
		"properties": properties,
		"additionalProperties": allow_extra_fields,
	}
	if required:
		schema["required"] = required
	description = (getattr(model, "__doc__", None) or "").strip()
	if description:
		schema["description"] = description.splitlines()[0]
	return schema


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

		# Strict dataclass validation: rejects extra fields, missing required
		# fields and wrong types (including bool-for-int) instead of silently
		# constructing an instance from whatever the caller sent.
		import dataclasses

		if dataclasses.is_dataclass(self.model):
			from ..validation.dataclass_validator import ValidationError as DataclassValidationError
			from ..validation.dataclass_validator import validate as strict_validate

			try:
				return strict_validate(
					data,
					self.model,
					strict=not self.allow_extra_fields,
				)
			except DataclassValidationError as e:
				from .error import ValidationError, ErrorCode

				raise ValidationError(
					tool_name="<unknown>",
					version=str(self.version),
					message=str(e),
					error_code=ErrorCode.INVALID_INPUT_SCHEMA,
					details={"field_errors": e.errors},
				) from e

		# Fallback: basic construction for non-dataclass models
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
		"""Convert the model to a JSON Schema dict.

		Returns:
			JSON Schema dictionary compatible with JSON Schema Draft 7.

		Constrained string fields declared with ``Literal[...]`` are emitted with
		an ``enum`` so that downstream tool-calling clients (MCP servers, AI
		agents) know the set of valid values. ``Optional[...]`` fields are emitted
		as ``{"anyOf": [base, {"type": "null"}]}``.
		"""
		return dataclass_to_json_schema(self.model, allow_extra_fields=self.allow_extra_fields)

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

	def to_json_schema(self) -> dict[str, Any]:
		"""Convert the output model to a JSON Schema dict (the ``data`` payload).

		Output schemas are derived from the model rather than transcribed by
		hand, so discovery, the Rust companion and the dataclass cannot drift.
		"""
		return dataclass_to_json_schema(self.model)

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
