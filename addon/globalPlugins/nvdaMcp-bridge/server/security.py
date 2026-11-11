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
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import logHandler

from ..core.base import ToolMetadata

# Permission capability identifiers
PERMISSION_READ_ONLY = "read_only"
PERMISSION_INPUT_CONTROL = "input_control"
PERMISSION_SPEECH_OUTPUT = "speech_output"
PERMISSION_DIAGNOSTICS = "diagnostics"
PERMISSION_NVDA_LIFECYCLE = "nvda_lifecycle"
PERMISSION_DEVELOPER = "developer"
#: Narrow capability to leave a dialog. Without it, a rule that forbids input
#: inside modals only traps an agent that has already entered one.
PERMISSION_DIALOG_CONTROL = "dialog_control"

ALL_PERMISSIONS = frozenset({
	PERMISSION_READ_ONLY,
	PERMISSION_INPUT_CONTROL,
	PERMISSION_SPEECH_OUTPUT,
	PERMISSION_DIAGNOSTICS,
	PERMISSION_NVDA_LIFECYCLE,
	PERMISSION_DEVELOPER,
	PERMISSION_DIALOG_CONTROL,
})

DEFAULT_PERMISSIONS = frozenset({
	PERMISSION_READ_ONLY,
	PERMISSION_SPEECH_OUTPUT,
})

INSECURE_DEFAULT_TOKEN = "nvda-bridge-secret-change-me"


def resolve_token_file_path(custom_dir: Optional[str | Path] = None) -> Path:
	"""Determine the appropriate Windows user path for the bridge authentication token."""
	if custom_dir is not None:
		return Path(custom_dir) / "token.key"

	# 1. Explicit token path override
	token_override = os.environ.get("NVDA_TOKEN_PATH", "").strip()
	if token_override:
		return Path(token_override)

	# 2. Config path override (for portable NVDA or test harnesses)
	config_override = os.environ.get("NVDA_CONFIG_PATH", "").strip()
	if config_override:
		return Path(config_override) / "nvda-mcp-bridge" / "token.key"

	# 3. NVDA config path if running inside NVDA
	try:
		import globalVars  # type: ignore[reportMissingImports]

		config_path = getattr(getattr(globalVars, "appArgs", None), "configPath", None)
		if config_path:
			return Path(config_path) / "nvda-mcp-bridge" / "token.key"
	except Exception:
		pass

	# 4. %APPDATA%\nvda\nvda-mcp-bridge
	appdata = os.environ.get("APPDATA")
	if appdata:
		return Path(appdata) / "nvda" / "nvda-mcp-bridge" / "token.key"

	# 5. %LOCALAPPDATA%\nvda-mcp-bridge
	localappdata = os.environ.get("LOCALAPPDATA")
	if localappdata:
		return Path(localappdata) / "nvda-mcp-bridge" / "token.key"

	# 6. Fallback to user home directory
	return Path.home() / ".nvda-mcp-bridge" / "token.key"


def _harden_file_permissions(file_path: Path) -> bool:
	"""Apply restrictive Windows ACLs to the token file so only the current user can access it.

	Returns:
		True if permissions were successfully hardened, False otherwise.
	"""
	try:
		if os.name == "nt":
			# Use icacls to remove inherited permissions and grant full access only to current user
			username = os.environ.get("USERNAME")
			if not username:
				logHandler.log.error("USERNAME environment variable missing; cannot set Windows ACL")
				return False
			cmd = [
				"icacls.exe",
				str(file_path),
				"/inheritance:r",
				"/grant:r",
				f"{username}:(R,W)",
			]
			result = subprocess.run(
				cmd,
				stdout=subprocess.PIPE,
				stderr=subprocess.PIPE,
				check=False,
				timeout=5,
			)
			if result.returncode != 0:
				logHandler.log.error(
					f"icacls failed with return code {result.returncode}: {result.stderr.decode(errors='ignore')}"
				)
				return False
			return True
		else:
			file_path.chmod(0o600)
			return (file_path.stat().st_mode & 0o777) == 0o600
	except Exception as err:
		logHandler.log.error(f"Failed to set strict ACL on token file {file_path}: {err}")
		return False


