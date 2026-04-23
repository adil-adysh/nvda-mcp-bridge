"""Object inspection and tree walking utilities for NVDA Bridge.

Provides functions to inspect NVDA UI objects, walk object trees,
and extract structural information for accessibility debugging.
"""

import sys
from typing import Any, Dict, Optional, Tuple

import api

from .validators import validate_target, validate_bounded_int


def _nvda_obj_role(obj: Any) -> Tuple[Optional[int], str]:
	"""Return (role_int, role_text) for any NVDA object.

	All object-info responses share the same two-field role schema:
	  "role"     – numeric controlTypes.Role value (None if unavailable)
	  "roleText" – human-readable string (empty string if unavailable)

	Centralising this avoids the previous inconsistency where
	get_current_focus returned an int while move_focus and
	get_navigator_object returned a str under the same "role" key.
	"""
	raw_role = getattr(obj, "role", None)
	return (
		int(raw_role) if raw_role is not None else None,
		getattr(obj, "roleText", None) or "",
	)


def _resolve_target_object(target: str) -> Any:
	"""Resolve a user target string to a concrete NVDA object."""
	normalized = validate_target(target)
	if normalized == "focus":
		obj = api.getFocusObject()
	elif normalized == "navigator":
		obj = api.getNavigatorObject()
	elif normalized == "desktop":
		obj = api.getDesktopObject()
	else:
		# validate_target guarantees this won't happen
		raise RuntimeError(f"Unexpected target: {normalized}")

	if not obj:
		raise RuntimeError(f"No object available for target: {normalized}")
	return obj


def _extract_location(obj: Any) -> Optional[Dict[str, int]]:
	"""Extract a best-effort location rectangle from an NVDA object."""
	location = getattr(obj, "location", None)
	if location is None:
		return None

	try:
		if isinstance(location, (tuple, list)) and len(location) >= 4:
			left, top, width, height = location[:4]
			return {
				"left": int(left),
				"top": int(top),
				"width": int(width),
				"height": int(height),
			}

		left = getattr(location, "left", None)
		top = getattr(location, "top", None)
		width = getattr(location, "width", None)
		height = getattr(location, "height", None)
		if None not in (left, top, width, height):
			return {
				"left": int(left),
				"top": int(top),
				"width": int(width),
				"height": int(height),
			}
	except Exception:
		return None

	return None


def _iter_children(obj: Any, max_children: int) -> list[Any]:
	"""Return up to max_children child objects with defensive fallbacks."""
	children: list[Any] = []
	limit = max(0, int(max_children))
	if limit == 0:
		return children

	child_count = getattr(obj, "childCount", 0)
	try:
		child_count = int(child_count)
	except Exception:
		child_count = 0

	if child_count > 0 and hasattr(obj, "getChild"):
		for index in range(min(child_count, limit)):
			try:
				child = obj.getChild(index)
				if child:
					children.append(child)
			except Exception:
				continue
		return children

	raw_children = getattr(obj, "children", None)
	if raw_children is None:
		return children

	try:
		for child in raw_children:
			if child:
				children.append(child)
			if len(children) >= limit:
				break
	except Exception:
		return children

	return children


def _serialize_object(obj: Any, include_relations: bool = False) -> Dict[str, Any]:
	"""Convert an NVDA object to a stable JSON-serializable dictionary."""
	role_int, role_text = _nvda_obj_role(obj)
	data: Dict[str, Any] = {
		"name": getattr(obj, "name", None) or "",
		"role": role_int,
		"roleText": role_text,
		"value": getattr(obj, "value", None) or "",
		"description": getattr(obj, "description", None) or "",
		"states": str(getattr(obj, "states", "")),
		"windowClassName": getattr(obj, "windowClassName", None) or "",
		"windowControlID": int(getattr(obj, "windowControlID", 0) or 0),
		"childCount": int(getattr(obj, "childCount", 0) or 0),
		"pythonClass": type(obj).__name__,
	}

	location = _extract_location(obj)
	if location is not None:
		data["location"] = location

	if include_relations:
		parent = getattr(obj, "parent", None)
		next_obj = getattr(obj, "next", None)
		previous_obj = getattr(obj, "previous", None)
		data["relations"] = {
			"parent": {
				"name": getattr(parent, "name", None) or "",
				"role": int(getattr(parent, "role", 0) or 0) if parent is not None else None,
			}
			if parent is not None
			else None,
			"next": {
				"name": getattr(next_obj, "name", None) or "",
				"role": int(getattr(next_obj, "role", 0) or 0) if next_obj is not None else None,
			}
			if next_obj is not None
			else None,
			"previous": {
				"name": getattr(previous_obj, "name", None) or "",
				"role": int(getattr(previous_obj, "role", 0) or 0) if previous_obj is not None else None,
			}
			if previous_obj is not None
			else None,
		}

	return data


