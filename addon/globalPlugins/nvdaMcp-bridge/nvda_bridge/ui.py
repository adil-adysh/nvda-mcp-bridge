"""UI interaction utilities for NVDA Bridge.

Provides functions for keyboard input, window titles, and UI messages.
"""

import threading
import time
from typing import Any, Callable, Dict, Optional

import ui
import winUser
import logHandler

_input_lock = threading.Lock()


def send_keyboard(
	gesture: str,
	repeat: int = 1,
	delay_ms: int = 0,
	cancellation_event: Optional[threading.Event] = None,
	main_thread_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
	"""Send a keyboard gesture safely without blocking NVDA's main thread.

	Args:
		gesture: NVDA keyboard gesture name (for example: ``windows+d``, ``alt+tab``)
		repeat: Number of times to send the gesture (1..20)
		delay_ms: Delay between repeated sends in milliseconds (0..5000)
		cancellation_event: Optional event to cancel pending repetitions
		main_thread_fn: Optional callable to dispatch single gesture send to NVDA main thread

	Returns:
		Dictionary with send metadata including sent status and cancellation state.

	Raises:
		ValueError: If input is invalid or gesture cannot be resolved.
		TimeoutError: If input lock cannot be acquired within deadline.
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

	# Resolve gesture once to validate before acquiring input lock
	try:
		resolved_gesture = keyboardHandler.KeyboardInputGesture.fromName(normalized_gesture)
	except Exception as e:
		raise ValueError(f"Failed to resolve keyboard gesture '{normalized_gesture}': {e}")

	# Serialize keyboard actions to prevent race conditions or interleaving
	acquired = _input_lock.acquire(timeout=10.0)
	if not acquired:
		raise TimeoutError("Timeout waiting for keyboard input lock; another operation is active")

	sent_count = 0
	was_cancelled = False

	try:
		def _send_single():
			resolved_gesture.send()

		dispatcher = main_thread_fn if main_thread_fn is not None else _send_single

		for idx in range(repeat):
			if cancellation_event and cancellation_event.is_set():
				was_cancelled = True
				break

			dispatcher()
			sent_count += 1

			if idx < repeat - 1 and delay_ms > 0:
				# CRITICAL: Never sleep on NVDA's main thread
				if threading.current_thread() is threading.main_thread():
					logHandler.log.warning(
						"send_keyboard called with delay_ms on main thread; omitting sleep to keep NVDA responsive"
					)
				else:
					if cancellation_event is not None:
						if cancellation_event.wait(timeout=delay_ms / 1000.0):
							was_cancelled = True
							break
					else:
						time.sleep(delay_ms / 1000.0)
	except Exception as e:
		if sent_count == 0:
			raise ValueError(f"Failed to send keyboard gesture '{normalized_gesture}': {e}")
		logHandler.log.error(f"Error during repeated keyboard send: {e}", exc_info=True)
	finally:
		_input_lock.release()

	return {
		"gesture": normalized_gesture,
		"repeat": repeat,
		"delay_ms": delay_ms,
		"sent": sent_count > 0,
		"sent_count": sent_count,
		"cancelled": was_cancelled,
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
