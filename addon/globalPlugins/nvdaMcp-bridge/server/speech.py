"""Bounded, opt-in observation of text sent through NVDA's speech pipeline."""

from __future__ import annotations

import collections
import importlib
import secrets
import threading
import time
from typing import Any, Callable, Iterable

MAX_SPEECH_TEXT_CHARS = 1024
MAX_SPEECH_EVENTS = 256
MAX_PAGE_LIMIT = 256
MAX_WAIT_MS = 30_000
WAIT_RECHECK_SECONDS = 0.25


class SpeechAccessDenied(PermissionError):
	"""A speech page became unavailable before it could be released."""
	protocol_code = 4003


class SpeechJournalChanged(RuntimeError):
	"""The bounded speech journal reset while a page was being delivered."""
	protocol_code = 4205


def is_secure_desktop_at_startup(focus_obj: Any, secure_desktop_check: Callable[[], Any]) -> bool:
	"""Combine NVDA's desktop predicate with its user-instance sleep sentinel.

	Unknown desktop state is unsafe: speech observation remains unavailable until
	NVDA can establish that its process is on the ordinary desktop.
	"""
	try:
		actual_secure = bool(secure_desktop_check())
		full_sleep = getattr(type(focus_obj), "SLEEP_FULL", None)
		focus_secure = full_sleep is not None and getattr(focus_obj, "sleepMode", None) == full_sleep
		return actual_secure or focus_secure
	except Exception:  # noqa: BLE001 — uncertain desktop state fails closed
		return True


def extract_speech_text(sequence: Iterable[Any], limit: int = MAX_SPEECH_TEXT_CHARS) -> tuple[str, bool]:
	"""Join string chunks from an NVDA speech sequence and bound the result.

	Speech command instances are deliberately ignored. The return value is the
	retained text and whether any text was omitted by the character limit.
	"""
	max_chars = max(0, min(MAX_SPEECH_TEXT_CHARS, int(limit)))
	parts: list[str] = []
	length = 0
	truncated = False
	for item in sequence:
		if not isinstance(item, str) or not item:
			continue
		remaining = max_chars - length
		if len(item) > remaining:
			if remaining > 0:
				parts.append(item[:remaining])
			length += remaining
			truncated = True
			break
		parts.append(item)
		length += len(item)
	return "".join(parts), truncated


