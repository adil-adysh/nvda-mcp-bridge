from __future__ import annotations

from typing import Any, Literal


def speak_text(text: str) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.speak_text(text=text)


def show_message(message: str) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.show_message(message=message)


def inspect_object(
	target: Literal["focus", "navigator", "desktop"] = "focus",
	include_relations: bool = True,
) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.inspect_object(target=target, include_relations=include_relations)


def walk_object_tree(
	target: Literal["focus", "navigator", "desktop"] = "focus",
	max_depth: int = 2,
	max_children: int = 20,
) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.walk_object_tree(target=target, max_depth=max_depth, max_children=max_children)


def get_focus_lineage(max_ancestors: int = 15) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.get_focus_lineage(max_ancestors=max_ancestors)


def inspect_app_module(source: Literal["focus", "navigator", "desktop"] = "focus") -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.inspect_app_module(source=source)


def list_loaded_addons(scope: Literal["running", "available", "all"] = "running") -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.list_loaded_addons(scope=scope)


def health_snapshot(
	include_focus: bool = True,
	include_addons: bool = False,
	include_logs: bool = False,
	log_lines: int = 20,
) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.health_snapshot(
		include_focus=include_focus,
		include_addons=include_addons,
		include_logs=include_logs,
		log_lines=log_lines,
	)


def restart_nvda(disable_addons: bool = False, debug_logging: bool = False) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.restart_nvda(disable_addons=disable_addons, debug_logging=debug_logging)


def reload_plugins() -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.reload_plugins()


def get_logs(
	max_lines: int = 100,
	filter_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None,
	use_tail: bool = True,
) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.get_logs(max_lines=max_lines, filter_level=filter_level, use_tail=use_tail)


def _log_time_kwargs(time_filter: Any) -> dict[str, Any]:
	return {
		"last_seconds": getattr(time_filter, "last_seconds", None),
		"start": getattr(time_filter, "start", None),
		"end": getattr(time_filter, "end", None),
	}


def _log_filter_from_input(input_obj: Any) -> Any:
	from ..server import log_diagnostics

	levels = getattr(input_obj, "levels", None)
	min_level = getattr(input_obj, "min_level", None)
	if levels and min_level:
		raise ValueError("Use either 'levels' (exact set) or 'min_level' (threshold), not both")
	sources = getattr(input_obj, "sources", None)
	message = getattr(input_obj, "message", None)
	return log_diagnostics.compile_filter(
		levels=levels,
		min_level=min_level,
		source_include=getattr(sources, "include", None) if sources is not None else None,
		source_exclude=getattr(sources, "exclude", None) if sources is not None else None,
		addon=getattr(input_obj, "addon", None),
		message_contains=getattr(message, "contains", None) if message is not None else None,
		message_regex=getattr(message, "regex", None) if message is not None else None,
		case_sensitive=bool(getattr(message, "case_sensitive", False)) if message is not None else False,
		threads=getattr(input_obj, "threads", None),
		trace_id=getattr(input_obj, "trace_id", None),
		**_log_time_kwargs(getattr(input_obj, "time", None)),
	)


def _log_entry(payload: dict[str, Any]) -> Any:
	from . import models

	thread = payload.get("thread") or {}
	return models.LogEntry(
		entry_id=str(payload.get("entry_id", "")),
		timestamp=payload.get("timestamp"),
		timestamp_ms=payload.get("timestamp_ms"),
		timestamp_date=payload.get("timestamp_date"),
		timestamp_date_source=str(payload.get("timestamp_date_source", "unknown")),
		timestamp_absolute=payload.get("timestamp_absolute"),
		timestamp_timezone=str(payload.get("timestamp_timezone", "unknown")),
		level=str(payload.get("level", "UNKNOWN")),
		logger=str(payload.get("logger", "")),
		module=str(payload.get("module", "")),
		function=str(payload.get("function", "")),
		subsystem=str(payload.get("subsystem", "unknown")),
		addon=payload.get("addon"),
		thread=models.LogThread(name=str(thread.get("name", "")), id=thread.get("id")),
		message=str(payload.get("message", "")),
		line_start=int(payload.get("line_start", 0)),
		line_end=int(payload.get("line_end", 0)),
		parse_status=str(payload.get("parse_status", "malformed")),
		parse_notes=list(payload.get("parse_notes") or []),
		derived_fields=list(payload.get("derived_fields") or []),
		exception=payload.get("exception"),
		enrichment=payload.get("enrichment"),
		raw=payload.get("raw"),
	)


