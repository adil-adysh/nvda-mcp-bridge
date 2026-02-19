"""
NVDA Bridge Module
Provides thin wrappers around NVDA APIs for the HTTP bridge.

This module contains pure NVDA API calls with no protocol logic.
Functions are called by nvda_http_bridge.py and return JSON-serializable dicts.

CRITICAL THREAD SAFETY REQUIREMENT:
All functions in this module MUST be called from NVDA's main thread only.
They are automatically queued to the main thread via _execute_on_main_thread()
in nvda_http_bridge.py. Never call these functions directly from background threads.

Keep this module focused on:
- NVDA API access
- Error handling
- JSON-compatible return values

Do NOT add:
- Protocol implementation
- MCP schemas
- Tool definitions
- HTTP routing
"""

import api
import speech
import winUser
import textInfos
import ui
import logHandler
from typing import Dict, Any


def get_current_focus() -> Dict[str, Any]:
	"""
	Get information about the currently focused object.

	Returns:
		Dictionary with focus information

	Raises:
		RuntimeError: If no focus object is available or operation fails
	"""
	focus = api.getFocusObject()
	if not focus:
		raise RuntimeError("No focus object available")

	# Safely get attributes with defaults
	return {
		"name": getattr(focus, "name", None) or "",
		"role": getattr(focus, "role", None),
		"roleText": getattr(focus, "roleText", None) or "",
		"value": getattr(focus, "value", None) or "",
		"description": getattr(focus, "description", None) or "",
		"states": str(getattr(focus, "states", "")),
		"windowClassName": getattr(focus, "windowClassName", None) or "",
		"windowControlID": getattr(focus, "windowControlID", 0),
	}


def read_current_line() -> Dict[str, Any]:
	"""
	Read the current line from the focused object.

	Returns:
		Dictionary with line text and position

	Raises:
		RuntimeError: If no focus object or cannot create text info
	"""
	focus = api.getFocusObject()
	if not focus:
		raise RuntimeError("No focus object available")

	# Try to get text info
	try:
		info = focus.makeTextInfo(textInfos.POSITION_CARET)
		if not info:
			raise RuntimeError("Cannot create text info for current position")

		info.expand(textInfos.UNIT_LINE)
		text = info.text

		# Get line number if available
		line_num = None
		try:
			bookmark = getattr(info, "bookmark", None)
			if bookmark:
				line_num = getattr(bookmark, "startOffset", None)
		except Exception:
			pass  # Line number not available

		return {
			"text": text or "",
			"lineNumber": line_num,
		}
	except (NotImplementedError, AttributeError):
		# Object doesn't support text info - return fallback
		return {
			"text": getattr(focus, "name", None) or getattr(focus, "value", None) or "",
			"lineNumber": None,
			"note": "Object doesn't support text navigation",
		}


def speak_text(text: str) -> Dict[str, Any]:
	"""
	Speak the given text using NVDA's speech system.

	Args:
		text: Text to speak

	Returns:
		Dictionary with spoken text

	Raises:
		ValueError: If text is invalid or too long
	"""
	# Validate input
	if not text:
		raise ValueError("No text provided")

	if not isinstance(text, str):
		raise ValueError("Text must be a string")

	# Limit text length to prevent issues
	if len(text) > 10000:
		raise ValueError("Text too long (max 10000 characters)")

	speech.speakText(text)
	return {"spoken": text}


def get_window_title() -> Dict[str, Any]:
	"""
	Get the title of the current foreground window.

	Returns:
		Dictionary with window title and handle

	Raises:
		RuntimeError: If no foreground window is available
	"""
	# Get foreground window
	hwnd = winUser.getForegroundWindow()
	if not hwnd:
		raise RuntimeError("No foreground window")

	# Get window text with error handling
	try:
		title = winUser.getWindowText(hwnd)
	except Exception as e:
		logHandler.log.warning(f"Error getting window text: {e}")
		title = ""

	# Get class name with error handling
	try:
		className = winUser.getClassName(hwnd)
	except Exception as e:
		logHandler.log.warning(f"Error getting class name: {e}")
		className = ""

	return {
		"title": title or "",
		"handle": hwnd,
		"className": className or "",
	}


