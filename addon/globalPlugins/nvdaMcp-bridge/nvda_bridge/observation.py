"""Capture the current observation from NVDA, with references to what was seen.

This is the only module that reads the screen. `server/state.py` is the pure half
(describe/diff/versioning) and stays importable outside NVDA; everything here runs
on NVDA's main thread and may fail on a dead object or a hung application.

Three properties are load-bearing, each from a measurement in
`docs/IMPLEMENTATION_PLAN.md`:

* **Invalidate before reading.** NVDA caches properties per object and nothing
  invalidates them behind its back: a checkbox that had just been toggled still
  reported its old `states`. A cached read costs 0.01 ms and is wrong; an
  invalidated read costs ~14 ms in Edge and is right.
* **Never walk.** Resolving a control by scanning children costs ~9 ms per node
  because every call crosses into the browser process; a 500-item page blew the
  main-thread budget and froze NVDA. So a capture records the focus, a bounded
  ancestor chain and a bounded neighbourhood, and hands back the objects
  themselves as references.
* **Bound the neighbourhood.** At most `MAX_ANCESTORS + MAX_CHILDREN +
  MAX_DIALOG_CHILDREN` nodes, so one observation has a predictable cost.
"""

from __future__ import annotations

import api
import logHandler

from ..server.state import MAX_ANCESTORS, describe, _enum_name, _safe_get

#: How many immediate children of the focus to expose as references.
MAX_CHILDREN = 10

#: How many children of the containing dialog to expose. This is what makes
#: ``dismiss`` and dialog automation possible without a tree walk. Fourteen,
#: because a Windows Find/Replace dialog spends ten controls before it reaches
#: the action buttons.
MAX_DIALOG_CHILDREN = 14


def _invalidate(obj: object) -> None:
	"""Drop NVDA's cached properties for one object, best effort."""
	invalidate = getattr(obj, "invalidateCache", None)
	if callable(invalidate):
		try:
			invalidate()
		except Exception:  # noqa: BLE001 — a stale read is better than a crash
			pass


def _children_of(obj: object, limit: int) -> list:
	"""Return up to ``limit`` immediate children, tolerating either API shape."""
	if limit <= 0:
		return []
	children: list = []
	try:
		count = int(getattr(obj, "childCount", 0) or 0)
	except Exception:  # noqa: BLE001
		count = 0
	get_child = getattr(obj, "getChild", None)
	if count > 0 and callable(get_child):
		for index in range(min(count, limit)):
			try:
				child = get_child(index)
			except Exception:  # noqa: BLE001
				continue
			if child is not None:
				children.append(child)
		return children
	try:
		for child in getattr(obj, "children", None) or ():
			if child is not None:
				children.append(child)
			if len(children) >= limit:
				break
	except Exception:  # noqa: BLE001
		return children
	return children


def _is_desktop(node: object) -> bool:
	"""Whether ``node`` is NVDA's desktop root.

	The desktop object reports ``role=WINDOW`` and ``name="Desktop"`` with the
	shell's window class, so a role check alone would mistake it for an
	application window and label every observation "Desktop".
	"""
	return _safe_get(node, "name") == "Desktop" or _safe_get(node, "windowClassName") == "#32769"


def _find_dialog(focus: object, ancestors: list) -> object | None:
	"""Find a semantic dialog or a native dialog/owned popup containing focus."""
	from .windows import root_window_handle, window_owner

	nodes = [focus, *ancestors]
	for candidate in nodes:
		if _enum_name(_safe_get(candidate, "role")) == "DIALOG":
			return candidate

	# NVDA can wrap every native control in a WINDOW object with the control's
	# name, including a dialog's OK button. Only native top-level windows can
	# qualify for the popup fallback.
	focus_root = root_window_handle(_safe_get(focus, "windowHandle"))
	for candidate in nodes:
		if _enum_name(_safe_get(candidate, "role")) != "WINDOW" or _is_desktop(candidate):
			continue
		handle = _safe_get(candidate, "windowHandle")
		root = focus_root or root_window_handle(handle)
		if root and handle == root and (
			_safe_get(candidate, "windowClassName") == "#32770" or window_owner(root)
		):
			return candidate
	return None


