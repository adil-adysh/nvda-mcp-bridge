"""Main-thread executor for safe NVDA API calls with full lifecycle tracking.

All NVDA API calls must run on NVDA's main thread.
"""

from __future__ import annotations

import enum
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import logHandler  # type: ignore[reportMissingImports]

from .queue_adapter import MainThreadQueue, NVDAQueueAdapter


class OperationState(str, enum.Enum):
	CREATED = "created"
	QUEUED = "queued"
	RUNNING = "running"
	COMPLETED = "completed"
	CANCELLED = "cancelled"
	FAILED = "failed"
	TIMED_OUT = "timed_out"


@dataclass
class MainThreadExecutor:
	"""Executes callables on NVDA's main thread with cancellation and lifecycle tracking."""

	default_timeout: float = 10.0
	queue_adapter: MainThreadQueue = NVDAQueueAdapter()

	def execute(
		self,
		func: Callable[..., Any],
		*args: Any,
		timeout: float | None = None,
		cancellation_event: threading.Event | None = None,
		**kwargs: Any,
	) -> Any:
		"""Queue `func` to NVDA main thread and wait for completion with cancellation protection."""
		effective_timeout = self.default_timeout if timeout is None else timeout
		result: Any = None
		error: Exception | None = None
		done = threading.Event()
		cancelled = threading.Event()

		state_lock = threading.Lock()
		current_state = OperationState.CREATED

		def _set_state(new_state: OperationState) -> None:
			nonlocal current_state
			with state_lock:
				current_state = new_state

		def _get_state() -> OperationState:
			with state_lock:
				return current_state

		def _run() -> None:
			nonlocal result
			nonlocal error

			# Prevent executing abandoned work if caller already timed out or cancelled
			if cancelled.is_set() or (cancellation_event and cancellation_event.is_set()):
				_set_state(OperationState.CANCELLED)
				done.set()
				return

			_set_state(OperationState.RUNNING)
			try:
				result = func(*args, **kwargs)
				_set_state(OperationState.COMPLETED)
			except Exception as execution_error:
				error = execution_error
				_set_state(OperationState.FAILED)
			finally:
				done.set()

		_set_state(OperationState.QUEUED)
		try:
			self.queue_adapter.queue(_run)
		except Exception as queue_error:
			_set_state(OperationState.FAILED)
			logHandler.log.error("Failed to queue function to NVDA main thread", exc_info=True)
			raise RuntimeError(f"Failed to queue to main thread: {queue_error}") from queue_error

		if not done.wait(timeout=effective_timeout):
			cancelled.set()
			with state_lock:
				if current_state == OperationState.QUEUED:
					current_state = OperationState.TIMED_OUT
			raise TimeoutError(
				f"Operation timed out after {effective_timeout} seconds (state was {current_state.value})"
			)

		if cancelled.is_set() or (cancellation_event and cancellation_event.is_set()):
			raise RuntimeError("Operation was cancelled before execution completed")

		if error is not None:
			raise error

		return result
