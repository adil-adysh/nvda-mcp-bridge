"""Security, authentication, and permission management for NVDA Bridge.

Provides:
- Secure per-installation token generation and Windows file ACL hardening
- Developer mode authorization lifecycle for arbitrary Python execution
- Explicit capability-based permission checking at the bridge boundary
"""

from __future__ import annotations

import hmac
import os
import secrets
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import logHandler

from ..core.base import ToolMetadata

# Permission capability identifiers
PERMISSION_READ_ONLY = "read_only"
PERMISSION_INPUT_CONTROL = "input_control"
PERMISSION_SPEECH_OUTPUT = "speech_output"
PERMISSION_DIAGNOSTICS = "diagnostics"
PERMISSION_NVDA_LIFECYCLE = "nvda_lifecycle"
PERMISSION_DEVELOPER = "developer"

ALL_PERMISSIONS = (
	PERMISSION_READ_ONLY,
	PERMISSION_INPUT_CONTROL,
	PERMISSION_SPEECH_OUTPUT,
	PERMISSION_DIAGNOSTICS,
	PERMISSION_NVDA_LIFECYCLE,
	PERMISSION_DEVELOPER,
)

INSECURE_DEFAULT_TOKEN = "nvda-bridge-secret-change-me"


def resolve_token_file_path(custom_dir: Optional[str | Path] = None) -> Path:
	"""Determine the appropriate Windows user path for the bridge authentication token."""
	if custom_dir is not None:
		return Path(custom_dir) / "token.key"

	# Try NVDA config path if running inside NVDA
	try:
		import globalVars  # type: ignore[reportMissingImports]

		config_path = getattr(getattr(globalVars, "appArgs", None), "configPath", None)
		if config_path:
			return Path(config_path) / "nvda-mcp-bridge" / "token.key"
	except Exception:
		pass

	# Try %APPDATA%\nvda\nvda-mcp-bridge
	appdata = os.environ.get("APPDATA")
	if appdata:
		return Path(appdata) / "nvda" / "nvda-mcp-bridge" / "token.key"

	# Try %LOCALAPPDATA%\nvda-mcp-bridge
	localappdata = os.environ.get("LOCALAPPDATA")
	if localappdata:
		return Path(localappdata) / "nvda-mcp-bridge" / "token.key"

	# Fallback to user home directory
	return Path.home() / ".nvda-mcp-bridge" / "token.key"


def _harden_file_permissions(file_path: Path) -> None:
	"""Apply restrictive Windows ACLs to the token file so only the current user can access it."""
	try:
		if os.name == "nt":
			# Use icacls to remove inherited permissions and grant full access only to current user
			username = os.environ.get("USERNAME")
			if username:
				cmd = [
					"icacls.exe",
					str(file_path),
					"/inheritance:r",
					"/grant:r",
					f"{username}:(R,W)",
				]
				subprocess.run(
					cmd,
					stdout=subprocess.DEVNULL,
					stderr=subprocess.DEVNULL,
					check=False,
					timeout=5,
				)
		else:
			file_path.chmod(0o600)
	except Exception as err:
		logHandler.log.debug("Failed to set strict ACL on token file: %s", err)


def get_or_create_bridge_token(custom_dir: Optional[str | Path] = None) -> str:
	"""Obtain the bridge authentication token.

	Resolution order:
	1. NVDA_BRIDGE_TOKEN environment variable (fails closed if insecure default without explicit opt-in).
	2. Installation-specific token file in Windows user profile.
	3. Cryptographically generates a fresh 256-bit token and persists it with user-only ACLs.
	"""
	env_token = os.environ.get("NVDA_BRIDGE_TOKEN", "").strip()
	if env_token:
		if env_token == INSECURE_DEFAULT_TOKEN:
			allow_insecure = os.environ.get("ALLOW_INSECURE_DEFAULT_TOKEN", "").strip().lower() in (
				"1",
				"true",
				"yes",
			)
			if not allow_insecure:
				logHandler.log.error(
					"Refusing to use insecure default token in production. "
					"Set a strong NVDA_BRIDGE_TOKEN or unset it to auto-generate a secure token."
				)
				raise ValueError(
					"Insecure default token rejected. Unset NVDA_BRIDGE_TOKEN to auto-generate a secure token."
				)
			logHandler.log.warning(
				"Using insecure legacy default bridge token because ALLOW_INSECURE_DEFAULT_TOKEN is set."
			)
		return env_token

	token_path = resolve_token_file_path(custom_dir)

	if token_path.is_file():
		try:
			existing = token_path.read_text(encoding="utf-8").strip()
			if len(existing) >= 16:
				return existing
		except Exception as err:
			logHandler.log.warning("Failed to read existing token file %s: %s", token_path, err)

	# Generate a cryptographically secure 256-bit URL-safe token
	token = secrets.token_urlsafe(32)

	try:
		token_path.parent.mkdir(parents=True, exist_ok=True)
		token_path.write_text(token, encoding="utf-8")
		_harden_file_permissions(token_path)
		logHandler.log.info("Generated new secure NVDA bridge authentication token at %s", token_path)
	except Exception as err:
		logHandler.log.error("Failed to persist bridge token to %s: %s", token_path, err)

	return token