def _node(obj: object, ref: str, max_text: int, include_states: bool = True) -> dict:
	described = describe(obj, max_value=max_text, include_states=include_states)
	described["ref"] = ref
	return described


def _window(primary: object, ancestors: list) -> tuple[dict | None, object | None]:
	"""Describe the native top-level window containing the focused object."""
	from .windows import object_for_handle, root_window_handle, window_title

	root_handle = root_window_handle(_safe_get(primary, "windowHandle"))
	chosen = None
	for candidate in [primary, *ancestors]:
		if _is_desktop(candidate) or _enum_name(_safe_get(candidate, "role")) not in ("WINDOW", "DIALOG"):
			continue
		handle = _safe_get(candidate, "windowHandle")
		if (root_handle and handle == root_handle) or (
			not root_handle and root_window_handle(handle) == handle and handle
		):
			chosen = candidate
	if chosen is not None:
		handle = _safe_get(chosen, "windowHandle")
		return (
			{
				"ref": "w",
				"title": _safe_get(chosen, "name") or "",
				"handle": int(handle) if isinstance(handle, int) else 0,
				"class_name": _safe_get(chosen, "windowClassName") or "",
			},
			chosen,
		)
	if root_handle:
		import winUser

		window_object = object_for_handle(root_handle)
		try:
			class_name = winUser.getClassName(root_handle)
		except Exception:  # noqa: BLE001 — the window may have just closed
			class_name = ""
		return (
			{
				"ref": "w" if window_object is not None else "",
				"title": window_title(root_handle),
				"handle": root_handle,
				"class_name": class_name,
			},
			window_object,
		)
	try:
		from . import ui as nvda_ui

		foreground = nvda_ui.get_window_title()
		window_object = object_for_handle(foreground.get("handle", 0))
	except Exception:  # noqa: BLE001
		return None, None
	return (
		{
			"ref": "w" if window_object is not None else "",
			"title": foreground.get("title", ""),
			"handle": foreground.get("handle", 0),
			"class_name": foreground.get("className", ""),
		},
		window_object,
	)


#: Container roles a dialog wraps its controls in. A dialog's immediate children
#: are usually one or two groupings, not the buttons an agent needs, so the
#: capture descends through them to depth two.
_DIALOG_CONTAINER_ROLES = {"GROUPING", "PANE", "PROPERTYPAGE", "PAGE", "SECTION"}


def dialog_children(dialog: object, limit: int = MAX_DIALOG_CHILDREN) -> list:
	"""Return a dialog's actionable descendants, flattening container groupings.

	Depth is capped at two levels and the result at ``limit``: this is a bounded
	walk that only happens while a dialog is open, and it is what makes ``invoke``
	work on a dialog's buttons instead of on the grouping around them.
	"""
	queue: list[tuple] = [(child, 0) for child in _children_of(dialog, 20)]
	found: list = []
	while queue and len(found) < limit:
		node, depth = queue.pop(0)
		role = _enum_name(_safe_get(node, "role"))
		if role in _DIALOG_CONTAINER_ROLES and depth < 2:
			queue.extend((child, depth + 1) for child in _children_of(node, 10))
			continue
		found.append(node)
	return found


def find_dialog(max_ancestors: int = 8) -> object | None:
	"""Return the dialog containing the current focus, if any.

	Shared with the action layer so that "is there a dialog" means the same thing
	when observing and when dismissing.
	"""
	focus = api.getFocusObject()
	if focus is None:
		return None
	ancestors: list = []
	node = focus
	for _ in range(max(0, max_ancestors)):
		node = _safe_get(node, "parent")
		if node is None:
			break
		ancestors.append(node)
	return _find_dialog(focus, ancestors)