def search_logs(input_obj: Any) -> dict[str, Any]:
	"""Run a paged native log search on the worker thread."""
	from ..server import log_diagnostics
	from . import models

	log_diagnostics.load_bridge_secret_once()
	snapshot = log_diagnostics.snapshot_log()
	filters = _log_filter_from_input(input_obj)
	payload = log_diagnostics.search_logs(
		snapshot,
		filters,
		limit=input_obj.limit,
		cursor=input_obj.cursor,
		order=input_obj.order,
		include_raw=input_obj.include_raw,
		max_scan_records=input_obj.max_scan_records,
	)
	page = payload["page"]
	source = payload["source"]
	diagnostics = payload["diagnostics"]
	return models.LogSearchResult(
		schema_version=int(payload["schema_version"]),
		entries=[_log_entry(entry) for entry in payload["entries"]],
		page=models.LogSearchPage(
			returned=int(page["returned"]),
			has_more=bool(page["has_more"]),
			next_cursor=page.get("next_cursor"),
			limit=int(page["limit"]),
			order=str(page["order"]),
		),
		source=models.LogSourceInfo(**source),
		diagnostics=models.LogSearchDiagnostics(**diagnostics),
	)


def get_log_context(input_obj: Any) -> dict[str, Any]:
	"""Return the surrounding records of one stable log entry reference."""
	from ..server import log_diagnostics
	from . import models

	log_diagnostics.load_bridge_secret_once()
	snapshot = log_diagnostics.snapshot_log()
	payload = log_diagnostics.get_log_context(
		snapshot,
		entry_id=input_obj.entry_id,
		before=input_obj.before,
		after=input_obj.after,
		include_related_threads=input_obj.include_related_threads,
		include_raw=input_obj.include_raw,
		before_ms=input_obj.before_ms,
		after_ms=input_obj.after_ms,
	)
	diagnostics = payload["diagnostics"]
	return models.LogContextResult(
		schema_version=int(payload["schema_version"]),
		entry_id=str(payload["entry_id"]),
		anchor=_log_entry(payload["anchor"]),
		entries=[_log_entry(entry) for entry in payload["entries"]],
		before=int(payload["before"]),
		after=int(payload["after"]),
		returned=int(payload["returned"]),
		complete_before=bool(payload["complete_before"]),
		complete_after=bool(payload["complete_after"]),
		include_related_threads=bool(payload["include_related_threads"]),
		source=models.LogSourceInfo(**payload["source"]),
		diagnostics=models.LogContextDiagnostics(**diagnostics),
	)


def summarize_logs(input_obj: Any) -> dict[str, Any]:
	"""Compute deterministic, bounded log statistics."""
	from ..server import log_diagnostics
	from . import models

	log_diagnostics.load_bridge_secret_once()
	snapshot = log_diagnostics.snapshot_log()
	filters = _log_filter_from_input(input_obj)
	payload = log_diagnostics.summarize_logs(
		snapshot,
		filters,
		group_by=input_obj.group_by,
		include=input_obj.include,
		limit=input_obj.limit,
		max_scan_records=input_obj.max_scan_records,
	)
	return models.LogSummaryResult(
		schema_version=int(payload["schema_version"]),
		summary=payload["summary"],
		coverage=models.LogSummaryCoverage(**payload["coverage"]),
	)


