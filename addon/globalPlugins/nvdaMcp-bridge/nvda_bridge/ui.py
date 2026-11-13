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


class KeyboardExecutionError(RuntimeError):
	"""Base exception for keyboard execution failures with structured execution tracking."""

	#: Wire error code for this failure, so the transport does not have to guess.
	protocol_code: int = 5003

	def __init__(
		self,
		message: str,
		gesture: str,
		repeat: int,
		sent_count: int,
		cancelled: bool = False,
		timed_out: bool = False,
		uncertain: bool = False,
		original_error: Optional[Exception] = None,
	):
		super().__init__(message)
		self.gesture = gesture
		self.repeat = repeat
		self.sent_count = sent_count
		self.cancelled = cancelled
		self.timed_out = timed_out
		self.uncertain = uncertain
		self.original_error = original_error

	def to_details(self) -> Dict[str, Any]:
		return {
			"gesture": self.gesture,
			"repeat": self.repeat,
			"sent_count": self.sent_count,
			"cancelled": self.cancelled,
			"timed_out": self.timed_out,
			"uncertain": self.uncertain,
		}


class KeyboardTimeoutError(KeyboardExecutionError, TimeoutError):
	"""Raised when a keyboard sequence or gesture dispatch times out."""

	protocol_code = 5004

	def __init__(
		self,
		message: str,
		gesture: str,
		repeat: int,
		sent_count: int,
		uncertain: bool = False,
		original_error: Optional[Exception] = None,
	):
		super().__init__(
			message=message,
			gesture=gesture,
			repeat=repeat,
			sent_count=sent_count,
			cancelled=False,
			timed_out=True,
			uncertain=uncertain,
			original_error=original_error,
		)


class KeyboardCancelledError(KeyboardExecutionError):
	"""Raised when a keyboard sequence is cancelled before completion."""

	protocol_code = 4204

	def __init__(
		self,
		message: str,
		gesture: str,
		repeat: int,
		sent_count: int,
		uncertain: bool = False,
		original_error: Optional[Exception] = None,
	):
		super().__init__(
			message=message,
			gesture=gesture,
			repeat=repeat,
			sent_count=sent_count,
			cancelled=True,
			timed_out=False,
			uncertain=uncertain,
			original_error=original_error,
		)


def resolve_gesture(gesture: str) -> Any:
	"""Validate and resolve an NVDA keyboard gesture name.

	Args:
		gesture: NVDA keyboard gesture name (for example: ``windows+d``, ``control+a``).

	Returns:
		Resolved NVDA KeyboardInputGesture instance.

	Raises:
		ValueError: If gesture name is invalid or cannot be resolved by NVDA.
	"""
	if not isinstance(gesture, str) or not gesture.strip():
		raise ValueError("'gesture' must be a non-empty string")

	normalized_gesture = normalize_gesture_name(gesture)
	import keyboardHandler

	try:
		return keyboardHandler.KeyboardInputGesture.fromName(normalized_gesture)
	except Exception as e:
		raise ValueError(f"Failed to resolve keyboard gesture '{normalized_gesture}': {e}")


#: Modifier spellings that callers write but NVDA does not accept.
#: NVDA gesture names use ``control`` and ``windows``; ``ctrl``/``win``/``esc``
#: are the conventional spellings and rejecting them costs a round trip on the
#: most common verbs in the vocabulary.
_MODIFIER_ALIASES = {
	"ctrl": "control",
	"ctl": "control",
	"win": "windows",
	"super": "windows",
	"meta": "windows",
	"esc": "escape",
	"del": "delete",
	"ins": "insert",
	"pgup": "pageup",
	"pgdn": "pagedown",
	"return": "enter",
}

_KEY_ALIASES = {
	"esc": "escape",
	"pgup": "pageup",
	"pgdn": "pagedown",
	"return": "enter",
	"del": "delete",
}


