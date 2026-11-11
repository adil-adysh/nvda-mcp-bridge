"""Unit tests for dataclass_validator module.

Tests cover:
- Valid payloads
- Type mismatches
- Extra fields (strict mode)
- Missing required fields
- Optional fields
- Nested dataclasses
- Lists and dicts
- datetime handling
- Error collection
"""

import sys
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# Add addon directory to path for imports
_ADDON_DIR = os.path.join(
	os.path.dirname(os.path.abspath(__file__)), "..", "addon", "globalPlugins", "nvdaMcp-bridge"
)
sys.path.insert(0, _ADDON_DIR)

import pytest
from validation.dataclass_validator import (  # noqa: E402
	validate,
	to_json_dict,
	ValidationError,
)


# ============================================================================
# Test Models
# ============================================================================


@dataclass(frozen=True)
class SimpleModel:
	"""Simple model with primitive types."""

	name: str
	count: int
	enabled: bool


@dataclass(frozen=True)
class Position:
	"""Nested model for testing."""

	x: int
	y: int


@dataclass(frozen=True)
class ComplexModel:
	"""Complex model with all feature types."""

	name: str
	value: Optional[str] = None
	position: Optional[Position] = None
	tags: list[str] = field(default_factory=list)
	metadata: dict[str, str] = field(default_factory=dict)
	created_at: Optional[datetime] = None
	count: int = 0


# ============================================================================
# Test Cases
# ============================================================================


def test_valid_simple_payload():
	"""Test 1: Valid simple payload."""
	data = {"name": "test", "count": 42, "enabled": True}
	obj = validate(data, SimpleModel)
	assert obj.name == "test"
	assert obj.count == 42
	assert obj.enabled is True


def test_type_mismatch():
	"""Test 2: Type mismatch (count should be int, got str)."""
	data = {
		"name": "test",
		"count": "not-a-number",  # Wrong type
		"enabled": True,
	}
	with pytest.raises(ValidationError) as excinfo:
		validate(data, SimpleModel)
	assert "count" in excinfo.value.errors
	assert "expected int" in excinfo.value.errors["count"]


def test_extra_field_strict():
	"""Test 3: Extra field not allowed in strict mode."""
	data = {
		"name": "test",
		"count": 42,
		"enabled": True,
		"extra_field": "not allowed",  # Extra field
	}
	with pytest.raises(ValidationError) as excinfo:
		validate(data, SimpleModel)
	assert "extra_field" in excinfo.value.errors
	assert "extra field not allowed" in excinfo.value.errors["extra_field"]


def test_missing_required_field():
	"""Test 4: Missing required field."""
	data = {
		"name": "test",
		# Missing "count" and "enabled"
	}
	with pytest.raises(ValidationError) as excinfo:
		validate(data, SimpleModel)
	assert "count" in excinfo.value.errors
	assert "enabled" in excinfo.value.errors


def test_optional_fields():
	"""Test 5: Optional fields can be None or omitted."""
	data1 = {
		"name": "test1",
		"value": None,  # Explicitly None
	}
	obj1 = validate(data1, ComplexModel)
	assert obj1.value is None

	data2 = {
		"name": "test2"
		# value omitted - should default to None
	}
	obj2 = validate(data2, ComplexModel)
	assert obj2.value is None


def test_nested_dataclass():
	"""Test 6: Nested dataclass validation."""
	data = {"name": "test", "position": {"x": 100, "y": 200}}
	obj = validate(data, ComplexModel)
	assert obj.position is not None
	assert obj.position.x == 100
	assert obj.position.y == 200


def test_nested_validation_error():
	"""Test 7: Nested validation error (position.x wrong type)."""
	data = {
		"name": "test",
		"position": {
			"x": "not-a-number",  # Wrong type
			"y": 200,
		},
	}
	with pytest.raises(ValidationError) as excinfo:
		validate(data, ComplexModel)
	assert "position.x" in excinfo.value.errors


def test_list_validation():
	"""Test 8: List[str] validation."""
	data = {"name": "test", "tags": ["tag1", "tag2", "tag3"]}
	obj = validate(data, ComplexModel)
	assert obj.tags == ["tag1", "tag2", "tag3"]

	data_bad = {
		"name": "test",
		"tags": ["tag1", 123, "tag3"],  # 123 is not a string
	}
	with pytest.raises(ValidationError) as excinfo:
		validate(data_bad, ComplexModel)
	assert "tags.[1]" in excinfo.value.errors


