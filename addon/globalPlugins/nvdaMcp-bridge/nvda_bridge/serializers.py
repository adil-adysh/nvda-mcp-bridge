"""Serialization utilities for NVDA Bridge.

Centralized serialization for exceptions and NVDA objects to eliminate duplication.
"""

from typing import Dict


def serialize_exception(e: Exception) -> Dict[str, str]:
	"""Convert exception to JSON-safe dictionary.

	Args:
		e: Exception instance

	Returns:
		Dictionary with exception type and message
	"""
	return {
		"type": type(e).__name__,
		"message": str(e),
	}


def serialize_exceptions(errors: Dict[str, Exception]) -> Dict[str, Dict[str, str]]:
	"""Convert multiple exceptions to JSON-safe dictionary.

	Args:
		errors: Dictionary mapping error names to exceptions

	Returns:
		Dictionary with serialized exceptions
	"""
	return {name: serialize_exception(e) for name, e in errors.items()}
