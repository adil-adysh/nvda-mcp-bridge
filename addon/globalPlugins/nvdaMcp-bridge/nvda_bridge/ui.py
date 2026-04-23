"""UI interaction utilities for NVDA Bridge.

Provides functions for keyboard input, window titles, and UI messages.
"""

import time
from typing import Any, Dict

import ui
import winUser
import logHandler


def send_keyboard(gesture: str, repeat: int = 1, delay_ms: int = 0) -> Dict[str, Any]:
	"""Send a keyboard gesture using NVDA's keyboard input gesture API.

	Args:
		gesture: NVDA keyboard gesture name (for example: ``windows+d``, ``alt+tab``)
		repeat: Number of times to send the gesture (1..20)
		delay_ms: Delay between repeated sends in milliseconds (0..5000)

	Returns:
		Dictionary with send metadata.

	Raises:
		ValueError: If input is invalid or gesture cannot be resolved.
	"""
	if not isinstance(gesture, str) or not gesture.strip():
		raise ValueError("'gesture' must be a non-empty string")

	if not isinstance(repeat, int):
		raise ValueError("'repeat' must be an integer")
	if repeat < 1 or repeat > 20:
		raise ValueError("'repeat' must be between 1 and 20")

	if not isinstance(delay_ms, int):
		raise ValueError("'delay_ms' must be an integer")
	if delay_ms < 0 or delay_ms > 5000:
		raise ValueError("'delay_ms' must be between 0 and 5000")

	normalized_gesture = gesture.strip()

	import keyboardHandler

	try:
		for idx in range(repeat):
			keyboardHandler.KeyboardInputGesture.fromName(normalized_gesture).send()
			if delay_ms > 0 and idx < repeat - 1:
				time.sleep(delay_ms / 1000.0)
	except Exception as e:
		raise ValueError(f"Failed to send keyboard gesture '{normalized_gesture}': {e}")

	return {
		"gesture": normalized_gesture,
		"repeat": repeat,
		"delay_ms": delay_ms,
		"sent": True,
	}


def get_window_title() -> Dict[str, Any]:
	"""Get the title of the current foreground window.

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

	# hwnd is a ctypes HANDLE (pointer-sized integer wrapper on 64-bit
	# Windows). It is NOT a plain Python int, and json.dumps will raise
	# TypeError unless we convert explicitly.
	return {
		"title": title or "",
		"handle": int(hwnd),
		"className": className or "",
	}


def show_message(message: str) -> Dict[str, Any]:
	"""Show a UI message to the user.

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