def get_or_create_bridge_token(custom_dir: Optional[str | Path] = None) -> str:
	"""Obtain the bridge authentication token.

	Resolution order:
	1. If custom_dir is specified, manages token strictly in that directory (for tests/custom isolation).
	2. NVDA_BRIDGE_TOKEN environment variable (fails closed if insecure default without explicit opt-in).
	3. Installation-specific token file in Windows user profile.
	4. Cryptographically generates a fresh 256-bit token and persists it with user-only ACLs.

	Raises:
		ValueError: If an insecure default token is supplied without explicit migration override.
		RuntimeError: If token file creation or permission hardening fails (fails closed).
	"""
	if custom_dir is None:
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
				# Ensure existing file has hardened permissions
				_harden_file_permissions(token_path)
				return existing
		except Exception as err:
			logHandler.log.warning("Failed to read existing token file %s: %s", token_path, err)

	# Generate a cryptographically secure 256-bit URL-safe token
	token = secrets.token_urlsafe(32)

	# Atomic file creation with strict ACLs
	temp_path = token_path.with_suffix(".tmp")
	try:
		token_path.parent.mkdir(parents=True, exist_ok=True)
		temp_path.write_text(token, encoding="utf-8")
		if not _harden_file_permissions(temp_path):
			try:
				temp_path.unlink()
			except Exception:
				pass
			raise RuntimeError(f"Failed to enforce restrictive Windows ACLs on {temp_path}")
		temp_path.replace(token_path)
		logHandler.log.info("Generated new secure NVDA bridge authentication token at %s", token_path)
	except Exception as err:
		try:
			temp_path.unlink()
		except Exception:
			pass
		logHandler.log.error("Failed to securely persist bridge token to %s: %s", token_path, err)
		raise RuntimeError(f"Secure token persistence failed: {err}") from err

	return token


def constant_time_compare(token_a: str, token_b: str) -> bool:
	"""Perform constant-time comparison to prevent timing attacks."""
	if not token_a or not token_b:
		return False
	try:
		return hmac.compare_digest(token_a.encode("utf-8"), token_b.encode("utf-8"))
	except Exception:
		return False


class PermissionPolicy:
	"""Enforces capability-based permission grants for the bridge.

	Distinguishes:
	1. Authentication (valid token proof)
	2. Authorization (caller capabilities / granted permissions)
	3. Runtime restrictions (NVDA state checks)

	Defaults to read-only access and speech output, requiring explicit
	authorization for input control, sensitive diagnostics, NVDA lifecycle,
	and independent local approval for developer mode.
	"""

	def __init__(
		self,
		granted_permissions: Optional[Iterable[str]] = None,
	):
		self._lock = threading.Lock()
		if granted_permissions is not None:
			self._granted: set[str] = {p.strip().lower() for p in granted_permissions if p.strip()}
		else:
			# Check environment variable NVDA_BRIDGE_PERMISSIONS
			env_perms = os.environ.get("NVDA_BRIDGE_PERMISSIONS", "").strip()
			if env_perms:
				if env_perms.lower() in ("*", "all"):
					# All non-developer permissions
					self._granted = set(ALL_PERMISSIONS) - {PERMISSION_DEVELOPER}
				else:
					self._granted = {p.strip().lower() for p in env_perms.split(",") if p.strip()}
			else:
				# Default safe capabilities: read_only and speech_output
				self._granted = set(DEFAULT_PERMISSIONS)

	def is_granted(self, permission: str) -> bool:
		"""Check if a specific permission capability is currently granted."""
		with self._lock:
			return permission in self._granted

	def grant(self, permission: str) -> None:
		"""Explicitly grant a capability permission."""
		perm = permission.strip().lower()
		if perm not in ALL_PERMISSIONS:
			raise ValueError(f"Unknown permission capability: {permission}")
		with self._lock:
			self._granted.add(perm)
		logHandler.log.info(f"SECURITY AUDIT: Granted bridge permission '{perm}'")

	def revoke(self, permission: str) -> None:
		"""Revoke a previously granted capability permission immediately."""
		perm = permission.strip().lower()
		with self._lock:
			self._granted.discard(perm)
		logHandler.log.info(f"SECURITY AUDIT: Revoked bridge permission '{perm}'")

	def reset(self) -> None:
		"""Reset permissions to default safe capabilities."""
		with self._lock:
			self._granted = set(DEFAULT_PERMISSIONS)
		logHandler.log.info("SECURITY AUDIT: Reset bridge permissions to default")

	def set_granted(self, permissions: Iterable[str]) -> None:
		"""Replace the granted set, ignoring unknown capability names."""
		normalized = {p.strip().lower() for p in permissions if p and p.strip()}
		with self._lock:
			self._granted = {p for p in normalized if p in ALL_PERMISSIONS}
		logHandler.log.info(f"SECURITY AUDIT: Applied permission set {sorted(self.get_granted())}")

	def get_granted(self) -> frozenset[str]:
		"""Return a snapshot of currently granted permissions."""
		with self._lock:
			return frozenset(self._granted)