def execute_python(
	code: str,
	snapshot_vars: bool = False,
	capture_vars: list[str] | None = None,
) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.execute_python(code=code, snapshot_vars=snapshot_vars, capture_vars=capture_vars)


# ---------------------------------------------------------------------------
# Action handlers (implementation plan P2)
#
# Every handler here is COORDINATOR mode: the body runs on the bridge worker
# thread so it can wait for the UI to settle, and each individual NVDA call is
# dispatched onto the main thread. Nothing in this section blocks the main
# thread, which is what keeps NVDA responsive while an action settles.
# ---------------------------------------------------------------------------


def _dispatch(context: Any, operation: Any) -> Any:
	"""Run ``operation`` on NVDA's main thread."""
	if context is None or getattr(context, "dispatch", None) is None:
		return operation()
	return context.dispatch(operation)


def observe(include: list[str], max_text: int, context: Any, detail: str = "summary") -> dict[str, Any]:
	"""Capture the screen, with references to what was seen."""
	result = dict(_dispatch(context, lambda: context.observe(include, max_text)))
	if detail == "summary":
		for node in _observation_nodes(result):
			node.pop("states", None)
	return result


def _observation_nodes(result: dict[str, Any]) -> list[dict[str, Any]]:
	nodes: list[dict[str, Any]] = []
	for key in ("focus", "dialog"):
		node = result.get(key)
		if isinstance(node, dict):
			nodes.append(node)
	for key in ("ancestors", "children"):
		nodes.extend(node for node in result.get(key, []) if isinstance(node, dict))
	dialog = result.get("dialog")
	if isinstance(dialog, dict):
		nodes.extend(node for node in dialog.get("children", []) if isinstance(node, dict))
	return nodes


def _validate_ref_argument(value: str | None, name: str = "ref") -> str | None:
	"""Reject a window title or other prose where a reference is required."""
	if value is None:
		return None
	if not isinstance(value, str) or not value.strip():
		raise ValueError(f"'{name}' must be a non-empty reference returned by observe or locate")
	cleaned = value.strip()
	if not cleaned[0].islower() or not cleaned.isalnum():
		raise ValueError(
			f"'{name}' must be a reference returned by observe or locate (for example 'f' or 'n3'), "
			f"not {value!r}; call observe and pass a ref from its refs map"
		)
	return cleaned


def locate(
	role: str | None,
	name: str | None,
	exact: bool,
	within: str | None,
	limit: int,
	max_nodes: int,
	timeout_ms: int,
	context: Any,
	detail: str = "summary",
) -> dict[str, Any]:
	"""Find controls by role and name; hand back references to each match.

	One main-thread hop does the whole thing — resolve ``within``, search, and
	register the matches — because the search already runs there and a second
	round trip would let the screen change between finding and registering.

	Bounds are enforced here rather than in the schema: the walk is the only
	backend that can overrun, and the caller needs a sentence explaining which
	value was wrong, not a generic schema violation.
	"""
	if not role and not name:
		raise ValueError("'locate' needs at least one of 'role' or 'name'")
	if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
		raise ValueError("'limit' must be an integer between 1 and 50")
	if not isinstance(max_nodes, int) or isinstance(max_nodes, bool) or not 1 <= max_nodes <= 2000:
		raise ValueError("'max_nodes' must be an integer between 1 and 2000")
	if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not 50 <= timeout_ms <= 3000:
		raise ValueError("'timeout_ms' must be an integer between 50 and 3000")
	within_ref = _validate_ref_argument(within, "within")

	def perform() -> dict[str, Any]:
		from ..nvda_bridge import locate as search

		root = context.resolve(within_ref) if within_ref else None
		payload, objects = search.find(
			role=role,
			name=name,
			exact=exact,
			within=root,
			limit=limit,
			max_nodes=max_nodes,
			timeout_ms=timeout_ms,
		)
		for record, obj in zip(payload["matches"], objects):
			record["ref"] = context.register(obj)
			if detail == "summary":
				for key in ("path", "depth", "automation_id", "states"):
					record.pop(key, None)
		return payload

	return _dispatch(context, perform)


