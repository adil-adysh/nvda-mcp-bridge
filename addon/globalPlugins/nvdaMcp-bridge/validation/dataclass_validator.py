"""Pure Python 3.13 dataclass validation and serialization.

Zero dependencies - uses only standard library:
- dataclasses
- typing (get_type_hints, get_origin, get_args)
- datetime

Features:
- Strict type enforcement (no silent coercion)
- Recursive nested dataclass validation
- List[T] and Dict[str, T] support
- Optional type handling
- datetime ISO 8601 validation
- Structured error collection
- JSON serialization with datetime support
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any, Type, TypeVar, get_type_hints, get_origin, get_args, Union


T = TypeVar("T")


class ValidationError(Exception):
	"""Raised when validation fails.

	Attributes:
		errors: Dict mapping field paths to error messages
	"""

	def __init__(self, errors: dict[str, str]) -> None:
		self.errors = errors
		messages = "\n".join(f"  {path}: {msg}" for path, msg in errors.items())
		super().__init__(f"Validation failed:\n{messages}")

	def __repr__(self) -> str:
		return f"ValidationError({self.errors!r})"


def _is_optional(type_hint: Any) -> tuple[bool, Any]:
	"""Check if a type is Optional[T] and return (True, T) or (False, original_type).

	Optional[str] is internally Union[str, NoneType].
	"""
	origin = get_origin(type_hint)
	if origin is Union:
		args = get_args(type_hint)
		# Check if None is in the union
		none_type = type(None)
		if none_type in args:
			# Filter out NoneType to get the actual type
			non_none_types = [arg for arg in args if arg is not none_type]
			if len(non_none_types) == 1:
				return True, non_none_types[0]
			else:
				# Union[str, int, None] - treat as complex union
				return True, Union[tuple(non_none_types)]
	return False, type_hint


def _validate_field(
	field_name: str,
	value: Any,
	expected_type: Any,
	errors: dict[str, str],
	path: str = "",
) -> Any:
	"""Validate a single field value against expected type.

	Args:
		field_name: Name of the field
		value: Value to validate
		expected_type: Expected type annotation
		errors: Dict to collect errors
		path: Current path for nested validation (e.g., "parent.child")

	Returns:
		Validated/converted value
	"""
	field_path = f"{path}.{field_name}" if path else field_name

	# Handle Optional types
	is_optional, inner_type = _is_optional(expected_type)
	if value is None:
		if is_optional:
			return None
		else:
			errors[field_path] = "field is required (got None)"
			return None

	# Use inner type for further validation
	if is_optional:
		expected_type = inner_type

	# Get origin for generic types (list, dict, etc.)
	origin = get_origin(expected_type)

	# Handle datetime specially
	if expected_type is datetime or (origin is None and expected_type == datetime):
		if isinstance(value, str):
			try:
				# Parse ISO 8601 format
				return datetime.fromisoformat(value.replace("Z", "+00:00"))
			except (ValueError, AttributeError) as e:
				errors[field_path] = f"invalid datetime format (expected ISO 8601): {e}"
				return value
		elif isinstance(value, datetime):
			return value
		else:
			errors[field_path] = f"expected datetime or ISO 8601 string, got {type(value).__name__}"
			return value

	# Handle list[T]
	if origin is list:
		if not isinstance(value, list):
			errors[field_path] = f"expected list, got {type(value).__name__}"
			return value

		args = get_args(expected_type)
		if not args:
			return value  # Untyped list, just verify it's a list

		item_type = args[0]
		validated_items = []
		for i, item in enumerate(value):
			validated_item = _validate_field(
				f"[{i}]",
				item,
				item_type,
				errors,
				path=field_path,
			)
			validated_items.append(validated_item)
		return validated_items

	# Handle dict[str, T]
	if origin is dict:
		if not isinstance(value, dict):
			errors[field_path] = f"expected dict, got {type(value).__name__}"
			return value

		args = get_args(expected_type)
		if len(args) < 2:
			return value  # Untyped dict

		key_type, value_type = args[0], args[1]

		# Validate key type (should be str)
		if key_type is not str:
			errors[field_path] = f"dict keys must be str, got {key_type}"
			return value

		validated_dict = {}
		for k, v in value.items():
			if not isinstance(k, str):
				errors[f"{field_path}.<key>"] = f"expected str key, got {type(k).__name__}"
				validated_dict[k] = v
				continue

			validated_value = _validate_field(
				k,
				v,
				value_type,
				errors,
				path=field_path,
			)
			validated_dict[k] = validated_value
		return validated_dict

	# Handle nested dataclasses
	if dataclasses.is_dataclass(expected_type):
		if isinstance(value, dict):
			return validate_and_map(value, expected_type, errors, path=field_path)
		elif dataclasses.is_dataclass(value):
			return value  # Already a dataclass instance
		else:
			errors[field_path] = f"expected dict or {expected_type.__name__}, got {type(value).__name__}"
			return value

	# Handle primitive types (int, str, bool, float)
	if expected_type in (int, str, bool, float):
		# Special case: bool is subclass of int in Python, be strict
		# Check type() first to catch bool/int mismatches
		if expected_type is int and type(value) is bool:  # noqa: E721
			errors[field_path] = "expected int, got bool"
			return value
		elif expected_type is bool and type(value) is int:  # noqa: E721
			errors[field_path] = "expected bool, got int"
			return value
		elif not isinstance(value, expected_type):
			errors[field_path] = f"expected {expected_type.__name__}, got {type(value).__name__}"
			return value
		return value

	# Handle Any type
	if expected_type is Any:
		return value

	# Unknown type - try isinstance check
	try:
		if not isinstance(value, expected_type):
			errors[field_path] = f"expected {expected_type}, got {type(value).__name__}"
	except TypeError:
		# Some types can't be used with isinstance
		pass

	return value


def validate_and_map(
	data: dict[str, Any],
	target_cls: Type[T],
	errors: dict[str, str] | None = None,
	path: str = "",
	strict: bool = True,
) -> T:
	"""Validate and map a dictionary to a dataclass instance.

	Args:
		data: Input dictionary (e.g., from JSON)
		target_cls: Target dataclass type
		errors: Optional dict to collect errors (will create if None)
		path: Current path for nested validation
		strict: If True, raise ValueError for extra keys not in dataclass

	Returns:
		Instance of target_cls

	Raises:
		ValidationError: If validation fails (errors collected)
		ValueError: If strict=True and extra keys found
	"""
	if errors is None:
		errors = {}

	if not dataclasses.is_dataclass(target_cls):
		raise TypeError(f"{target_cls} is not a dataclass")

	if not isinstance(data, dict):
		errors[path or "root"] = f"expected dict, got {type(data).__name__}"
		if errors:
			raise ValidationError(errors)
		# Fallback - shouldn't reach here
		raise TypeError(f"Expected dict, got {type(data).__name__}")

	# Get type hints for the dataclass
	type_hints = get_type_hints(target_cls)

	# Get all fields
	fields = {f.name: f for f in dataclasses.fields(target_cls)}

	# Check for extra keys (strict mode)
	if strict:
		extra_keys = set(data.keys()) - set(fields.keys())
		if extra_keys:
			for key in extra_keys:
				field_path = f"{path}.{key}" if path else key
				errors[field_path] = "extra field not allowed"

	# Validate each field
	validated_data = {}
	for field_name, field in fields.items():
		field_path = f"{path}.{field_name}" if path else field_name

		# Check if field is in data
		if field_name not in data:
			# Check if field has a default value
			if field.default is not dataclasses.MISSING:
				validated_data[field_name] = field.default
				continue
			elif field.default_factory is not dataclasses.MISSING:
				validated_data[field_name] = field.default_factory()
				continue
			else:
				# Check if it's optional
				expected_type = type_hints.get(field_name, field.type)
				is_optional, _ = _is_optional(expected_type)
				if is_optional:
					validated_data[field_name] = None
					continue
				else:
					errors[field_path] = "required field missing"
					continue

		# Validate the field value
		expected_type = type_hints.get(field_name, field.type)
		validated_value = _validate_field(
			field_name,
			data[field_name],
			expected_type,
			errors,
			path=path,
		)
		validated_data[field_name] = validated_value

	# If we have errors, raise ValidationError
	if errors:
		raise ValidationError(errors)

	# Create the dataclass instance
	try:
		return target_cls(**validated_data)
	except TypeError as e:
		# Shouldn't happen if validation is correct, but catch it
		errors["__init__"] = f"failed to construct {target_cls.__name__}: {e}"
		raise ValidationError(errors)


def to_json_dict(obj: Any) -> Any:
	"""Convert a dataclass (possibly nested) to a JSON-serializable dict.

	Args:
		obj: Dataclass instance or primitive value

	Returns:
		JSON-serializable dict/list/primitive
	"""
	# Handle None
	if obj is None:
		return None

	# Handle datetime
	if isinstance(obj, datetime):
		return obj.isoformat()

	# Handle dataclasses
	if dataclasses.is_dataclass(obj):
		result = {}
		for field in dataclasses.fields(obj):
			value = getattr(obj, field.name)
			result[field.name] = to_json_dict(value)
		return result

	# Handle lists
	if isinstance(obj, list):
		return [to_json_dict(item) for item in obj]

	# Handle dicts
	if isinstance(obj, dict):
		return {k: to_json_dict(v) for k, v in obj.items()}

	# Handle primitives (int, str, bool, float)
	if isinstance(obj, (int, str, bool, float)):
		return obj

	# Unknown type - try to convert or return as-is
	return obj


# Convenience function for validation
def validate(data: dict[str, Any], target_cls: Type[T], *, strict: bool = True) -> T:
	"""Validate and convert a dict to a dataclass instance.

	Args:
		data: Input dictionary
		target_cls: Target dataclass type
		strict: Reject extra fields (default: True)

	Returns:
		Validated dataclass instance

	Raises:
		ValidationError: If validation fails
	"""
	return validate_and_map(data, target_cls, strict=strict)