def _build_tree(node: Any, remaining_depth: int, max_children: int, seen: set[int]) -> Dict[str, Any]:
	"""Recursively build a bounded object tree with cycle detection and depth safety."""
	node_id = id(node)
	if node_id in seen:
		cycle_node = _serialize_object(node)
		cycle_node["cycle"] = True
		cycle_node["children"] = []
		return cycle_node

	seen.add(node_id)
	result = _serialize_object(node)
	result["children"] = []

	# Safety check: stop recursion if depth exhausted
	if remaining_depth <= 0:
		seen.remove(node_id)
		return result

	children = _iter_children(node, max_children=max_children)
	for child in children:
		result["children"].append(
			_build_tree(child, remaining_depth=remaining_depth - 1, max_children=max_children, seen=seen)
		)

	seen.remove(node_id)
	return result


def inspect_object(target: str = "focus", include_relations: bool = True) -> Dict[str, Any]:
	"""Inspect a single NVDA object with optional parent/neighbor relation hints.

	Args:
		target: Object target ("focus", "navigator", or "desktop")
		include_relations: Whether to include parent/next/previous relations

	Returns:
		Dictionary with target and object serialization

	Raises:
		ValueError: If target is invalid
		RuntimeError: If object cannot be resolved
	"""
	obj = _resolve_target_object(target)
	return {
		"target": target.lower(),
		"object": _serialize_object(obj, include_relations=include_relations),
	}


def walk_object_tree(target: str = "focus", max_depth: int = 2, max_children: int = 20) -> Dict[str, Any]:
	"""Walk a bounded subtree for an NVDA object target.

	Args:
		target: Object target ("focus", "navigator", or "desktop")
		max_depth: Maximum depth to traverse (0-6)
		max_children: Maximum children per node (1-100)

	Returns:
		Dictionary with tree structure and metadata

	Raises:
		ValueError: If parameters are invalid
		RuntimeError: If object cannot be resolved
	"""
	validate_target(target)
	max_depth = validate_bounded_int(max_depth, "max_depth", 0, 6)
	max_children = validate_bounded_int(max_children, "max_children", 1, 100)

	root = _resolve_target_object(target)
	tree = _build_tree(root, remaining_depth=max_depth, max_children=max_children, seen=set())
	return {
		"target": target.lower(),
		"max_depth": max_depth,
		"max_children": max_children,
		"tree": tree,
	}


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
	max_ancestors = validate_bounded_int(max_ancestors, "max_ancestors", 1, 100)

	focus = api.getFocusObject()
	if not focus:
		raise RuntimeError("No focus object available")

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


def get_navigator_object() -> Dict[str, Any]:
	"""Get information about the current navigator object.

	Returns:
		Dictionary with navigator object information

	Raises:
		RuntimeError: If no navigator object is available
	"""
	nav = api.getNavigatorObject()
	if not nav:
		raise RuntimeError("No navigator object available")

	role_int, role_text = _nvda_obj_role(nav)
	return {
		"name": getattr(nav, "name", None) or "",
		"role": role_int,
		"roleText": role_text,
		"value": getattr(nav, "value", None) or "",
		"description": getattr(nav, "description", None) or "",
	}


def get_desktop_object() -> Dict[str, Any]:
	"""Get information about the desktop object.

	Returns:
		Dictionary with desktop object information

	Raises:
		RuntimeError: If no desktop object is available
	"""
	desktop = api.getDesktopObject()
	if not desktop:
		raise RuntimeError("No desktop object available")

	role_int, role_text = _nvda_obj_role(desktop)
	return {
		"name": getattr(desktop, "name", None) or "",
		"role": role_int,
		"roleText": role_text,
		"childCount": getattr(desktop, "childCount", 0),
	}


def inspect_app_module(source: str = "focus") -> Dict[str, Any]:
	"""Inspect app module details resolved from a target object source.

	Args:
		source: Source object ("focus", "navigator", or "desktop")

	Returns:
		Dictionary with source object and app module details

	Raises:
		ValueError: If source is invalid
		RuntimeError: If app module cannot be resolved
	"""
	obj = _resolve_target_object(source)
	app_module = getattr(obj, "appModule", None)
	if app_module is None:
		raise RuntimeError(f"No app module available for source: {source}")

	module_name = type(app_module).__module__
	module_file = None
	loaded_module = sys.modules.get(module_name)
	if loaded_module is not None:
		module_file = getattr(loaded_module, "__file__", None)

	is_alive = getattr(app_module, "isAlive", None)
	if callable(is_alive):
		try:
			is_alive = bool(is_alive())
		except Exception:
			is_alive = None

	return {
		"source": source.lower(),
		"object": _serialize_object(obj),
		"appModule": {
			"className": type(app_module).__name__,
			"classModule": module_name,
			"moduleFile": module_file,
			"appName": getattr(app_module, "appName", None) or "",
			"productName": getattr(app_module, "productName", None) or "",
			"productVersion": getattr(app_module, "productVersion", None) or "",
			"processID": int(getattr(app_module, "processID", 0) or 0),
			"sleepMode": bool(getattr(app_module, "sleepMode", False)),
			"isAlive": is_alive,
		},
	}