def start_browse_mode(within: str | None, expect: dict[str, Any] | None, context: Any) -> dict[str, Any]:
	"""Build the document model the reading tools need, as NVDA+space does.

	Word and some PDF readers keep documents in focus mode and declare that no
	tree interceptor should be created; the quick navigation machinery the
	reading tools use does not exist until one is. That is a change to the
	user's session — afterwards the document is read in browse mode — so it is
	its own action instead of a flag a read can flip.
	"""
	from ..nvda_bridge import reading

	captured: dict[str, Any] = {}

	def perform() -> dict[str, Any]:
		def act() -> dict[str, Any]:
			root = context.resolve(_validate_ref_argument(within, "within")) if within else None
			captured.update(reading.create_document(root))
			return captured

		return context.dispatch(act)

	context.settle(perform, expect=expect)
	return dict(captured)


def list_reading_targets(
	item: str,
	limit: int,
	within: str | None,
	timeout_ms: int,
	context: Any,
) -> dict[str, Any]:
	"""Enumerate a document's headings, links, form fields, and so on.

	This is the Elements List as data: one call answers "what is the structure
	of this page", where ``locate`` would need one search per kind and would
	return them in tree order. Nothing moves — the reading position is left
	where it was, so this is a read, not a move.

	The whole thing runs in one main-thread hop for the same reason ``locate``
	does: a second round trip would let the page change between enumerating and
	registering the references. ``timeout_ms`` is what stops that hop from
	outlasting the reader's patience on a document with thousands of positions.
	"""
	from ..server import reading as rules

	item_type = rules.normalize_item(item)
	if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= rules.MAX_TARGETS:
		raise ValueError(f"'limit' must be an integer between 1 and {rules.MAX_TARGETS}")
	timeout = rules.normalize_timeout(timeout_ms)

	def perform() -> dict[str, Any]:
		from ..nvda_bridge import reading

		root = context.resolve(_validate_ref_argument(within, "within")) if within else None
		payload, objects = reading.targets(item=item_type, limit=limit, root=root, timeout_ms=timeout)
		for record, obj in zip(payload["targets"], objects):
			record["ref"] = context.register(obj) if obj is not None else ""
		return payload

	return _dispatch(context, perform)


def navigate_reading(
	item: str,
	direction: str,
	steps: int,
	within: str | None,
	timeout_ms: int,
	expect: dict[str, Any] | None,
	context: Any,
) -> dict[str, Any]:
	"""Move a document's reading position, the way a quick nav key would.

	The move is a real change to the user's session — the screen reader speaks
	the new position — so it is an action, not an observation. It settles like
	one: the reply's ``changed``/``delta`` say what the screen did about it.
	"""
	from ..server import reading as rules

	item_type = rules.normalize_item(item)
	move = rules.normalize_direction(direction)
	if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= rules.MAX_STEPS:
		raise ValueError(f"'steps' must be an integer between 1 and {rules.MAX_STEPS}")
	if move == "last" and steps != 1:
		raise ValueError("'steps' must be 1 when 'direction' is 'last'")
	timeout = rules.normalize_timeout(timeout_ms)

	captured: dict[str, Any] = {}

	def perform() -> dict[str, Any]:
		def act() -> dict[str, Any]:
			from ..nvda_bridge import reading

			root = context.resolve(_validate_ref_argument(within, "within")) if within else None
			payload, obj = reading.navigate(
				item=item_type,
				direction=move,
				steps=steps,
				root=root,
				timeout_ms=timeout,
			)
			if obj is not None:
				payload["ref"] = context.register(obj)
			captured.update(payload)
			return payload

		return context.dispatch(act)

	context.settle(perform, expect=expect)
	return dict(captured)