# Capability permissions the user may persist through the settings panel.
# "developer" is deliberately excluded: it is time-limited and must always
# require fresh local approval, never a stored grant.
PERSISTABLE_PERMISSIONS = frozenset({
	PERMISSION_READ_ONLY,
	PERMISSION_SPEECH_OUTPUT,
	PERMISSION_INPUT_CONTROL,
	PERMISSION_DIAGNOSTICS,
	PERMISSION_NVDA_LIFECYCLE,
	PERMISSION_DIALOG_CONTROL,
})

CONFIG_SECTION = "nvdaMcpBridge"
CONFIG_PERMISSIONS_KEY = "permissions"
CONFIG_DEVELOPER_TTL_KEY = "developerModeTtlSeconds"

#: Developer mode is time-limited by design. The window is configurable because
#: five minutes is shorter than a real debugging session, and re-approving
#: interrupts the thing being debugged; it is bounded because elevation is the
#: one grant that must never become permanent.
DEFAULT_DEVELOPER_TTL_SECONDS = 300
MIN_DEVELOPER_TTL_SECONDS = 60
MAX_DEVELOPER_TTL_SECONDS = 1800
#: Windows offered in the settings panel, in minutes.
DEVELOPER_TTL_CHOICES_MINUTES = (1, 5, 10, 15, 30)


def clamp_developer_ttl(seconds: Any) -> int:
	"""Return a usable elevation window, bounded to the configured limits."""
	try:
		value = int(seconds)
	except (TypeError, ValueError):
		return DEFAULT_DEVELOPER_TTL_SECONDS
	return max(MIN_DEVELOPER_TTL_SECONDS, min(MAX_DEVELOPER_TTL_SECONDS, value))


def load_developer_ttl_seconds() -> int:
	"""Return the saved elevation window, or the default when unset or invalid."""
	try:
		import config

		raw = config.conf[CONFIG_SECTION][CONFIG_DEVELOPER_TTL_KEY]
	except Exception:
		return DEFAULT_DEVELOPER_TTL_SECONDS
	return clamp_developer_ttl(raw)


def save_developer_ttl_seconds(seconds: int) -> bool:
	"""Persist the elevation window so it survives NVDA restarts and reloads."""
	value = clamp_developer_ttl(seconds)
	try:
		import config

		config.conf[CONFIG_SECTION][CONFIG_DEVELOPER_TTL_KEY] = value
		save = getattr(config.conf, "save", None)
		if callable(save):
			save()
		logHandler.log.info(f"SECURITY AUDIT: Developer mode elevation window set to {value} seconds")
		return True
	except Exception as e:  # pragma: no cover - depends on NVDA runtime
		logHandler.log.warning(f"NVDA MCP Bridge: could not persist developer mode window: {e}")
		return False


