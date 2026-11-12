"""Best-effort redaction for the one secret the bridge knows for certain.

Native logs are not structured and cannot be perfectly redacted. The bridge
does know its own authentication token, so every log-tool response replaces
that exact value, plus obvious ``key=value`` credential assignments. This is a
safety net, not a promise that arbitrary document text or third-party secrets
are found.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "<redacted>"
_REDACTION_SECRETS: set[str] = set()
_LOADED = False

_ASSIGNMENT_RE = re.compile(
	r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|credential)\b"
	r"(\s*[:=]\s*)"
	r"(\"[^\"]{1,512}\"|'[^']{1,512}'|[^\s,;]{1,512})"
)


def register_secret(secret: str | None) -> None:
	if isinstance(secret, str) and len(secret) >= 8:
		_REDACTION_SECRETS.add(secret)


def load_bridge_secret_once() -> None:
	"""Load the bridge token lazily, never failing a log request over it."""
	global _LOADED
	if _LOADED:
		return
	_LOADED = True
	try:
		from ..security import get_or_create_bridge_token

		register_secret(get_or_create_bridge_token())
	except Exception:  # noqa: BLE001 — redaction must not break diagnostics
		pass


def redact_text(value: str) -> str:
	if not isinstance(value, str) or not value:
		return value
	redacted = value
	for secret in _REDACTION_SECRETS:
		if secret in redacted:
			redacted = redacted.replace(secret, REDACTED)
	redacted = _ASSIGNMENT_RE.sub(r"\1\2" + REDACTED, redacted)
	return redacted


def redact_value(value: Any) -> Any:
	"""Recursively redact strings in a JSON-shaped value."""
	if isinstance(value, str):
		return redact_text(value)
	if isinstance(value, list):
		return [redact_value(item) for item in value]
	if isinstance(value, dict):
		return {key: redact_value(item) for key, item in value.items()}
	return value