def invoke(ref: str, action: str | None, expect: dict[str, Any] | None, context: Any) -> dict[str, Any]:
	from ..nvda_bridge import targets

	captured: dict[str, Any] = {}

	def perform() -> dict[str, Any]:
		def act() -> dict[str, Any]:
			return targets.invoke(context.resolve(ref), action)

		captured.update(context.dispatch(act))
		return captured

	context.settle(perform, expect=expect)
	return {
		"ref": ref,
		"invoked": True,
		"index": int(captured.get("index", -1)),
		"action_name": str(captured.get("actionName", "")),
		"available": list(captured.get("available", [])),
	}


def set_value(ref: str, value: str, expect: dict[str, Any] | None, context: Any) -> dict[str, Any]:
	"""Write a value, read the control back, and retry once when the write did not stick.

	The write used to stop at "the input was dispatched": a keyboard-mode write
	reported ``units: 17`` while the field stayed empty, and an agent that trusts
	``success`` would go on to read a screen that never changed. The read-back is
	the difference between "I asked" and "it happened", so it is reported on
	every call — ``verified`` when it matched, ``observed`` when it did not, and
	``uncertain`` on the result block so the caller cannot miss it.

	The retry exists because a control can accept a platform write without
	applying it: a browser's contenteditable exposes ``ValuePattern`` and
	ignores ``SetValue``, which reads as success right up until the value is read
	back. One keyboard attempt is cheap, and it is the only thing that moves
	such a control; if it also fails, that is reported rather than papered over.
	"""
	from ..nvda_bridge import targets

	captured: dict[str, Any] = {}

	def perform(force_keyboard: bool = False) -> dict[str, Any]:
		def act() -> dict[str, Any]:
			return targets.set_value(context.resolve(ref), value, force_keyboard=force_keyboard)

		captured.update(context.dispatch(act))
		return captured

	def read_observed() -> tuple[bool, str, str]:
		try:
			read_back = context.dispatch(lambda: targets.read_value(context.resolve(ref)))
		except Exception:  # noqa: BLE001 — a vanished control is "unverified", not a failure
			return False, "", "unavailable"
		seen_value = read_back.get("value")
		seen = "" if seen_value is None else str(seen_value)
		# Browsers keep the placeholder in the value pattern on an empty field, and
		# an emptied contenteditable holds "\n"; compare the text, not the framing.
		return seen.strip() == value.strip(), seen, str(read_back.get("source", ""))

	# What the control holds now, so a platform write that changes nothing can be
	# told apart from one that landed.
	_before_verified, before, _before_source = read_observed()

	outcome = context.settle(perform, expect=expect)
	verified, observed, value_source = read_observed()

	# Retry once with keystrokes only when the platform write changed nothing: a
	# control can expose ValuePattern and ignore SetValue, and typing is the only
	# thing that moves it. A write that *did* change something is reported as-is
	# rather than typed over.
	if not verified and str(captured.get("mode", "")) == "platform" and observed.strip() == before.strip():
		captured.clear()
		outcome = context.settle(lambda: perform(force_keyboard=True), expect=expect)
		verified, observed, value_source = read_observed()

	if not verified:
		setattr(outcome, "uncertain", True)

	return {
		"ref": ref,
		"mode": str(captured.get("mode", "keyboard")),
		"value": value,
		"previous": before,
		"units": int(captured.get("units", 0)),
		"verified": verified,
		"observed": "" if verified else observed,
		"value_source": value_source,
	}