def register_config_spec() -> None:
	"""Register the persistent settings section with NVDA's configuration system.

	Must run before the section is read or written. Safe to call repeatedly and
	safe to call outside NVDA (it simply does nothing when config is absent).
	"""
	try:
		import config

		conf = getattr(config, "conf", None)
		if conf is None or not hasattr(conf, "spec"):
			return
		conf.spec.setdefault(
			CONFIG_SECTION,
			{CONFIG_PERMISSIONS_KEY: "string", CONFIG_DEVELOPER_TTL_KEY: "integer"},
		)
	except Exception as e:  # pragma: no cover - depends on NVDA runtime
		logHandler.log.debug(f"NVDA MCP Bridge: could not register config spec: {e}")


def load_persisted_permissions() -> Optional[frozenset[str]]:
	"""Return the saved permission set, or None when nothing was ever saved.

	An empty or unreadable value means "no stored preference", so callers keep
	the safe defaults rather than starting with no capabilities at all.
	"""
	try:
		import config

		raw = config.conf[CONFIG_SECTION][CONFIG_PERMISSIONS_KEY]
	except Exception:
		return None

	if not isinstance(raw, str) or not raw.strip():
		return None

	requested = {item.strip().lower() for item in raw.split(",") if item.strip()}
	accepted = {p for p in requested if p in PERSISTABLE_PERMISSIONS}
	# read_only and speech_output are the baseline capabilities, never toggleable
	# in the UI, so a stored preference always keeps them enabled.
	return frozenset(accepted | set(DEFAULT_PERMISSIONS))


def save_persisted_permissions(granted: Iterable[str]) -> bool:
	"""Store the given capability set so it survives NVDA restarts and reloads."""
	persistable = sorted({p.strip().lower() for p in granted if p and p.strip()} & PERSISTABLE_PERMISSIONS)
	try:
		import config

		config.conf[CONFIG_SECTION][CONFIG_PERMISSIONS_KEY] = ",".join(persistable)
		save = getattr(config.conf, "save", None)
		if callable(save):
			save()
		logHandler.log.info(f"SECURITY AUDIT: Persisted bridge permissions {persistable}")
		return True
	except Exception as e:  # pragma: no cover - depends on NVDA runtime
		logHandler.log.warning(f"NVDA MCP Bridge: could not persist permissions: {e}")
		return False