def normalize_gesture_name(gesture: str) -> str:
	"""Normalise a gesture name to NVDA's spelling.

	``ctrl+a`` becomes ``control+a``; ``shift+esc`` becomes ``shift+escape``.
	Unknown tokens are passed through untouched so NVDA can still reject them.
	"""
	parts = [part.strip().lower() for part in gesture.strip().split("+")]
	if not parts or not parts[-1]:
		return gesture.strip()

	*modifiers, key = parts
	modifiers = [_MODIFIER_ALIASES.get(modifier, modifier) for modifier in modifiers]
	key = _KEY_ALIASES.get(key, key)
	return "+".join([*modifiers, key])


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
	try:
		script = resolved.script
	except Exception:
		script = None
	if script is None:
		resolved.send()
		return
	import inputCore
	import queueHandler

	def _run_script() -> None:
		try:
			inputCore.manager.executeGesture(resolved)
		except inputCore.NoInputGestureAction:
			resolved.send()
		except Exception:
			logHandler.log.error("NVDA Bridge: executing gesture failed", exc_info=True)

	try:
		queueHandler.queueFunction(queueHandler.eventQueue, _run_script)
	except Exception:
		logHandler.log.warning("NVDA Bridge: queuing gesture failed; injecting keys")
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
		KeyboardTimeoutError: If input lock cannot be acquired or sequence deadline is exceeded.
		KeyboardExecutionError: If dispatch fails on the main thread.
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

	normalized_gesture = normalize_gesture_name(gesture)
	resolved_gesture = resolve_gesture(normalized_gesture)

	deadline = time.monotonic() + timeout

	# Acquire sequence input lock to serialize keyboard sequences
	wait_time = max(0.01, deadline - time.monotonic())
	acquired = _input_lock.acquire(timeout=wait_time)
	if not acquired:
		raise KeyboardTimeoutError(
			message="Timeout waiting for keyboard input lock; another operation is active",
			gesture=normalized_gesture,
			repeat=repeat,
			sent_count=0,
			uncertain=False,
		)

	sent_count = 0
	was_cancelled = False

	try:
		for idx in range(repeat):
			# 1. Check cancellation before gesture
			if cancellation_event and cancellation_event.is_set():
				was_cancelled = True
				break

			# 2. Check sequence deadline
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				raise KeyboardTimeoutError(
					message=f"Keyboard sequence timed out before dispatching gesture {idx + 1} of {repeat}",
					gesture=normalized_gesture,
					repeat=repeat,
					sent_count=sent_count,
					uncertain=False,
				)

			# 3. Dispatch single gesture primitive (does NOT acquire _input_lock)
			if main_thread_fn is not None:
				try:
					main_thread_fn(resolved_gesture)
				except Exception as fn_err:
					raise KeyboardExecutionError(
						message=f"Keyboard dispatch failed on gesture {idx + 1} of {repeat}: {fn_err}",
						gesture=normalized_gesture,
						repeat=repeat,
						sent_count=sent_count,
						uncertain=False,
						original_error=fn_err,
					) from fn_err
			elif threading.current_thread() is threading.main_thread():
				try:
					send_single_gesture(resolved_gesture)
				except Exception as direct_err:
					raise KeyboardExecutionError(
						message=f"Keyboard dispatch failed on gesture {idx + 1} of {repeat}: {direct_err}",
						gesture=normalized_gesture,
						repeat=repeat,
						sent_count=sent_count,
						uncertain=False,
						original_error=direct_err,
					) from direct_err
			else:
				# Background thread without custom dispatcher: queue to NVDA event queue
				import queueHandler

				done_evt = threading.Event()
				dispatch_exception: Optional[Exception] = None

				def _dispatch_worker():
					nonlocal dispatch_exception
					try:
						send_single_gesture(resolved_gesture)
					except Exception as cb_err:
						dispatch_exception = cb_err
					finally:
						done_evt.set()

				try:
					queueHandler.queueFunction(queueHandler.eventQueue, _dispatch_worker)
				except Exception as q_err:
					raise RuntimeError(
						f"Failed to queue keyboard gesture to NVDA main thread: {q_err}"
					) from q_err

				time_for_gesture = max(0.01, deadline - time.monotonic())
				if not done_evt.wait(timeout=time_for_gesture):
					raise KeyboardTimeoutError(
						message=f"Timed out waiting for main thread execution of gesture {idx + 1} of {repeat}",
						gesture=normalized_gesture,
						repeat=repeat,
						sent_count=sent_count,
						uncertain=True,
					)

				if dispatch_exception is not None:
					raise KeyboardExecutionError(
						message=f"Main thread exception during gesture {idx + 1} of {repeat}: {dispatch_exception}",
						gesture=normalized_gesture,
						repeat=repeat,
						sent_count=sent_count,
						uncertain=False,
						original_error=dispatch_exception,
					) from dispatch_exception

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
						raise KeyboardTimeoutError(
							message=f"Keyboard sequence timed out during pacing after dispatching {sent_count} of {repeat} gestures",
							gesture=normalized_gesture,
							repeat=repeat,
							sent_count=sent_count,
							uncertain=False,
						)

	finally:
		_input_lock.release()

	return {
		"gesture": normalized_gesture,
		"repeat": repeat,
		"delay_ms": delay_ms,
		"sent": sent_count > 0,
		"sent_count": sent_count,
		"cancelled": was_cancelled,
		# ``dispatched`` means the input was posted. It deliberately does not say
		# "completed": a keystroke is posted long before the UI has reacted, and
		# claiming otherwise is what made this tool report success over a stale
		# screen. Settling is the caller's (the coordinator's) job.
		"dispatched": not was_cancelled and sent_count == repeat,
		"uncertain": False,
	}


def get_window_title() -> Dict[str, Any]:
	"""Get the title of the current foreground window.

	Returns:
		Dictionary with window title and handle

	``handle`` is 0 with an empty title when Windows reports no foreground
	window (for example between closing the last window and the desktop taking
	focus). That is a real, reportable state — a caller asking "what is in front"
	should get an answer, not a bridge fault (5303).
	"""
	# Get foreground window
	hwnd = winUser.getForegroundWindow()
	if not hwnd:
		return {"title": "", "handle": 0, "className": ""}

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