def activate_window(
	title: str,
	handle: int,
	exact: bool,
	expect: dict[str, Any] | None,
	context: Any,
) -> dict[str, Any]:
	"""Bring a top-level window to the foreground, then settle on it.

	This is the verb the tool surface was missing: without it, every task that
	did not start in the foreground window needed a script outside the protocol.
	The window object is registered as a reference, so the caller can search
	inside it (`within`) or observe it without a second lookup.
	"""
	from ..nvda_bridge import windows

	if not title and not handle:
		raise ValueError("'activate_window' needs a title or a handle")
	if handle and (not isinstance(handle, int) or isinstance(handle, bool) or handle <= 0):
		raise ValueError("'handle' must be a positive integer window handle")

	# Fast path: if the window is already in front there is nothing to wait for.
	# Going through settle would spend its first-activity window (about 800 ms)
	# learning that a no-op was a no-op.
	if not handle:
		current_handle = windows.foreground_handle()
		current_title = windows.foreground_title()
		# A minimised window can still be the one Windows calls foreground, and
		# calling that "already in front" would leave it hidden.
		if windows.title_matches(current_title, title, exact) and not windows.is_minimized(
			current_handle,
		):
			ref = ""
			window_object = None
			try:
				window_object = context.dispatch(
					lambda: windows.object_for_handle(current_handle),
				)
			except Exception:  # noqa: BLE001 — a ref is a bonus, not the answer
				window_object = None
			if window_object is not None:
				try:
					ref = context.register(window_object)
				except Exception:  # noqa: BLE001
					ref = ""
			try:
				focus_handle, focus_title = context.dispatch(windows.focus_window)
			except Exception:  # noqa: BLE001 — "cannot tell" is an answer too
				focus_handle, focus_title = 0, ""
			return {
				"activated": True,
				"handle": current_handle,
				"title": current_title,
				"ref": ref,
				"previous_title": current_title,
				"method": "already-foreground",
				"restored": False,
				"waited_ms": 0.0,
				"matches": 1,
				"focus_in_window": (focus_handle == current_handle) if focus_handle else None,
				"focus_window": focus_title if focus_handle and focus_handle != current_handle else "",
			}

	captured: dict[str, Any] = {}

	def perform() -> dict[str, Any]:
		def act() -> dict[str, Any]:
			result = windows.activate(title=title or "", handle=handle, exact=exact)
			captured.update(result)
			return {key: value for key, value in result.items() if key != "object"}

		context.dispatch(act)
		return {key: value for key, value in captured.items() if key != "object"}

	context.settle(perform, expect=expect)

	window_object = captured.get("object")
	ref = ""
	if window_object is not None:
		try:
			ref = context.register(window_object)
		except Exception:  # noqa: BLE001 — a window without a ref is still activated
			ref = ""

	return {
		"activated": bool(captured.get("activated", False)),
		"handle": int(captured.get("handle", 0)),
		"title": str(captured.get("title", "")),
		"ref": ref,
		"previous_title": str(captured.get("previous_title", "")),
		"method": str(captured.get("method", "foreground")),
		"restored": bool(captured.get("restored", False)),
		"waited_ms": float(captured.get("waited_ms", 0.0)),
		"matches": int(captured.get("matches", 1)),
		"focus_in_window": captured.get("focus_in_window"),
		"focus_window": str(captured.get("focus_window", "")),
	}


def focus_object(ref: str, expect: dict[str, Any] | None, context: Any) -> dict[str, Any]:
	from ..nvda_bridge import targets

	def perform() -> dict[str, Any]:
		return context.dispatch(lambda: targets.set_focus(context.resolve(ref)))

	context.settle(perform, expect=expect)
	return {"ref": ref, "requested": True}


def press(
	keys: str,
	repeat: int,
	delay_ms: int,
	expect: dict[str, Any] | None,
	context: Any,
) -> dict[str, Any]:
	"""Send keys, then wait for the UI to settle.

	The keystroke is still the same primitive it always was; what changed is that
	the call no longer returns before its effect is visible.
	"""
	from .. import nvda_bridge
	from ..nvda_bridge import ui as nvda_ui

	captured: dict[str, Any] = {}

	def perform() -> dict[str, Any]:
		result = nvda_bridge.send_keyboard(
			gesture=keys,
			repeat=repeat,
			delay_ms=delay_ms,
			cancellation_event=getattr(context, "cancellation_event", None),
			main_thread_fn=lambda gesture: context.dispatch(
				lambda: nvda_ui.send_single_gesture(gesture)
			),
			timeout=getattr(context, "timeout", None) or 10.0,
		)
		captured.update(result)
		return result

	context.settle(perform, expect=expect)
	return {
		"requested": keys,
		"normalized_keys": str(captured.get("normalized_keys", keys)),
		"repeat": int(captured.get("repeat", repeat)),
		"delay_ms": int(captured.get("delay_ms", delay_ms)),
		"sent": bool(captured.get("sent", False)),
		"sent_count": int(captured.get("sent_count", 0)),
		"dispatched": bool(captured.get("dispatched", False)),
		"cancelled": bool(captured.get("cancelled", False)),
		"uncertain": bool(captured.get("uncertain", False)),
	}


