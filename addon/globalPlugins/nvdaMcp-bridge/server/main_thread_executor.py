"""
Main-thread executor for safe NVDA API calls.

All NVDA API calls must run on NVDA's main thread.
"""

import threading
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, cast

import logHandler  # type: ignore[reportMissingImports]
import queueHandler  # type: ignore[reportMissingImports]


@dataclass
class MainThreadExecutor:
	"""Executes callables on NVDA's main thread and propagates exceptions."""

	default_timeout: float = 10.0

	def execute(
		self,
		func: Callable[..., Any],
		*args: Any,
		timeout: float | None = None,
		**kwargs: Any,
	) -> Any:
		"""Queue `func` to NVDA main thread and wait for completion."""
		effective_timeout = self.default_timeout if timeout is None else timeout
		result: Any = None
		error: Exception | None = None
		done = threading.Event()

		def _run() -> None:
			nonlocal result
			nonlocal error
			try:
				result = func(*args, **kwargs)
			except Exception as execution_error:
				error = execution_error
			finally:
				done.set()

		try:
			queue_module = cast(Any, queueHandler)
			queue_module.queueFunction(queue_module.eventQueue, _run)
		except Exception as queue_error:
			# Use a distinct name to avoid shadowing (and then deleting) the
			# outer `error` closure variable that _run() writes to.
			log_module = cast(Any, logHandler)
			log_module.log.error("Failed to queue function to NVDA main thread", exc_info=True)
			raise RuntimeError(f"Failed to queue to main thread: {queue_error}") from queue_error

		if not done.wait(timeout=effective_timeout):
			raise TimeoutError(f"Operation timed out after {effective_timeout} seconds")

		if error is not None:
			raise error

		return result