class DeveloperModeManager:
	"""Manages the developer mode authorization lifecycle for arbitrary Python execution.

	Security constraints:
	- Developer mode is disabled by default.
	- Elevation requires independent local user approval (local token or direct API call).
	- Ordinary bridge token cannot self-enable developer mode.
	- Elevation approvals are time-limited (default 300s, configurable 60-1800s in
	  the add-on settings) and automatically expire.
	- Disabling takes effect immediately.
	- Resets to disabled on NVDA restart or bridge shutdown.
	"""

	def __init__(
		self,
		initial_enabled: bool = False,
		default_ttl_seconds: int = DEFAULT_DEVELOPER_TTL_SECONDS,
		permission_policy: Optional[PermissionPolicy] = None,
	):
		self._enabled: bool = False
		self._expires_at: Optional[float] = None
		self._default_ttl_seconds: int = clamp_developer_ttl(default_ttl_seconds)
		self._local_approval_secret: Optional[str] = None
		self._secret_expires_at: Optional[float] = None
		self._local_token_path: Optional[Path] = None
		self._permission_policy: Optional[PermissionPolicy] = permission_policy

		if initial_enabled:
			self.approve_locally()

	@property
	def default_ttl_seconds(self) -> int:
		"""The window a plain elevation gets, in seconds, as configured."""
		return self._default_ttl_seconds

	def set_default_ttl_seconds(self, seconds: int) -> int:
		"""Change the window used by later elevations.

		An elevation already in progress keeps the expiry it was granted with:
		shortening the setting must not silently cut a session the user approved.
		"""
		self._default_ttl_seconds = clamp_developer_ttl(seconds)
		logHandler.log.info(
			f"SECURITY AUDIT: Developer mode elevation window changed to {self._default_ttl_seconds} seconds"
		)
		return self._default_ttl_seconds

	def set_permission_policy(self, policy: PermissionPolicy) -> None:
		"""Link permission policy for atomic capability grant and revocation."""
		self._permission_policy = policy
		if self.is_enabled:
			policy.grant(PERMISSION_DEVELOPER)

	@property
	def is_enabled(self) -> bool:
		"""Return whether developer mode is currently active and unexpired."""
		if not self._enabled:
			return False
		if self._expires_at is not None and time.monotonic() > self._expires_at:
			self.disable()
			return False
		return True

	@property
	def time_remaining_seconds(self) -> float:
		"""Return remaining active duration in seconds, or 0.0 if not active."""
		if not self.is_enabled or self._expires_at is None:
			return 0.0
		return max(0.0, self._expires_at - time.monotonic())

	def approve_locally(self, duration_seconds: Optional[int] = None) -> None:
		"""Approve developer mode locally (e.g. from NVDA settings, console, or local prompt)."""
		ttl = duration_seconds if duration_seconds is not None else self._default_ttl_seconds
		self._enabled = True
		self._expires_at = time.monotonic() + max(1, ttl)
		self._local_approval_secret = None
		self._secret_expires_at = None
		if self._permission_policy is not None:
			self._permission_policy.grant(PERMISSION_DEVELOPER)
		logHandler.log.warning(
			f"SECURITY AUDIT: Developer mode enabled via local approval for {ttl} seconds. "
			f"Arbitrary Python execution is permitted until expiration."
		)

	def enable(self, duration_seconds: Optional[int] = None) -> None:
		"""Alias for approve_locally() to enable developer mode."""
		self.approve_locally(duration_seconds=duration_seconds)

	def generate_local_approval_token(
		self, ttl_seconds: int = 60, custom_dir: Optional[str | Path] = None
	) -> str:
		"""Generate a short-lived, single-use local approval secret file.

		This secret is written to a user-restricted local file (dev_approval.key)
		that only the local Windows user can read. An external caller must present
		this secret to prove local user authorization.
		"""
		secret = secrets.token_hex(16)
		self._local_approval_secret = secret
		self._secret_expires_at = time.monotonic() + ttl_seconds

		token_dir = custom_dir if custom_dir is not None else resolve_token_file_path().parent
		approval_path = Path(token_dir) / "dev_approval.key"
		self._local_token_path = approval_path

		try:
			approval_path.parent.mkdir(parents=True, exist_ok=True)
			approval_path.write_text(secret, encoding="utf-8")
			_harden_file_permissions(approval_path)
			logHandler.log.info("Generated one-time local developer approval token at %s", approval_path)
		except Exception as err:
			logHandler.log.error("Failed to write local developer approval token: %s", err)
			raise RuntimeError(f"Failed to persist local approval token: {err}") from err

		return secret

	def approve_with_token(self, token: str, duration_seconds: Optional[int] = None) -> bool:
		"""Validate a local approval token and activate developer mode."""
		if not token or not self._local_approval_secret:
			return False
		if self._secret_expires_at is not None and time.monotonic() > self._secret_expires_at:
			self._cleanup_local_token()
			return False
		if not constant_time_compare(token.strip(), self._local_approval_secret.strip()):
			return False

		# Token is valid; consume it immediately (single-use)
		self._cleanup_local_token()
		self.approve_locally(duration_seconds=duration_seconds)
		return True

	def disable(self) -> None:
		"""Disable developer mode immediately."""
		self._enabled = False
		self._expires_at = None
		self._cleanup_local_token()
		if self._permission_policy is not None:
			self._permission_policy.revoke(PERMISSION_DEVELOPER)
		logHandler.log.info("SECURITY AUDIT: Developer mode disabled.")

	def reset(self) -> None:
		"""Reset developer mode to the default disabled state (fail-closed)."""
		self._enabled = False
		self._expires_at = None
		self._cleanup_local_token()
		if self._permission_policy is not None:
			self._permission_policy.revoke(PERMISSION_DEVELOPER)

	def _cleanup_local_token(self) -> None:
		self._local_approval_secret = None
		self._secret_expires_at = None
		if self._local_token_path and self._local_token_path.is_file():
			try:
				self._local_token_path.unlink()
			except Exception:
				pass
		self._local_token_path = None


