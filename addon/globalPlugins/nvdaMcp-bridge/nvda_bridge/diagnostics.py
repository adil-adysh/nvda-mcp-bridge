"""Diagnostic and monitoring utilities for NVDA Bridge.

Provides functions for health snapshots, log retrieval, and addon listing.
"""

import re
import time
from collections import deque
from typing import Any, Dict

import logHandler

from .validators import validate_bounded_int, validate_scope
from .serializers import serialize_exception
from .focus import get_current_focus, get_focus_lineage
from .objects import (
	get_navigator_object,
	inspect_app_module,
	walk_object_tree,
)
from .ui import get_window_title


def _addon_to_dict(addon: Any, running_names: set[str]) -> Dict[str, Any]:
	"""Normalize addon metadata for transport-safe JSON responses."""
	manifest = getattr(addon, "manifest", {}) or {}
	name = str(getattr(addon, "name", None) or manifest.get("name") or "")

	def _call_bool(attr_name: str) -> bool | None:
		attr = getattr(addon, attr_name, None)
		if callable(attr):
			try:
				return bool(attr())
			except Exception:
				return None
		return None

	is_running = name in running_names
	if not is_running:
		runtime_flag = _call_bool("isRunning")
		if runtime_flag is True:
			is_running = True

	return {
		"name": name,
		"summary": str(manifest.get("summary") or ""),
		"version": str(getattr(addon, "version", None) or manifest.get("version") or ""),
		"author": str(manifest.get("author") or ""),
		"url": str(manifest.get("url") or ""),
		"path": str(getattr(addon, "path", None) or ""),
		"isRunning": is_running,
		"isEnabled": _call_bool("isEnabled"),
		"isDisabled": _call_bool("isDisabled"),
		"isBlocked": _call_bool("isBlocked"),
		"isCompatible": _call_bool("isCompatible"),
		"requiresRestart": _call_bool("requiresRestart"),
		"minimumNVDAVersion": str(
			getattr(addon, "minimumNVDAVersion", None) or manifest.get("minimumNVDAVersion") or ""
		),
		"lastTestedNVDAVersion": str(
			getattr(addon, "lastTestedNVDAVersion", None) or manifest.get("lastTestedNVDAVersion") or ""
		),
	}


def list_loaded_addons(scope: str = "running") -> Dict[str, Any]:
	"""List addon metadata from NVDA addon handler with selectable scope.

	Args:
		scope: "running", "available", or "all"

	Returns:
		Dictionary with selected addon list and summary counts.

	Raises:
		ValueError: If scope is invalid
	"""
	normalized_scope = validate_scope(scope)

	import addonHandler

	running_addons = list(addonHandler.getRunningAddons())
	available_addons = list(addonHandler.getAvailableAddons())
	running_names = {
		str(getattr(addon, "name", None) or "") for addon in running_addons if getattr(addon, "name", None)
	}

	if normalized_scope == "running":
		selected = running_addons
	elif normalized_scope == "available":
		selected = available_addons
	else:
		by_name: dict[str, Any] = {}
		for addon in available_addons:
			name = str(getattr(addon, "name", None) or "")
			if name:
				by_name[name] = addon
		for addon in running_addons:
			name = str(getattr(addon, "name", None) or "")
			if name:
				by_name[name] = addon
		selected = list(by_name.values())

	addons_out = [_addon_to_dict(addon, running_names=running_names) for addon in selected]
	addons_out.sort(key=lambda item: item.get("name", "").lower())

	return {
		"scope": normalized_scope,
		"count": len(addons_out),
		"runningCount": len(running_addons),
		"availableCount": len(available_addons),
		"addons": addons_out,
	}