class SpeechJournal:
	"""Thread-safe memory-only ring buffer for speech and speech controls."""

	def __init__(self, capacity: int = MAX_SPEECH_EVENTS, max_text_chars: int = MAX_SPEECH_TEXT_CHARS) -> None:
		self._capacity = max(1, min(MAX_SPEECH_EVENTS, int(capacity)))
		self._max_text_chars = max(0, min(MAX_SPEECH_TEXT_CHARS, int(max_text_chars)))
		self._events: collections.deque[dict[str, Any]] = collections.deque(maxlen=self._capacity)
		self._condition = threading.Condition()
		self._version = 0
		self._dropped = 0
		self._journal_id = secrets.token_hex(8)
		self._last_at_ms = 0
		self._reset_reason: str | None = None

	@property
	def version(self) -> int:
		with self._condition:
			return self._version

	@property
	def journal_id(self) -> str:
		with self._condition:
			return self._journal_id

	def _append(self, event: dict[str, Any], expected_journal_id: str | None = None) -> int | None:
		with self._condition:
			if expected_journal_id is not None and expected_journal_id != self._journal_id:
				return None
			self._version += 1
			at_ms = int(time.monotonic() * 1000)
			# Keep timestamps strictly ordered even when two events land in one ms.
			self._last_at_ms = max(at_ms, self._last_at_ms + 1)
			event["at_ms"] = self._last_at_ms
			event["version"] = self._version
			if len(self._events) == self._capacity:
				self._dropped += 1
			self._events.append(event)
			self._condition.notify_all()
			return self._version

	def record_speech(
		self,
		text: str,
		phase: str,
		text_truncated: bool = False,
		*,
		expected_journal_id: str | None = None,
	) -> int | None:
		"""Record non-empty text only; returns its speech-journal version."""
		if not isinstance(text, str) or not text:
			return None
		truncated = bool(text_truncated or len(text) > self._max_text_chars)
		bounded_text = text[:self._max_text_chars]
		if not bounded_text:
			return None
		return self._append({
			"kind": "speech",
			"phase": str(phase),
			"text": bounded_text,
			"text_truncated": truncated,
		}, expected_journal_id)

	def record_control(
		self,
		kind: str,
		paused: bool | None = None,
		*,
		expected_journal_id: str | None = None,
	) -> int | None:
		"""Record a speech lifecycle control without retaining transcript text."""
		event: dict[str, Any] = {"kind": str(kind)}
		if paused is not None:
			event["paused"] = bool(paused)
		return self._append(event, expected_journal_id)

	def reset(self, reason: str = "reset") -> str:
		"""Clear retained speech and begin a new cursor generation."""
		with self._condition:
			self._events.clear()
			self._version = 0
			self._dropped = 0
			self._journal_id = secrets.token_hex(8)
			self._reset_reason = str(reason or "reset")
			self._condition.notify_all()
			return self._journal_id

	def snapshot(self, since: int = 0, limit: int = 20, journal_id: str | None = None) -> dict[str, Any]:
		"""Return an oldest-first page and explicit cursor recovery metadata."""
		requested_since = max(0, int(since))
		page_limit = max(1, min(MAX_PAGE_LIMIT, int(limit)))
		with self._condition:
			current = self._version
			current_id = self._journal_id
			events = list(self._events)
			dropped = self._dropped
			reset_reason = self._reset_reason
			oldest = events[0]["version"] if events else 0
			if journal_id is not None and journal_id != current_id:
				return self._mismatch_page(requested_since, current_id, current, oldest, dropped)

			expired = bool(oldest and requested_since + 1 < oldest)
			if requested_since > current:
				return self._future_page(requested_since, current_id, current, oldest, dropped)
			if expired:
				cursor_status = "stale"
			else:
				cursor_status = "ok"
			eligible = [event for event in events if event["version"] > requested_since]
			page = eligible[:page_limit]
			has_more = len(eligible) > len(page)
			to_version = page[-1]["version"] if page else requested_since
			payload: dict[str, Any] = {
				"journal_id": current_id,
				"from_version": requested_since,
				"to_version": to_version,
				"earliest_available_version": oldest,
				"dropped": dropped,
				"events": [dict(event) for event in page],
				"expired": expired,
				"resync_required": expired,
				"resync_reason": "cursor_expired" if expired else None,
				"resync_from": ({"journal_id": current_id, "version": oldest - 1} if expired else None),
				"history_complete": not expired,
				"truncated": expired,
				"has_more": has_more,
				"cursor_status": cursor_status,
				"timed_out": False,
				"cancelled": False,
			}
			if reset_reason is not None:
				payload["reset_reason"] = reset_reason
			return payload

	@staticmethod
	def _future_page(since: int, journal_id: str, current: int, oldest: int, dropped: int) -> dict[str, Any]:
		page = SpeechJournal._mismatch_page(since, journal_id, current, oldest, dropped)
		page.update({"to_version": current, "resync_reason": "future_cursor", "cursor_status": "future_cursor"})
		return page

	@staticmethod
	def _mismatch_page(since: int, journal_id: str, current: int, oldest: int, dropped: int) -> dict[str, Any]:
		return {
			"journal_id": journal_id,
			"from_version": since,
			"to_version": since,
			"earliest_available_version": oldest,
			"dropped": dropped,
			"events": [],
			"expired": False,
			"resync_required": True,
			"resync_reason": "journal_mismatch",
			"resync_from": {"journal_id": journal_id, "version": current},
			"history_complete": False,
			"truncated": False,
			"has_more": False,
			"cursor_status": "journal_mismatch",
			"timed_out": False,
			"cancelled": False,
		}

	def wait(
		self,
		since: int = 0,
		timeout_ms: int = 0,
		limit: int = 20,
		journal_id: str | None = None,
		cancellation_event: Any = None,
	) -> dict[str, Any]:
		"""Wait for a newer event, timeout, cancellation, or generation reset."""
		timeout = max(0, min(MAX_WAIT_MS, int(timeout_ms)))
		requested_since = max(0, int(since))
		if timeout == 0:
			return self.snapshot(requested_since, limit, journal_id)
		with self._condition:
			start_id = self._journal_id
			if journal_id is not None and journal_id != start_id:
				return self.snapshot(requested_since, limit, journal_id)
			deadline = time.monotonic() + timeout / 1000.0
			cancelled = False
			while self._version <= requested_since and self._journal_id == start_id:
				if cancellation_event is not None and cancellation_event.is_set():
					cancelled = True
					break
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					break
				self._condition.wait(timeout=min(remaining, WAIT_RECHECK_SECONDS))
			reset = self._journal_id != start_id
			current = self._version
		payload = self.snapshot(requested_since, limit, start_id if reset or journal_id is None else journal_id)
		payload["cancelled"] = cancelled
		payload["timed_out"] = not cancelled and not reset and current <= requested_since and not payload["resync_required"]
		return payload


