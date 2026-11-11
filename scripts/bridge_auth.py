"""Resolve the bridge token the same way the add-on writes it.

Shared by the scripts in this directory so none of them carries a hardcoded
secret that stops working as soon as the add-on generates a real token.
"""

from __future__ import annotations

import os
from pathlib import Path

TOKEN_HEADER = "X-NVDA-Bridge-Token"


def resolve_token(explicit: str | None = None) -> str:
	"""Return the bridge token.

	Order: explicit argument, ``NVDA_BRIDGE_TOKEN``, ``NVDA_TOKEN_PATH``, then the
	per-installation ``token.key`` under the user's application data directory.
	"""
	if explicit:
		return explicit.strip()

	env_token = os.environ.get("NVDA_BRIDGE_TOKEN", "").strip()
	if env_token:
		return env_token

	env_path = os.environ.get("NVDA_TOKEN_PATH", "").strip()
	if env_path and Path(env_path).is_file():
		return Path(env_path).read_text(encoding="utf-8").strip()

	appdata = os.environ.get("APPDATA")
	candidates = []
	if appdata:
		candidates.append(Path(appdata) / "nvda" / "nvda-mcp-bridge" / "token.key")
	candidates.append(Path.home() / ".nvda-mcp-bridge" / "token.key")

	for candidate in candidates:
		if candidate.is_file():
			return candidate.read_text(encoding="utf-8").strip()

	raise SystemExit(
		"No bridge token found. Pass it explicitly, set NVDA_BRIDGE_TOKEN, or run the "
		"add-on so it writes token.key next to the NVDA configuration.",
	)


def auth_headers(explicit: str | None = None) -> dict[str, str]:
	"""Return request headers carrying the bridge token."""
	return {TOKEN_HEADER: resolve_token(explicit), "Content-Type": "application/json"}
