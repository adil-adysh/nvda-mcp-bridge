"""NVDA main-thread queue adapter abstraction."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import logHandler  # type: ignore[reportMissingImports]
import queueHandler  # type: ignore[reportMissingImports]


class MainThreadQueue:
	"""Abstraction for queuing execution on NVDA main thread."""

	def queue(self, func: Callable[[], Any]) -> None:
		"""Queue a callable for main-thread execution."""
		raise NotImplementedError


class NVDAQueueAdapter(MainThreadQueue):
	"""Adapter that forwards work to NVDA's queueHandler."""

	def queue(self, func: Callable[[], Any]) -> None:
		try:
			queueHandler.queueFunction(queueHandler.eventQueue, func)
		except Exception:
			logHandler.log.error("Failed to queue function to NVDA main thread", exc_info=True)
			raise