def _read_log_tail_optimized(log_file: str, max_lines: int, filter_level: str | None = None) -> list:
	"""Efficiently read the tail of a log file using seek from end.

	This is much faster and more memory-efficient than reading the entire file.
	Based on NVDA's own tail reading approach.

	Args:
		log_file: Path to log file
		max_lines: Maximum number of lines to read
		filter_level: Optional log level filter (e.g., "ERROR", "INFO")

	Returns:
		List of log lines (last N lines matching filter)
	"""
	import os

	# Compile regex for log level filtering if needed
	level_pattern = None
	if filter_level:
		# NVDA log format: "LEVEL - module.function (timestamp) - thread:"
		level_pattern = re.compile(rf"^{re.escape(filter_level)}\s+[-\(]", re.IGNORECASE)

	lines = []

	try:
		with open(log_file, "rb") as f:
			# Seek to end
			f.seek(0, os.SEEK_END)
			file_size = f.tell()

			if file_size == 0:
				return []

			# Estimate bytes needed (heuristic: ~150 bytes per line)
			# Add 50% buffer to account for filtering
			estimated_bytes = int(max_lines * 150 * 1.5)
			estimated_bytes = min(estimated_bytes, file_size)

			# Read from estimated position
			seek_pos = max(0, file_size - estimated_bytes)
			f.seek(seek_pos)

			# Read chunk
			chunk = f.read(estimated_bytes)

			# Decode with error handling
			text = chunk.decode("utf-8", errors="replace")

			# Split into lines
			all_lines = text.splitlines()

			# If we didn't start at file beginning, first line might be partial
			if seek_pos > 0 and all_lines:
				all_lines = all_lines[1:]

			# Apply filter and collect lines
			for line in all_lines:
				line = line.rstrip()
				if not line:
					continue

				if filter_level:
					if level_pattern and level_pattern.match(line):
						lines.append(line)
				else:
					lines.append(line)

			# Return last max_lines
			return lines[-max_lines:] if len(lines) > max_lines else lines

	except Exception as e:
		logHandler.log.error(f"Error in tail reading: {e}", exc_info=True)
		# Fallback to simple approach
		return _read_log_simple(log_file, max_lines, filter_level)


def _read_log_simple(log_file: str, max_lines: int, filter_level: str | None = None) -> list:
	"""Simple log reading approach - reads entire file line by line.

	Used as fallback or for small files.

	Args:
		log_file: Path to log file
		max_lines: Maximum lines to return
		filter_level: Optional log level filter

	Returns:
		List of log lines
	"""
	# Use deque with maxlen for automatic FIFO buffer
	lines_buffer = deque(maxlen=max_lines)

	level_pattern = None
	if filter_level:
		level_pattern = re.compile(rf"^{re.escape(filter_level)}\s+[-\(]", re.IGNORECASE)

	with open(log_file, "r", encoding="utf-8", errors="replace") as f:
		for line in f:
			line = line.rstrip()
			if not line:
				continue

			if filter_level:
				if level_pattern and level_pattern.match(line):
					lines_buffer.append(line)
			else:
				lines_buffer.append(line)

	return list(lines_buffer)


