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


def resolve_gesture(gesture: str) -> Any:
	"""Validate and resolve an NVDA keyboard gesture name.

	Args:
		gesture: NVDA keyboard gesture name (for example: ``windows+d``, ``alt+tab``).

	Returns:
		Resolved NVDA KeyboardInputGesture instance.

	Raises:
		ValueError: If gesture name is invalid or cannot be resolved by NVDA.
	"""
	if not isinstance(gesture, str) or not gesture.strip():
		raise ValueError("'gesture' must be a non-empty string")

	normalized_gesture = gesture.strip()
	import keyboardHandler

	try:
		return keyboardHandler.KeyboardInputGesture.fromName(normalized_gesture)
	except Exception as e:
		raise ValueError(f"Failed to resolve keyboard gesture '{normalized_gesture}': {e}")


def send_single_gesture(gesture_or_resolved: Any) -> None:
	"""Send a single resolved keyboard gesture without acquiring the sequence lock.

	Must be called in NVDA's main thread context. Does not perform locking or delays.

	Args:
		gesture_or_resolved: KeyboardInputGesture instance or gesture name string.
	"""
	if isinstance(gesture_or_resolved, str):
		resolved = resolve_gesture(gesture_or_resolved)
	else:
		resolved = gesture_or_resolved
	resolved.send()


def send_keyboard(
	gesture: str,
	repeat: int = 1,
	delay_ms: int = 0,
	cancellation_event: Optional[threading.Event] = None,
	main_thread_fn: Optional[Callable[[Any], Any]] = None,
	timeout: float = 10.0,
) -> Dict[str, Any]:
	"""Coordinate and send a keyboard sequence safely without blocking NVDA's main thread.

	The sequence coordinator owns the `_input_lock`. Single-gesture dispatches
	must never reacquire `_input_lock`, eliminating deadlock.

	Args:
		gesture: NVDA keyboard gesture name (for example: ``windows+d``, ``alt+tab``)
		repeat: Number of times to send the gesture (1..20)
		delay_ms: Delay between repeated sends in milliseconds (0..5000)
		cancellation_event: Optional event to cancel pending repetitions
		main_thread_fn: Optional callable to dispatch a single resolved gesture to NVDA main thread
		timeout: Maximum duration in seconds for the entire sequence operation

	Returns:
		Dictionary with send metadata including sent status, sent_count, and cancellation state.

	Raises:
		ValueError: If input is invalid or gesture cannot be resolved.
		TimeoutError: If input lock cannot be acquired or sequence deadline is exceeded.
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
	resolved_gesture = resolve_gesture(normalized_gesture)

	deadline = time.monotonic() + timeout

	# Acquire sequence input lock to serialize keyboard sequences
	wait_time = max(0.01, deadline - time.monotonic())
	acquired = _input_lock.acquire(timeout=wait_time)
	if not acquired:
		raise TimeoutError("Timeout waiting for keyboard input lock; another operation is active")

	sent_count = 0
	was_cancelled = False

	try:
		for idx in range(repeat):
			# 1. Check cancellation before gesture
			if cancellation_event and cancellation_event.is_set():
				was_cancelled = True
				break

			# 2. Check sequence deadline
			if time.monotonic() >= deadline:
				raise TimeoutError(
					f"Keyboard sequence timed out after dispatching {sent_count} of {repeat} gestures"
				)

			# 3. Dispatch single gesture primitive (does NOT acquire _input_lock)
			if main_thread_fn is not None:
				try:
					main_thread_fn(resolved_gesture)
				except TypeError:
					main_thread_fn()
			elif threading.current_thread() is threading.main_thread():
				send_single_gesture(resolved_gesture)
			else:
				# Background thread without dispatcher: try queueHandler or direct
				try:
					import queueHandler
					done_evt = threading.Event()
					queueHandler.queueFunction(
						queueHandler.eventQueue,
						lambda: (send_single_gesture(resolved_gesture), done_evt.set()),
					)
					done_evt.wait(timeout=max(0.1, deadline - time.monotonic()))
				except Exception:
					send_single_gesture(resolved_gesture)

			sent_count += 1

			# 4. Handle inter-gesture delay
			if idx < repeat - 1 and delay_ms > 0:
				if threading.current_thread() is threading.main_thread():
					logHandler.log.warning(
						"send_keyboard called with delay_ms on NVDA main thread; omitting sleep to keep UI responsive"
					)
				else:
					delay_secs = delay_ms / 1000.0
					remaining_time = max(0.0, deadline - time.monotonic())
					actual_wait = min(delay_secs, remaining_time)
					if cancellation_event is not None:
						if cancellation_event.wait(timeout=actual_wait):
							was_cancelled = True
							break
					else:
						time.sleep(actual_wait)

					if time.monotonic() >= deadline and idx < repeat - 1:
						raise TimeoutError(
							f"Keyboard sequence timed out during pacing after dispatching {sent_count} of {repeat} gestures"
						)

	except TimeoutError:
		raise
	except Exception as e:
		if sent_count == 0:
			raise ValueError(f"Failed to send keyboard gesture '{normalized_gesture}': {e}")
		logHandler.log.error(
			f"Error during repeated keyboard send after {sent_count} gestures: {e}", exc_info=True
		)
		raise RuntimeError(
			f"Keyboard sequence error after {sent_count} of {repeat} gestures: {e}"
		) from e
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