def guarded_speech_wait(
	journal: SpeechJournal,
	access_check: Callable[[], tuple[bool, str | None]],
	*,
	since: int,
	timeout_ms: int,
	limit: int,
	journal_id: str | None = None,
	cancellation_event: Any = None,
) -> dict[str, Any]:
	"""Wait for speech and refuse pages whose authorization or generation changed."""
	try:
		allowed, reason = access_check()
	except Exception:  # noqa: BLE001 — a broken access check fails closed
		allowed, reason = False, "Speech observation safety state is unavailable"
	if not allowed:
		raise SpeechAccessDenied(reason or "Speech observation is unavailable")

	start_generation = journal.journal_id
	page = journal.wait(
		since=since,
		timeout_ms=timeout_ms,
		limit=limit,
		journal_id=journal_id if journal_id is not None else start_generation,
		cancellation_event=cancellation_event,
	)
	try:
		allowed, reason = access_check()
	except Exception:  # noqa: BLE001 — a broken access check fails closed
		allowed, reason = False, "Speech observation safety state is unavailable"
	if not allowed:
		raise SpeechAccessDenied(reason or "Speech observation is unavailable")
	if journal.journal_id != start_generation or page.get("journal_id") != start_generation:
		raise SpeechJournalChanged("Speech journal changed while waiting; retry from the current cursor")
	return page