def move_focus(direction: str) -> Dict[str, Any]:
	"""
	Move focus to next or previous object.

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
		raise RuntimeError("No focus object available")

	# Determine navigation direction
	if direction.lower() == "next":
		# Move to next object
		next_obj = getattr(focus, "next", None)
		if not next_obj:
			raise RuntimeError("No next object available")

		next_obj.setFocus()
		return {
			"direction": "next",
			"newFocus": {
				"name": getattr(next_obj, "name", None) or "",
				"role": getattr(next_obj, "roleText", None) or "",
			},
		}

	elif direction.lower() == "previous":
		# Move to previous object
		prev_obj = getattr(focus, "previous", None)
		if not prev_obj:
			raise RuntimeError("No previous object available")

		prev_obj.setFocus()
		return {
			"direction": "previous",
			"newFocus": {
				"name": getattr(prev_obj, "name", None) or "",
				"role": getattr(prev_obj, "roleText", None) or "",
			},
		}
	else:
		raise ValueError(f"Invalid direction: {direction}. Use 'next' or 'previous'")


def get_navigator_object() -> Dict[str, Any]:
	"""
	Get information about the current navigator object.

	Returns:
		Dictionary with navigator object information

	Raises:
		RuntimeError: If no navigator object is available
	"""
	nav = api.getNavigatorObject()
	if not nav:
		raise RuntimeError("No navigator object available")

	return {
		"name": getattr(nav, "name", None) or "",
		"role": getattr(nav, "roleText", None) or "",
		"value": getattr(nav, "value", None) or "",
		"description": getattr(nav, "description", None) or "",
	}


def get_desktop_object() -> Dict[str, Any]:
	"""
	Get information about the desktop object.

	Returns:
		Dictionary with desktop object information

	Raises:
		RuntimeError: If no desktop object is available
	"""
	desktop = api.getDesktopObject()
	if not desktop:
		raise RuntimeError("No desktop object available")

	return {
		"name": getattr(desktop, "name", None) or "",
		"role": getattr(desktop, "roleText", None) or "",
		"childCount": getattr(desktop, "childCount", 0),
	}


def show_message(message: str) -> Dict[str, Any]:
	"""
	Show a UI message to the user.

	Args:
		message: Message to display

	Returns:
		Dictionary with displayed message

	Raises:
		ValueError: If message is invalid or too long
	"""
	# Validate input
	if not message:
		raise ValueError("No message provided")

	if not isinstance(message, str):
		raise ValueError("Message must be a string")

	# Limit message length
	if len(message) > 1000:
		raise ValueError("Message too long (max 1000 characters)")

	ui.message(message)
	return {"message": message}


def restart_nvda(disable_addons: bool = False, debug_logging: bool = False) -> Dict[str, Any]:
	"""
	Restart NVDA with optional parameters.

	Args:
		disable_addons: Whether to restart with addons disabled
		debug_logging: Whether to enable debug logging on restart

	Returns:
		Dictionary with restart confirmation

	Raises:
		RuntimeError: If restart fails
	"""
	import core

	try:
		logHandler.log.info(
			f"Restarting NVDA (disable_addons={disable_addons}, debug_logging={debug_logging})"
		)

		# Trigger NVDA restart
		core.restart(disableAddons=disable_addons, debugLogging=debug_logging)

		# If we reach here, restart was initiated
		return {
			"restarting": True,
			"disable_addons": disable_addons,
			"debug_logging": debug_logging,
		}
	except Exception as e:
		logHandler.log.error(f"Failed to restart NVDA: {e}", exc_info=True)
		raise RuntimeError(f"Restart failed: {str(e)}")


def reload_plugins() -> Dict[str, Any]:
	"""
	Reload all global plugins and app modules from the scratchpad.

	This reloads:
	- All global plugins (including those in scratchpad)
	- All app modules (including those in scratchpad)
	- Clears the dynamic NVDAObject class cache

	Useful for development when testing changes to plugins without restarting NVDA.

	Returns:
		Dictionary with reload confirmation and counts

	Raises:
		RuntimeError: If reload fails
	"""
	import globalPluginHandler
	import appModuleHandler
	from NVDAObjects import NVDAObject

	try:
		logHandler.log.info("Reloading all plugins from scratchpad")

		# Reload app modules
		appModuleHandler.reloadAppModules()

		# Reload global plugins
		globalPluginHandler.reloadGlobalPlugins()

		# Clear dynamic class cache
		NVDAObject.clearDynamicClassCache()

		logHandler.log.info("Successfully reloaded all plugins")

		return {
			"reloaded": True,
			"app_modules": True,
			"global_plugins": True,
			"cache_cleared": True,
		}
	except Exception as e:
		logHandler.log.error(f"Failed to reload plugins: {e}", exc_info=True)
		raise RuntimeError(f"Plugin reload failed: {str(e)}")


def get_logs(max_lines: int = 100, filter_level: str = None, use_tail: bool = True) -> Dict[str, Any]:
	"""
	Retrieve recent log entries from the NVDA log file.

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
		# Validate max_lines
		if max_lines < 1:
			max_lines = 100
		elif max_lines > 10000:
			max_lines = 10000

		# Validate filter_level using NVDA's Logger class levels
		valid_levels = {
			"DEBUG": logHandler.Logger.DEBUG,
			"INFO": logHandler.Logger.INFO,
			"WARNING": logHandler.Logger.WARNING,
			"ERROR": logHandler.Logger.ERROR,
			"CRITICAL": logHandler.Logger.CRITICAL,
			"DEBUGWARNING": logHandler.Logger.DEBUGWARNING,
			"IO": logHandler.Logger.IO,
		}

		if filter_level:
			filter_level = filter_level.upper()
			if filter_level not in valid_levels:
				raise ValueError(
					f"Invalid log level: {filter_level}. Must be one of {list(valid_levels.keys())}"
				)

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

		# Read lines using optimized tail algorithm
		if use_tail:
			lines = _read_log_tail_optimized(log_file, max_lines, filter_level)
		else:
			# Fallback: simple read-all approach
			lines = _read_log_simple(log_file, max_lines, filter_level)

		logHandler.log.debug(f"get_logs: returning {len(lines)} lines from {file_size} byte file")

		return {
			"log_file": log_file,
			"total_lines": len(lines),
			"max_lines": max_lines,
			"filter_level": filter_level,
			"lines": lines,
			"file_size": file_size,
		}
	except Exception as e:
		logHandler.log.error(f"Failed to read logs: {e}", exc_info=True)
		raise RuntimeError(f"Get logs failed: {str(e)}")


def _read_log_tail_optimized(log_file: str, max_lines: int, filter_level: str = None) -> list:
	"""
	Efficiently read the tail of a log file using seek from end.

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
	import re

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


def _read_log_simple(log_file: str, max_lines: int, filter_level: str = None) -> list:
	"""
	Simple log reading approach - reads entire file line by line.

	Used as fallback or for small files.

	Args:
		log_file: Path to log file
		max_lines: Maximum lines to return
		filter_level: Optional log level filter

	Returns:
		List of log lines
	"""
	import re
	from collections import deque

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