def get_logs(max_lines: int = 100, filter_level: str | None = None, use_tail: bool = True) -> Dict[str, Any]:
	"""Retrieve recent log entries from the NVDA log file.

	Uses NVDA's native APIs and efficient tail-reading for large files.

	Args:
		max_lines: Maximum number of log lines to return (default: 100, max: 10000)
		filter_level: Optional filter by log level (DEBUG, INFO, WARNING, ERROR, CRITICAL, DEBUGWARNING, IO)
		use_tail: If True, use optimized tail reading for large files (default: True)

	Returns:
		Dictionary with log lines and metadata:
		{
			"log_file": str,
			"total_lines": int,
			"max_lines": int,
			"filter_level": str | None,
			"lines": list[str],
			"file_size": int
		}

	Raises:
		RuntimeError: If log file cannot be read
	"""
	import globalVars
	import os

	try:
		# Validate max_lines and filter_level
		max_lines = validate_bounded_int(max_lines, "max_lines", 1, 10000)
		if filter_level:
			from .validators import validate_log_filter_level

			filter_level = validate_log_filter_level(filter_level)

		# Get log file path from NVDA's global args (canonical source)
		log_file = globalVars.appArgs.logFileName
		if not log_file:
			raise RuntimeError("Log file is not available (logging may be disabled or not initialized)")

		if not os.path.exists(log_file):
			raise RuntimeError(f"Log file not found: {log_file}")

		# Get file size for metadata
		file_size = os.path.getsize(log_file)

		logHandler.log.debug(
			f"get_logs: file={log_file}, size={file_size} bytes, "
			f"max_lines={max_lines}, filter_level={filter_level}, use_tail={use_tail}"
		)

		# When a level filter is active the tail heuristic is unreliable:
		# filtered lines may be very sparse (e.g. one ERROR per 1 000 lines)
		# so the estimated-byte window will miss most matching entries.
		# Use the full sequential scan for every filtered request.
		if use_tail and not filter_level:
			lines = _read_log_tail_optimized(log_file, max_lines, filter_level)
		else:
			lines = _read_log_simple(log_file, max_lines, filter_level)

		logHandler.log.debug(f"get_logs: returning {len(lines)} lines from {file_size} byte file")

		# `total_lines` was a misnomer — it counted returned lines, not all
		# lines in the file. Renamed to `returned_lines` for clarity.
		return {
			"log_file": log_file,
			"returned_lines": len(lines),
			"max_lines": max_lines,
			"filter_level": filter_level,
			"lines": lines,
			"file_size": file_size,
		}
	except Exception as e:
		logHandler.log.error(f"Failed to read logs: {e}", exc_info=True)
		raise RuntimeError(f"Get logs failed: {str(e)}")


def health_snapshot(
	include_logs: bool = True,
	log_lines: int = 30,
	include_tree: bool = False,
	tree_depth: int = 1,
	tree_max_children: int = 10,
	addon_scope: str = "running",
) -> Dict[str, Any]:
	"""Collect a one-call diagnostics snapshot for addon debugging.

	This function is resilient by design: each section is collected independently.
	Failures are returned under `errors` while successful sections are still included.

	Args:
		include_logs: Whether to include recent log entries
		log_lines: Number of log lines to include (1-500)
		include_tree: Whether to include focus object tree
		tree_depth: Maximum depth for object tree (0-4)
		tree_max_children: Maximum children per node (1-50)
		addon_scope: Addon listing scope ("running", "available", or "all")

	Returns:
		Dictionary with comprehensive system snapshot

	Raises:
		ValueError: If parameters are invalid
	"""
	log_lines = validate_bounded_int(log_lines, "log_lines", 1, 500)
	tree_depth = validate_bounded_int(tree_depth, "tree_depth", 0, 4)
	tree_max_children = validate_bounded_int(tree_max_children, "tree_max_children", 1, 50)
	addon_scope = validate_scope(addon_scope)

	snapshot: dict[str, Any] = {
		"timestamp": int(time.time()),
		"options": {
			"include_logs": bool(include_logs),
			"log_lines": int(log_lines),
			"include_tree": bool(include_tree),
			"tree_depth": int(tree_depth),
			"tree_max_children": int(tree_max_children),
			"addon_scope": str(addon_scope),
		},
		"errors": {},
	}

	def _collect(section: str, producer) -> None:
		try:
			snapshot[section] = producer()
		except Exception as e:
			snapshot["errors"][section] = serialize_exception(e)

	_collect("window", get_window_title)
	_collect("focus", get_current_focus)
	_collect("navigator", get_navigator_object)
	_collect("appModule", lambda: inspect_app_module(source="focus"))
	_collect("addons", lambda: list_loaded_addons(scope=addon_scope))
	_collect("focusLineage", lambda: get_focus_lineage(max_ancestors=10))

	if include_tree:
		_collect(
			"focusTree",
			lambda: walk_object_tree(
				target="focus",
				max_depth=tree_depth,
				max_children=tree_max_children,
			),
		)

	if include_logs:
		_collect("logs", lambda: get_logs(max_lines=log_lines, use_tail=True))

	snapshot["ok"] = len(snapshot["errors"]) == 0
	snapshot["errorCount"] = len(snapshot["errors"])

	return snapshot
