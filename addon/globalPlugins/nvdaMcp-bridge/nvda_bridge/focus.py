"""Focus and navigation utilities for NVDA Bridge.

Provides functions to get focus information and navigate between objects.
"""

from typing import Any, Dict

import api

from .errors import DomainUnavailableError
from .objects import _nvda_obj_role


def get_current_focus() -> Dict[str, Any]:
	"""Get information about the currently focused object.

	Returns:
		Dictionary with focus information

	Raises:
		RuntimeError: If no focus object is available
	"""
	focus = api.getFocusObject()
	if not focus:
		raise DomainUnavailableError("No focus object available")

	role_int, role_text = _nvda_obj_role(focus)
	return {
		"name": getattr(focus, "name", None) or "",
		"role": role_int,
		"roleText": role_text,
		"value": getattr(focus, "value", None) or "",
		"description": getattr(focus, "description", None) or "",
		"states": str(getattr(focus, "states", "")),
		"windowClassName": getattr(focus, "windowClassName", None) or "",
		"windowControlID": getattr(focus, "windowControlID", 0),
	}


def move_focus(direction: str) -> Dict[str, Any]:
	"""Move focus to next or previous object.

	Args:
		direction: "next" or "previous"

	Returns:
		Dictionary with navigation result

	Raises:
		ValueError: If direction is invalid
		RuntimeError: If no focus object or navigation fails
	"""
	# Validate input
	if not direction or not isinstance(direction, str):
		raise ValueError("Direction must be a non-empty string")

	focus = api.getFocusObject()
	if not focus:
		raise DomainUnavailableError("No focus object available")

	# Determine navigation direction
	if direction.lower() == "next":
		# Move to next object
		next_obj = getattr(focus, "next", None)
		if not next_obj:
			raise DomainUnavailableError("No next object available")

		next_obj.setFocus()
		# setFocus() posts an async Windows message; NVDA won't register the
		# new focus until the WM_SETFOCUS event fires on the message loop.
		# The data below reflects the *target* object we requested focus on,
		# not the result of a confirmed post-focus query.
		role_int, role_text = _nvda_obj_role(next_obj)
		return {
			"direction": "next",
			"pendingFocus": True,
			"newFocus": {
				"name": getattr(next_obj, "name", None) or "",
				"role": role_int,
				"roleText": role_text,
			},
		}

	elif direction.lower() == "previous":
		# Move to previous object
		prev_obj = getattr(focus, "previous", None)
		if not prev_obj:
			raise DomainUnavailableError("No previous object available")

		prev_obj.setFocus()
		# See note above: data reflects the target object, not confirmed focus.
		role_int, role_text = _nvda_obj_role(prev_obj)
		return {
			"direction": "previous",
			"pendingFocus": True,
			"newFocus": {
				"name": getattr(prev_obj, "name", None) or "",
				"role": role_int,
				"roleText": role_text,
			},
		}
	else:
		raise ValueError(f"Invalid direction: {direction}. Use 'next' or 'previous'")


def get_focus_lineage(max_ancestors: int = 15) -> Dict[str, Any]:
	"""Return focus object plus bounded ancestor chain for hierarchy debugging.

	Args:
		max_ancestors: Maximum ancestors to include (1-100)

	Returns:
		Dictionary with focus object and ancestor chain

	Raises:
		ValueError: If parameters are invalid
		RuntimeError: If no focus object available
	"""
	from .validators import validate_bounded_int
	from .objects import _serialize_object

	max_ancestors = validate_bounded_int(max_ancestors, "max_ancestors", 1, 100)

	focus = api.getFocusObject()
	if not focus:
		raise DomainUnavailableError("No focus object available")

	ancestors: list[Dict[str, Any]] = []
	seen: set[int] = {id(focus)}
	current = getattr(focus, "parent", None)
	depth = 1
	truncated = False

	while current is not None and depth <= max_ancestors:
		current_id = id(current)
		if current_id in seen:
			ancestors.append({"depth": depth, "cycle": True, "object": _serialize_object(current)})
			break
		seen.add(current_id)
		ancestors.append({"depth": depth, "object": _serialize_object(current)})
		current = getattr(current, "parent", None)
		depth += 1

	if current is not None and depth > max_ancestors:
		truncated = True

	return {
		"focus": _serialize_object(focus, include_relations=True),
		"ancestorCount": len(ancestors),
		"truncated": truncated,
		"ancestors": ancestors,
	}