def test_dict_validation():
	"""Test 9: Dict[str, str] validation."""
	data = {"name": "test", "metadata": {"key1": "value1", "key2": "value2"}}
	obj = validate(data, ComplexModel)
	assert obj.metadata == {"key1": "value1", "key2": "value2"}

	data_bad = {
		"name": "test",
		"metadata": {
			"key1": "value1",
			"key2": 123,  # Should be string
		},
	}
	with pytest.raises(ValidationError) as excinfo:
		validate(data_bad, ComplexModel)
	assert "metadata.key2" in excinfo.value.errors


def test_datetime_validation():
	"""Test 10: datetime field validation (ISO 8601)."""
	test_cases = [
		"2026-02-21T10:30:00",
		"2026-02-21T10:30:00+00:00",
		"2026-02-21T10:30:00Z",
	]
	for iso_str in test_cases:
		data = {"name": "test", "created_at": iso_str}
		obj = validate(data, ComplexModel)
		assert isinstance(obj.created_at, datetime)

	data_bad = {"name": "test", "created_at": "not-a-datetime"}
	with pytest.raises(ValidationError) as excinfo:
		validate(data_bad, ComplexModel)
	assert "created_at" in excinfo.value.errors


def test_to_json_dict():
	"""Test 11: Serialization with to_json_dict."""
	data = {
		"name": "test",
		"position": {"x": 100, "y": 200},
		"tags": ["tag1", "tag2"],
		"metadata": {"key": "value"},
		"created_at": "2026-02-21T10:30:00+00:00",
		"count": 42,
	}
	obj = validate(data, ComplexModel)
	json_data = to_json_dict(obj)
	assert json_data["name"] == "test"
	assert json_data["position"]["x"] == 100
	assert json_data["tags"] == ["tag1", "tag2"]
	assert json_data["metadata"] == {"key": "value"}
	assert isinstance(json_data["created_at"], str)
	assert json_data["count"] == 42


def test_default_values():
	"""Test 12: Default values in dataclass."""
	data = {
		"name": "test"
	}
	obj = validate(data, ComplexModel)
	assert obj.count == 0
	assert obj.tags == []
	assert obj.metadata == {}


def test_bool_int_strictness():
	"""Test 13: bool/int strictness (bool is subclass of int in Python)."""
	data = {
		"name": "test",
		"count": True,  # bool where int expected - should fail
		"enabled": True,
	}
	with pytest.raises(ValidationError) as excinfo:
		validate(data, SimpleModel)
	assert "count" in excinfo.value.errors


def test_multiple_errors_collected():
	"""Test 14: Multiple errors collected (not fail-fast)."""
	data = {
		"name": 123,  # Should be str
		"count": "not-a-number",  # Should be int
		"enabled": "not-a-bool",  # Should be bool
		"extra": "field",  # Extra field
	}
	with pytest.raises(ValidationError) as excinfo:
		validate(data, SimpleModel, strict=True)
	assert len(excinfo.value.errors) >= 3


# ============================================================================
# Run All Tests
# ============================================================================


def run_all_tests():
	"""Run all test cases."""
	print("=" * 70)
	print("DATACLASS VALIDATOR UNIT TESTS")
	print("=" * 70)
	print()

	tests = [
		test_valid_simple_payload,
		test_type_mismatch,
		test_extra_field_strict,
		test_missing_required_field,
		test_optional_fields,
		test_nested_dataclass,
		test_nested_validation_error,
		test_list_validation,
		test_dict_validation,
		test_datetime_validation,
		test_to_json_dict,
		test_default_values,
		test_bool_int_strictness,
		test_multiple_errors_collected,
	]

	passed = 0
	failed = 0

	for test in tests:
		try:
			result = test()
			if result:
				passed += 1
			else:
				failed += 1
		except Exception as e:
			print(f"  FAIL Test raised unexpected exception: {e}")
			import traceback

			traceback.print_exc()
			failed += 1
		print()

	print("=" * 70)
	print(f"RESULTS: {passed} passed, {failed} failed out of {len(tests)} tests")
	print("=" * 70)

	return failed == 0


if __name__ == "__main__":
	success = run_all_tests()
	sys.exit(0 if success else 1)