def read(
	ref: str | None,
	scope: str,
	max_chars: int,
	context: Any,
) -> dict[str, Any]:
	from ..nvda_bridge import targets

	def perform() -> dict[str, Any]:
		import api

		obj = context.resolve(ref) if ref else api.getFocusObject()
		if obj is None:
			raise RuntimeError("No focused object to read from")
		return targets.read_text(obj, scope=scope, max_chars=max_chars)

	result = _dispatch(context, perform)
	return {"ref": ref, **result}


def navigate(
	direction: str,
	ref: str | None,
	expect: dict[str, Any] | None,
	context: Any,
) -> dict[str, Any]:
	from ..nvda_bridge import targets

	captured: dict[str, Any] = {}

	def perform() -> dict[str, Any]:
		def act() -> dict[str, Any]:
			import api

			obj = context.resolve(ref) if ref else api.getFocusObject()
			if obj is None:
				raise RuntimeError("No focused object to navigate from")
			target, focused = targets.navigate(obj, direction)
			captured["focused"] = focused
			captured["ref"] = context.register(target)
			return captured

		return context.dispatch(act)

	context.settle(perform, expect=expect)
	return {
		"direction": direction,
		"ref": captured.get("ref"),
		"focused": bool(captured.get("focused", False)),
	}


def scroll(
	direction: str,
	amount: str,
	ref: str | None,
	expect: dict[str, Any] | None,
	context: Any,
) -> dict[str, Any]:
	from ..nvda_bridge import targets

	captured: dict[str, Any] = {}

	def perform() -> dict[str, Any]:
		def act() -> dict[str, Any]:
			import api

			obj = context.resolve(ref) if ref else api.getFocusObject()
			if obj is None:
				raise RuntimeError("No focused object to scroll")
			return targets.scroll(obj, direction, amount)

		captured.update(context.dispatch(act))
		return captured

	context.settle(perform, expect=expect)
	return {
		"direction": direction,
		"amount": amount,
		"method": str(captured.get("method", "keyboard")),
		"key": str(captured.get("key", "")),
	}


def dismiss(expect: dict[str, Any] | None, context: Any) -> dict[str, Any]:
	"""Leave a dialog: Escape first, then a clearly non-affirmative button.

	This exists because the previous rule let an agent enter a modal and then
	forbade every tool from leaving it — a safety rule that only traps. It needs
	the narrow ``dialog_control`` permission and is allowed in modal and menu
	states.
	"""
	from ..nvda_bridge import actions
	from ..nvda_bridge import ui as nvda_ui

	steps: list[str] = []

	def press_escape() -> None:
		context.dispatch(lambda: nvda_ui.send_single_gesture("escape"))
		steps.append("pressed Escape")

	outcome = context.settle(press_escape, budget_ms=600)
	dialog = (outcome.projection or {}).get("dialog")

	if dialog:
		def activate_cancel() -> dict[str, Any]:
			def act() -> dict[str, Any]:
				active = actions.dialog_object()
				if active is None:
					return {}
				button = actions.cancel_dialog_button(active)
				if button is None:
					steps.append("no safe cancel button found; left dialog open")
					return {}
				return actions.do_action(button)

			result = context.dispatch(act)
			if result:
				steps.append(f"activated cancel button {result.get('actionName')!r}")
			return result

		# The fallback is deliberately limited to explicit negative/close labels;
		# if no such control exists, it reports the still-open dialog without
		# activating the affirmative default.
		outcome = context.settle(activate_cancel, budget_ms=800)
		dialog = (outcome.projection or {}).get("dialog")
	else:
		steps.append("no dialog was open")

	# The expectation is about the final state, whichever attempt produced it.
	from ..server.settle import evaluate_expect

	expectation = evaluate_expect(outcome.projection, expect)
	if expect:
		outcome.completed = expectation.satisfied
		outcome.expect_mismatch = expectation.mismatch
	if context.result is not None:
		context.result.settle = outcome

	return {
		"dismissed": dialog is None,
		"steps": steps,
		"dialog_name": str((dialog or {}).get("name") or ""),
	}