class SpeechMonitor:
	"""Register NVDA speech hooks and append only authorized, safe observations."""

	def __init__(self, journal: SpeechJournal, is_enabled, initially_secure: bool = False) -> None:
		self._journal = journal
		self._is_enabled = is_enabled
		self._lock = threading.RLock()
		self._secure_desktop = bool(initially_secure)
		self._protected_focus = False
		self._registered: list[tuple[Any, Any]] = []
		self._started = False
		self._text_phase: str | None = None

	@property
	def is_started(self) -> bool:
		with self._lock:
			return self._started

	@property
	def is_protected_focus(self) -> bool:
		with self._lock:
			return self._protected_focus

	@property
	def is_secure_desktop(self) -> bool:
		with self._lock:
			return self._secure_desktop

	def set_protected_focus(self, obj: Any) -> None:
		"""Cache only protected state on NVDA's main thread, never during speech."""
		try:
			if obj is None:
				protected = True
			else:
				try:
					protected = bool(obj.isProtected)
				except AttributeError:
					states = getattr(obj, "states", None)
					if states is None:
						protected = False
					else:
						import controlTypes
						protected = controlTypes.State.PROTECTED in states
		except Exception:  # noqa: BLE001 — uncertain focus state suppresses capture
			protected = True
		with self._lock:
			became_protected = protected and not self._protected_focus
			self._protected_focus = protected
		if became_protected:
			self._journal.reset("protected_focus")

	def set_secure_desktop(self, is_secure: bool) -> None:
		with self._lock:
			secure = bool(is_secure)
			became_secure = secure and not self._secure_desktop
			self._secure_desktop = secure
		if became_secure:
			self._journal.reset("secure_desktop")

	@staticmethod
	def _load_module(name: str) -> Any:
		try:
			return importlib.import_module(name)
		except Exception:  # noqa: BLE001 — NVDA API may vary by supported version
			return None

	def _register(self, point: Any, callback: Any) -> bool:
		register = getattr(point, "register", None)
		if not callable(register):
			return False
		# Track before calling: an extension point may add the callback and then
		# raise, in which case start() still has to unregister it.
		self._registered.append((point, callback))
		register(callback)
		return True

	def _unregister_all(self) -> None:
		for point, callback in reversed(self._registered):
			try:
				point.unregister(callback)
			except Exception:  # noqa: BLE001 — cleanup must continue for all hooks
				pass
		self._registered.clear()

	def start(self) -> bool:
		"""Register one text hook and available control/security hooks."""
		with self._lock:
			if self._started:
				return True
			self._unregister_all()
			synth_module = self._load_module("synthDriverHandler")
			text_point = getattr(synth_module, "pre_synthSpeak", None) if synth_module else None
			self._text_phase = "about_to_synthesize"
			if text_point is None:
				extensions = self._load_module("speech.extensions")
				text_point = getattr(extensions, "pre_speechQueued", None) if extensions else None
				self._text_phase = "queued"
				if text_point is None:
					return False
			else:
				extensions = self._load_module("speech.extensions")

			secure_module = self._load_module("winAPI.secureDesktop")
			secure_point = getattr(secure_module, "post_secureDesktopStateChange", None) if secure_module else None
			if secure_point is None:
				# Capture cannot be safe when desktop transitions cannot be observed.
				return False
			points = [
				(text_point, self._on_speech),
				(getattr(extensions, "speechCanceled", None) if extensions else None, self._on_speech_cancelled),
				(getattr(extensions, "post_speechPaused", None) if extensions else None, self._on_speech_paused),
				(secure_point, self._on_secure_desktop_change),
			]
			try:
				for point, callback in points:
					if point is not None and not self._register(point, callback):
						raise RuntimeError("NVDA extension point does not support registration")
			except Exception:  # noqa: BLE001 — never leave a partial set of hooks behind
				self._unregister_all()
				return False
			self._started = True
			return True

	def stop(self) -> None:
		with self._lock:
			self._unregister_all()
			self._started = False

	def _capture_allowed(self) -> bool:
		try:
			if not self._is_enabled():
				return False
		except Exception:  # noqa: BLE001 — permission lookup failure is fail-closed
			return False
		with self._lock:
			return not self._secure_desktop and not self._protected_focus

	def _on_speech(self, speechSequence=(), **kwargs) -> None:
		journal_id = self._journal.journal_id
		if not self._capture_allowed():
			return
		try:
			text, truncated = extract_speech_text(speechSequence or ())
			if text:
				self._journal.record_speech(
					text,
					self._text_phase or "queued",
					truncated,
					expected_journal_id=journal_id,
				)
		except Exception:  # noqa: BLE001 — speech hooks must not disturb synthesis
			return

	def _on_speech_cancelled(self, *args, **kwargs) -> None:
		journal_id = self._journal.journal_id
		if self._capture_allowed():
			self._journal.record_control("speech_canceled", expected_journal_id=journal_id)

	def _on_speech_paused(self, switch=False, **kwargs) -> None:
		journal_id = self._journal.journal_id
		if self._capture_allowed():
			paused = bool(switch)
			self._journal.record_control(
				"speech_paused" if paused else "speech_resumed",
				paused=paused,
				expected_journal_id=journal_id,
			)

	def _on_secure_desktop_change(self, isSecureDesktop=False, **kwargs) -> None:
		self.set_secure_desktop(bool(isSecureDesktop))