def constant_time_compare(token_a: str, token_b: str) -> bool:
	"""Perform constant-time comparison to prevent timing attacks."""
	if not token_a or not token_b:
		return False
	try:
		return hmac.compare_digest(token_a.encode("utf-8"), token_b.encode("utf-8"))
	except Exception:
		return False


class DeveloperModeManager:
	"""Manages the developer mode authorization lifecycle for arbitrary Python execution."""

	def __init__(self, initial_enabled: bool = False):
		self._enabled = bool(initial_enabled)

	@property
	def is_enabled(self) -> bool:
		"""Return whether developer mode is currently active."""
		return self._enabled

	def enable(self) -> None:
		"""Enable developer mode with audit logging."""
		self._enabled = True
		logHandler.log.warning(
			"SECURITY AUDIT: Developer mode enabled. Arbitrary Python execution is now permitted."
		)

	def disable(self) -> None:
		"""Disable developer mode."""
		self._enabled = False
		logHandler.log.info("SECURITY AUDIT: Developer mode disabled.")

	def reset(self) -> None:
		"""Reset developer mode to the default disabled state (fail-closed)."""
		self._enabled = False


def check_action_permissions(
	action_name: str,
	metadata: Optional[Any],
	nvda_status_snapshot: Dict[str, bool],
	dev_mode_enabled: bool,
	tool_category: Optional[str] = None,
) -> Tuple[bool, int, str]:
	"""Enforce explicit security and capability permissions at the bridge boundary.

	Returns:
		Tuple of (allowed: bool, error_code: int, error_message: str)
	"""
	is_secure_screen = bool(nvda_status_snapshot.get("secure_screen", False))
	is_modal = bool(nvda_status_snapshot.get("modal_blocking", False))
	is_menu = bool(nvda_status_snapshot.get("menu_open", False))

	# Derive permissions and allowed states from metadata or category fallback
	if metadata is not None:
		required_permission = getattr(metadata, "required_permission", PERMISSION_READ_ONLY)
		allowed_states = getattr(metadata, "allowed_nvda_states", ("normal",))
	else:
		category = tool_category or ""
		if action_name == "execute_python" or category == "developer":
			required_permission = PERMISSION_DEVELOPER
			allowed_states = ("normal",)
		elif action_name in ("restart_nvda", "reload_plugins") or category == "system":
			required_permission = PERMISSION_NVDA_LIFECYCLE
			allowed_states = ("normal",)
		elif category in ("diagnostics",):
			required_permission = PERMISSION_DIAGNOSTICS
			allowed_states = ("normal", "secure_desktop")
		else:
			required_permission = PERMISSION_READ_ONLY
			allowed_states = ("normal",)

	# 1. Secure screen restrictions — strictly forbid arbitrary execution and state changes
	if is_secure_screen:
		if required_permission == PERMISSION_DEVELOPER or action_name == "execute_python":
			return (
				False,
				4201,
				"Arbitrary Python execution is strictly forbidden while on Windows secure desktop",
			)
		if "secure_desktop" not in allowed_states:
			return (
				False,
				4201,
				f"Action '{action_name}' is blocked while NVDA is on Windows secure desktop",
			)

	# 2. Developer mode requirement — execute_python requires explicit authorization
	if required_permission == PERMISSION_DEVELOPER or action_name == "execute_python":
		if not dev_mode_enabled:
			return (
				False,
				4003,
				f"Action '{action_name}' requires developer mode to be explicitly enabled",
			)

	# 3. Modal dialog blocking
	if is_modal and "modal" not in allowed_states:
		return (
			False,
			4202,
			f"Action '{action_name}' is blocked by active modal NVDA dialog",
		)

	# 4. NVDA menu open blocking
	if is_menu and "menu" not in allowed_states:
		return (
			False,
			4203,
			f"Action '{action_name}' is blocked while an NVDA menu is open",
		)

	# 5. NVDA lifecycle operations authorization check
	if required_permission == PERMISSION_NVDA_LIFECYCLE:
		if is_secure_screen:
			return (
				False,
				4201,
				f"Lifecycle action '{action_name}' is blocked on secure desktop",
			)

	return (True, 0, "")