def click_at(x: int, y: int, button: str, expect: dict[str, Any] | None, context: Any) -> dict[str, Any]:
	from ..nvda_bridge import actions

	captured: dict[str, Any] = {}

	def perform() -> dict[str, Any]:
		captured.update(context.dispatch(lambda: actions.click_at(x, y, button)))
		return captured

	context.settle(perform, expect=expect)
	return {
		"x": int(captured.get("x", x)),
		"y": int(captured.get("y", y)),
		"button": button,
		"hit": captured.get("hit"),
		"cursor": list(captured.get("cursor") or []),
		"on_target": bool(captured.get("onTarget", True)),
	}


def wait_for_events(
	since: int | None,
	timeout_ms: int,
	kinds: list[str] | None,
	context: Any,
	limit: int = 50,
) -> dict[str, Any]:
	if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms < 0 or timeout_ms > 30000:
		raise ValueError("'timeout_ms' must be an integer between 0 and 30000")
	if since is None:
		event_version = getattr(context, "event_version", None)
		state_version = getattr(context, "state_version", None)
		if callable(event_version):
			since = int(event_version())
		elif callable(state_version):
			since = int(state_version())
		else:
			since = 0
	if not isinstance(since, int) or isinstance(since, bool) or since < 0:
		raise ValueError("'since' must be a non-negative integer, or omitted")
	result = context.wait_events(since=since, timeout_ms=timeout_ms, kinds=kinds, limit=limit)
	return {
		"from_version": int(result.get("from_version", since)),
		"to_version": int(result.get("to_version", since)),
		"events": list(result.get("events", [])),
		"truncated": bool(result.get("truncated", False)),
		"has_more": bool(result.get("has_more", False)),
		"timed_out": int(result.get("to_version", since)) <= since,
	}


def get_action_trace(trace_id: str, context: Any) -> dict[str, Any]:
	"""Read one retained action trace.

	Purely a store read: no NVDA call, no state change, and no dependency on the
	event resource being subscribed. A missing record is reported as
	``found: false`` with the retention window, because an expired trace is a
	normal consequence of bounded storage rather than a failure of the action
	being investigated.
	"""
	store = getattr(context, "trace_store", None)
	trace = None
	if store is not None:
		try:
			trace = store.get(trace_id)
		except Exception:  # noqa: BLE001 — a broken store must not look like a bad tool
			trace = None
	stats = {}
	if store is not None:
		try:
			stats = store.stats()
		except Exception:  # noqa: BLE001
			stats = {}
	window = {
		"max_traces": stats.get("max_traces"),
		"max_age_seconds": stats.get("max_age_seconds"),
		"retained_count": stats.get("count"),
	}
	if not isinstance(trace, dict):
		return {
			"trace_id": trace_id,
			"found": False,
			"schema_version": 1,
			"missing_reason": "expired_or_unknown" if store is not None else "not_stored",
			"retention": window,
		}
	return {
		"trace_id": trace_id,
		"found": True,
		"schema_version": int(trace.get("schema_version", 1)),
		"trace": trace,
		"retention": window,
	}

