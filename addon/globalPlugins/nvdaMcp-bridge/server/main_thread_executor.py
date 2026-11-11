"""
Main-thread executor for safe NVDA API calls.

All NVDA API calls must run on NVDA's main thread.
"""

import threading
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

import logHandler  # type: ignore[reportMissingImports]

from .queue_adapter import MainThreadQueue, NVDAQueueAdapter


@dataclass
class MainThreadExecutor:
	"""Executes callables on NVDA's main thread and propagates exceptions."""

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

		def _run() -> None:
			nonlocal result
			nonlocal error
			# Prevent executing abandoned work if caller already timed out or cancelled
			if cancelled.is_set() or (cancellation_event and cancellation_event.is_set()):
				done.set()
				return
			try:
				result = func(*args, **kwargs)
			except Exception as execution_error:
				error = execution_error
			finally:
				done.set()

		try:
			self.queue_adapter.queue(_run)
		except Exception as queue_error:
			logHandler.log.error("Failed to queue function to NVDA main thread", exc_info=True)
			raise RuntimeError(f"Failed to queue to main thread: {queue_error}") from queue_error

		if not done.wait(timeout=effective_timeout):
			cancelled.set()
			raise TimeoutError(f"Operation timed out after {effective_timeout} seconds")

		if cancelled.is_set() or (cancellation_event and cancellation_event.is_set()):
			raise RuntimeError("Operation was cancelled before execution completed")

		if error is not None:
			raise error

		return result