def capture(
	max_ancestors: int = MAX_ANCESTORS,
	max_children: int = MAX_CHILDREN,
	max_text: int = 200,
	include_children: bool = True,
	include_selection: bool = False,
	include_states: bool = True,
) -> tuple[dict, dict]:
	"""Return ``(projection, objects_by_ref)`` for the current focus.

	``projection`` is JSON-ready and is what `StateProjector` stores and diffs.
	``objects_by_ref`` holds the live NVDA objects behind each ``ref``; it is kept
	by the reference table and never serialized.
	"""
	focus = api.getFocusObject()
	if focus is None:
		return {
			"focus": None,
			"ancestors": [],
			"children": [],
			"dialog": None,
			"top_window": None,
			**attention(None),
		}, {}

	# Invalidate the focus and its ancestors *before* reading anything: reads
	# below share these objects, and NVDA's per-object cache does not notice a
	# change made by another process.
	objects: dict[str, object] = {}
	_invalidate(focus)

	ancestors: list = []
	node = focus
	for _ in range(max(0, max_ancestors)):
		node = _safe_get(node, "parent")
		if node is None:
			break
		_invalidate(node)
		ancestors.append(node)

	projection: dict = {
		"focus": _node(focus, "f", max_text, include_states),
		"ancestors": [
			_node(ancestor, f"a{index}", max_text, include_states)
			for index, ancestor in enumerate(ancestors, 1)
		],
		"children": [],
		"dialog": None,
		"top_window": None,
	}
	objects["f"] = focus
	for index, ancestor in enumerate(ancestors, 1):
		objects[f"a{index}"] = ancestor

	if include_children:
		for index, child in enumerate(_children_of(focus, max_children), 1):
			_invalidate(child)
			ref = f"c{index}"
			projection["children"].append(_node(child, ref, max_text, include_states))
			objects[ref] = child

	dialog = _find_dialog(focus, ancestors)
	if dialog is not None:
		dialog_ref = "f" if dialog is focus else next(
			(ref for ref, obj in objects.items() if obj is dialog),
			None,
		)
		if dialog_ref is None:
			dialog_ref = "d0"
			objects[dialog_ref] = dialog
		dialog_node = _node(dialog, dialog_ref, max_text, include_states)
		dialog_children_projection = []
		for index, child in enumerate(dialog_children(dialog), 1):
			_invalidate(child)
			ref = f"d{index}"
			dialog_children_projection.append(_node(child, ref, max_text, include_states))
			objects[ref] = child
		dialog_node["children"] = dialog_children_projection
		projection["dialog"] = dialog_node

	window, window_object = _window(focus, ancestors)
	projection["top_window"] = window
	projection.update(attention(focus))
	if window_object is not None:
		objects["w"] = window_object

	if include_selection:
		projection["selection"] = selection_text(focus, max_text)

	logHandler.log.debug(
		f"bridge.observe nodes={len(objects)} role={projection['focus']['role']}"
	)
	return projection, objects


def selection_text(obj: object, max_chars: int = 200) -> str:
	"""Return the selected text of an object, or an empty string."""
	try:
		import textInfos

		info = obj.makeTextInfo(textInfos.POSITION_SELECTION)
		text = info.text or ""
	except Exception:  # noqa: BLE001 — most objects have no selection
		return ""
	return text[:max_chars] if max_chars > 0 else text


def attention(focus: object = None) -> dict:
	"""Return the coherence facts that are cheaper to compute than to infer.

	``top_window`` is the window that scopes the observation, but it is derived
	from the focus chain, not from the foreground window. When those disagree an
	agent can act on a control the user cannot see, so both handles and the
	comparison travel with the observation.
	"""
	from .windows import root_window_handle

	if focus is None:
		try:
			focus = api.getFocusObject()
		except Exception:  # noqa: BLE001
			focus = None
	focus_handle = root_window_handle(_safe_get(focus, "windowHandle"))
	try:
		import winUser

		foreground_handle = root_window_handle(winUser.getForegroundWindow())
	except Exception:  # noqa: BLE001 — no foreground evidence is reported as 0
		foreground_handle = 0
	browse_mode = False
	if focus is not None:
		try:
			interceptor = getattr(focus, "treeInterceptor", None)
			browse_mode = bool(interceptor is not None and not getattr(interceptor, "passThrough", False))
		except Exception:  # noqa: BLE001 — a dead object has no mode
			browse_mode = False
	return {
		"browse_mode": browse_mode,
		"focus_window_handle": focus_handle,
		"foreground_window_handle": foreground_handle,
		"focus_in_foreground_window": bool(
			focus_handle and foreground_handle and focus_handle == foreground_handle
		),
	}