def check_action_permissions(
	action_name: str,
	metadata: Optional[Any],
	nvda_status_snapshot: Dict[str, bool],
	dev_mode_enabled: bool,
	permission_policy: Optional[PermissionPolicy] = None,
	tool_category: Optional[str] = None,
) -> Tuple[bool, int, str]:
	"""Enforce explicit security, capability permissions, and runtime restrictions at the bridge boundary.

	Returns:
		Tuple of (allowed: bool, error_code: int, error_message: str)
	"""
	is_secure_screen = bool(nvda_status_snapshot.get("secure_screen", False))
	is_modal = bool(nvda_status_snapshot.get("modal_blocking", False))
	is_menu = bool(nvda_status_snapshot.get("menu_open", False))

	# 1. Derive required capability permission and allowed states
	if metadata is not None:
		required_permission = getattr(metadata, "required_permission", None)
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
		elif action_name == "send_keyboard" or category == "input":
			required_permission = PERMISSION_INPUT_CONTROL
			allowed_states = ("normal",)
		elif action_name == "dismiss":
			required_permission = PERMISSION_DIALOG_CONTROL
			allowed_states = ("normal", "modal", "menu")
		elif action_name in ("speak_text", "cancel_speech") or category == "speech":
			required_permission = PERMISSION_SPEECH_OUTPUT
			allowed_states = ("normal", "secure_desktop")
		elif category in ("navigation", "focus", "window", "text", "inspection"):
			required_permission = PERMISSION_READ_ONLY
			allowed_states = ("normal",)
		else:
			required_permission = None
			allowed_states = ("normal",)

	# Missing or unknown permission fails closed
	if not required_permission or required_permission not in ALL_PERMISSIONS:
		return (
			False,
			4003,
			f"Action '{action_name}' requires unknown or unspecified permission '{required_permission}'",
		)

	# 2. Secure screen restrictions — strictly forbid arbitrary execution and non-whitelisted actions
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

	# 3. Authorization check — capability permission grant
	if permission_policy is not None:
		if not permission_policy.is_granted(required_permission):
			return (
				False,
				4003,
				f"Action '{action_name}' requires permission '{required_permission}' which has not been granted",
			)

	# 4. Developer mode check — execute_python requires independent local approval
	if required_permission == PERMISSION_DEVELOPER or action_name == "execute_python":
		if not dev_mode_enabled:
			return (
				False,
				4003,
				f"Action '{action_name}' requires developer mode to be explicitly enabled with local approval",
			)

	# 5. Modal dialog blocking
	if is_modal and "modal" not in allowed_states:
		return (
			False,
			4202,
			f"Action '{action_name}' is blocked by active modal NVDA dialog",
		)

	# 6. NVDA menu open blocking
	if is_menu and "menu" not in allowed_states:
		return (
			False,
			4203,
			f"Action '{action_name}' is blocked while an NVDA menu is open",
		)

	return (True, 0, "")
