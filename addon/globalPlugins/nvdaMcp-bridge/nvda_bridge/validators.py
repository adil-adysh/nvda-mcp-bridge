"""Parameter validation utilities for NVDA Bridge.

Centralized validation for common parameter patterns to eliminate duplication.
"""

from typing import Any


def validate_target(target: Any) -> str:
	"""Validate and normalize target parameter.

	Args:
		target: Should be a non-empty string in {'focus', 'navigator', 'desktop'}

	Returns:
		Normalized lowercase target string

	Raises:
		ValueError: If target is invalid
	"""
	if not isinstance(target, str) or not target.strip():
		raise ValueError("'target' must be a non-empty string")

	normalized = target.strip().lower()
	if normalized not in {"focus", "navigator", "desktop"}:
		raise ValueError(f"Invalid target: {target}. Use 'focus', 'navigator', or 'desktop'")

	return normalized


def validate_scope(scope: Any) -> str:
	"""Validate and normalize scope parameter.

	Args:
		scope: Should be a non-empty string in {'running', 'available', 'all'}

	Returns:
		Normalized lowercase scope string

	Raises:
		ValueError: If scope is invalid
	"""
	if not isinstance(scope, str) or not scope.strip():
		raise ValueError("'scope' must be a non-empty string")

	normalized = scope.strip().lower()
	if normalized not in {"running", "available", "all"}:
		raise ValueError("Invalid scope. Use 'running', 'available', or 'all'")

	return normalized


def validate_bounded_int(
	value: Any,
	name: str,
	min_val: int,
	max_val: int,
) -> int:
	"""Validate and normalize bounded integer parameter.

	Args:
		value: Should be an integer
		name: Parameter name for error messages
		min_val: Minimum allowed value (inclusive)
		max_val: Maximum allowed value (inclusive)

	Returns:
		Validated integer value

	Raises:
		ValueError: If value is invalid or out of bounds
	"""
	if not isinstance(value, int):
		raise ValueError(f"'{name}' must be an integer")

	if value < min_val or value > max_val:
		raise ValueError(f"'{name}' must be between {min_val} and {max_val}")

	return value


def validate_log_filter_level(filter_level: Any) -> str | None:
	"""Validate log filter level parameter.

	Args:
		filter_level: Should be None or a string in valid log levels

	Returns:
		Normalized filter level or None

	Raises:
		ValueError: If filter_level is invalid
	"""
	if filter_level is None:
		return None

	if not isinstance(filter_level, str):
		raise ValueError("'filter_level' must be None or a string")

	valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
	normalized = filter_level.strip().upper()

	if normalized not in valid_levels:
		raise ValueError(f"Invalid filter_level. Use one of {valid_levels} or None")

	return normalized


def validate_non_empty_string(value: Any, name: str) -> str:
	"""Validate and normalize non-empty string parameter.

	Args:
		value: Should be a non-empty string
		name: Parameter name for error messages

	Returns:
		Normalized string

	Raises:
		ValueError: If value is invalid
	"""
	if not isinstance(value, str):
		raise ValueError(f"'{name}' must be a string")

	normalized = value.strip()
	if not normalized:
		raise ValueError(f"'{name}' cannot be empty")

	return normalized


def validate_code_string(code: Any, max_length: int = 20000) -> str:
	"""Validate and check code string parameter.

	Args:
		code: Should be a non-empty string
		max_length: Maximum allowed code length

	Returns:
		Validated code string

	Raises:
		ValueError: If code is invalid
	"""
	if not isinstance(code, str):
		raise ValueError("'code' must be a string")

	if not code:
		raise ValueError("No code provided")

	if len(code) > max_length:
		raise ValueError(f"Code too long (max {max_length} characters)")

	return code


def validate_string_list(value: Any, name: str = "capture_vars") -> list[str]:
	"""Validate list of strings parameter.

	Args:
		value: Should be a list of strings
		name: Parameter name for error messages

	Returns:
		Validated list of strings

	Raises:
		ValueError: If value is invalid
	"""
	if not isinstance(value, list):
		raise ValueError(f"'{name}' must be a list")

	if not all(isinstance(item, str) for item in value):
		raise ValueError(f"'{name}' must be a list of strings")

	return value
