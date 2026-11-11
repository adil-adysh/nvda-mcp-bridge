"""MCP Client Configurator for NVDA MCP Bridge.

Provides accessible, safe detection, preview, and configuration of supported MCP clients:
- Visual Studio Code (user mcp.json using official 'servers' schema)
- Claude Desktop (using 'mcpServers' schema)
- Cursor (global and user-storage mcp.json)
- VS Code Roo Code / Cline extension
- Google Antigravity / Gemini CLI (global mcp_config.json)

Security and safety guarantees:
- Schema compliance tailored to each client's official specification
- Explicit user approval and conflict confirmation before writing
- Atomic JSON file writing via temporary files with round-trip verification
- Automatic backups (.bak.<timestamp>) before modifying any existing configuration
- Non-destructive merging: preserves all existing server configurations and settings
- Never writes bridge authentication tokens into client configuration files
- Uses the stable installed companion executable path
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import logHandler


@dataclass
class ClientInfo:
	client_id: str
	name: str
	config_path: Path
	detected: bool
	configured: bool
	has_conflict: bool
	target_key: str
	existing_command: Optional[str] = None


@dataclass
class ConfigPreview:
	client_id: str
	name: str
	config_path: Path
	detected: bool
	is_new_file: bool
	has_conflict: bool
	target_key: str
	existing_command: Optional[str]
	proposed_entry: Dict[str, Any]
	backup_path: Optional[Path]


class BaseClientAdapter:
	"""Base class for MCP client configuration format adapters."""

	def __init__(self, client_id: str, display_name: str, config_key: str):
		self.client_id = client_id
		self.display_name = display_name
		self.config_key = config_key

	def build_entry(self, resolved_exe: str) -> Dict[str, Any]:
		"""Build the server definition object for this client."""
		return {
			"command": resolved_exe,
			"args": [],
		}

	def inspect_entry(self, data: Dict[str, Any], resolved_exe: str) -> Tuple[bool, bool, Optional[str]]:
		"""Check if already configured or conflicting.

		Returns:
			Tuple of (is_configured: bool, has_conflict: bool, existing_cmd: Optional[str])
		"""
		servers = data.get(self.config_key)
		if not isinstance(servers, dict) or "nvda" not in servers:
			return False, False, None

		entry = servers["nvda"]
		if not isinstance(entry, dict):
			return False, True, str(entry)

		cmd = str(entry.get("command", "")).strip()
		if cmd.lower() == resolved_exe.lower():
			return True, False, cmd
		return False, bool(cmd), cmd

	def apply_entry(self, data: Dict[str, Any], resolved_exe: str) -> None:
		"""Insert or update the nvda server definition in data dictionary."""
		servers = data.setdefault(self.config_key, {})
		if not isinstance(servers, dict):
			raise ValueError(f"'{self.config_key}' in configuration is not a JSON object")
		servers["nvda"] = self.build_entry(resolved_exe)


class VSCodeAdapter(BaseClientAdapter):
	"""Official VS Code user configuration schema adapter.

	VS Code uses the top-level 'servers' key with 'type': 'stdio':
	{
	  "servers": {
	    "nvda": {
	      "type": "stdio",
	      "command": "C:\\path\\nvda-mcp-server.exe"
	    }
	  }
	}
	"""

	def __init__(self):
		super().__init__("vscode", "Visual Studio Code", "servers")

	def build_entry(self, resolved_exe: str) -> Dict[str, Any]:
		return {
			"type": "stdio",
			"command": resolved_exe,
		}

	def inspect_entry(self, data: Dict[str, Any], resolved_exe: str) -> Tuple[bool, bool, Optional[str]]:
		# Check official 'servers' key first
		is_conf, has_conf, cmd = super().inspect_entry(data, resolved_exe)
		if is_conf or has_conf:
			return is_conf, has_conf, cmd

		# Fallback: check legacy 'mcpServers' if user or older tool placed it there
		legacy_servers = data.get("mcpServers")
		if isinstance(legacy_servers, dict) and "nvda" in legacy_servers:
			entry = legacy_servers["nvda"]
			if isinstance(entry, dict):
				cmd = str(entry.get("command", "")).strip()
				if cmd.lower() == resolved_exe.lower():
					return True, False, cmd
				return False, bool(cmd), cmd

		return False, False, None


class StandardMcpAdapter(BaseClientAdapter):
	"""Standard MCP adapter using 'mcpServers' (Claude Desktop, Cursor, Antigravity, Roo Code)."""

	def __init__(self, client_id: str, display_name: str):
		super().__init__(client_id, display_name, "mcpServers")


_CLIENT_ADAPTERS: Dict[str, BaseClientAdapter] = {
	"vscode": VSCodeAdapter(),
	"claude_desktop": StandardMcpAdapter("claude_desktop", "Claude Desktop"),
	"cursor": StandardMcpAdapter("cursor", "Cursor"),
	"cursor_storage": StandardMcpAdapter("cursor_storage", "Cursor (App Storage)"),
	"vscode_roo": StandardMcpAdapter("vscode_roo", "VS Code (Roo Code)"),
	"antigravity": StandardMcpAdapter("antigravity", "Google Antigravity"),
}


def get_adapter_for_client(client_id: str) -> BaseClientAdapter:
	"""Retrieve the adapter for a client ID, defaulting to standard schema."""
	return _CLIENT_ADAPTERS.get(client_id, StandardMcpAdapter(client_id, client_id))


def get_default_client_candidates(
	appdata_override: Optional[Path] = None,
	userprofile_override: Optional[Path] = None,
) -> List[Tuple[str, str, Path]]:
	"""Return candidates for supported MCP client configuration files.

	Returns:
		List of tuples: (client_id, display_name, config_path)
	"""
	appdata = appdata_override or (Path(os.environ["APPDATA"]) if "APPDATA" in os.environ else None)
	userprofile = userprofile_override or (Path(os.environ["USERPROFILE"]) if "USERPROFILE" in os.environ else Path.home())

	candidates = []

	if appdata:
		# Claude Desktop
		candidates.append((
			"claude_desktop",
			"Claude Desktop",
			appdata / "Claude" / "claude_desktop_config.json",
		))

		# VS Code standard User MCP configuration
		candidates.append((
			"vscode",
			"Visual Studio Code",
			appdata / "Code" / "User" / "mcp.json",
		))

		# VS Code Roo Code / Cline extension MCP settings
		candidates.append((
			"vscode_roo",
			"VS Code (Roo Code)",
			appdata / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline" / "settings" / "cline_mcp_settings.json",
		))

		# Cursor user storage MCP settings
		candidates.append((
			"cursor_storage",
			"Cursor (App Storage)",
			appdata / "Cursor" / "User" / "globalStorage" / "cursor.mcp" / "mcp.json",
		))

	if userprofile:
		# Cursor standard user profile MCP settings
		candidates.append((
			"cursor",
			"Cursor",
			userprofile / ".cursor" / "mcp.json",
		))

		# Google Antigravity / Gemini global MCP configuration
		candidates.append((
			"antigravity",
			"Google Antigravity",
			userprofile / ".gemini" / "config" / "mcp_config.json",
		))

	return candidates


def detect_supported_clients(
	appdata_override: Optional[Path] = None,
	userprofile_override: Optional[Path] = None,
	expected_companion_exe: Optional[Path] = None,
) -> List[ClientInfo]:
	"""Detect installed MCP clients and their current configuration status."""
	candidates = get_default_client_candidates(appdata_override, userprofile_override)
	results: List[ClientInfo] = []

	expected_cmd = str(expected_companion_exe.resolve()).lower() if expected_companion_exe else None

	for client_id, display_name, config_path in candidates:
		parent_dir = config_path.parent
		is_detected = parent_dir.is_dir() or config_path.is_file()
		adapter = get_adapter_for_client(client_id)

		if not is_detected:
			results.append(
				ClientInfo(
					client_id=client_id,
					name=display_name,
					config_path=config_path,
					detected=False,
					configured=False,
					has_conflict=False,
					target_key=adapter.config_key,
				)
			)
			continue

		is_configured = False
		has_conflict = False
		existing_cmd = None

		if config_path.is_file():
			try:
				data = json.loads(config_path.read_text(encoding="utf-8"))
				if isinstance(data, dict):
					is_configured, has_conflict, existing_cmd = adapter.inspect_entry(
						data, expected_cmd or ""
					)
			except Exception as e:
				logHandler.log.debug(f"Could not parse config at {config_path}: {e}")

		results.append(
			ClientInfo(
				client_id=client_id,
				name=display_name,
				config_path=config_path,
				detected=True,
				configured=is_configured,
				has_conflict=has_conflict,
				target_key=adapter.config_key,
				existing_command=existing_cmd,
			)
		)

	return results


def preview_client_configuration(
	client_id: str,
	companion_exe_path: Path,
	config_path_override: Optional[Path] = None,
	appdata_override: Optional[Path] = None,
	userprofile_override: Optional[Path] = None,
) -> Tuple[bool, ConfigPreview, str]:
	"""Generate a non-destructive preview of configuration changes for a client.

	Returns:
		Tuple of (success: bool, preview: ConfigPreview, message: str)
	"""
	resolved_exe = str(companion_exe_path.resolve())
	adapter = get_adapter_for_client(client_id)

	if config_path_override:
		target_path = Path(config_path_override)
	else:
		candidates = get_default_client_candidates(appdata_override, userprofile_override)
		matched = [c for c in candidates if c[0] == client_id]
		if not matched:
			empty_preview = ConfigPreview(
				client_id=client_id,
				name=client_id,
				config_path=Path(""),
				detected=False,
				is_new_file=False,
				has_conflict=False,
				target_key=adapter.config_key,
				existing_command=None,
				proposed_entry={},
				backup_path=None,
			)
			return False, empty_preview, f"Unknown client ID: {client_id}"
		target_path = matched[0][2]

	is_new = not target_path.is_file()
	has_conflict = False
	existing_cmd = None
	backup_path = None

	if target_path.is_file():
		timestamp = int(time.time())
		backup_path = target_path.with_suffix(f".json.bak.{timestamp}")
		try:
			data = json.loads(target_path.read_text(encoding="utf-8"))
			if isinstance(data, dict):
				_, has_conflict, existing_cmd = adapter.inspect_entry(data, resolved_exe)
		except Exception as e:
			empty_preview = ConfigPreview(
				client_id=client_id,
				name=adapter.display_name,
				config_path=target_path,
				detected=True,
				is_new_file=False,
				has_conflict=False,
				target_key=adapter.config_key,
				existing_command=None,
				proposed_entry={},
				backup_path=None,
			)
			return False, empty_preview, f"Existing configuration contains invalid JSON: {e}"

	proposed = adapter.build_entry(resolved_exe)
	preview = ConfigPreview(
		client_id=client_id,
		name=adapter.display_name,
		config_path=target_path,
		detected=target_path.parent.is_dir() or target_path.is_file(),
		is_new_file=is_new,
		has_conflict=has_conflict,
		target_key=adapter.config_key,
		existing_command=existing_cmd,
		proposed_entry=proposed,
		backup_path=backup_path,
	)
	return True, preview, "Preview generated successfully"


def format_preview_text(preview: ConfigPreview) -> str:
	"""Format a screen-reader accessible plain-text summary of the configuration preview."""
	lines = [
		f"Client: {preview.name}",
		f"Configuration File: {preview.config_path}",
		f"Schema Section: '{preview.target_key}'",
	]
	if preview.is_new_file:
		lines.append("Status: New configuration file will be created.")
	elif preview.has_conflict:
		lines.append(f"Status: CONFLICT — currently configured with '{preview.existing_command}'.")
	else:
		lines.append("Status: Existing configuration file will be updated.")

	if preview.backup_path:
		lines.append(f"Backup File: {preview.backup_path.name}")

	lines.append("Proposed Server Entry:")
	lines.append(json.dumps({preview.target_key: {"nvda": preview.proposed_entry}}, indent=2))
	return "\n".join(lines)


def configure_client(
	client_id: str,
	companion_exe_path: Path,
	config_path_override: Optional[Path] = None,
	overwrite_conflict: bool = False,
	appdata_override: Optional[Path] = None,
	userprofile_override: Optional[Path] = None,
) -> Tuple[bool, str]:
	"""Configure an MCP client to use the NVDA MCP companion executable.

	Args:
		client_id: ID of the client to configure
		companion_exe_path: Path to the installed nvda-mcp-server.exe
		config_path_override: Optional explicit configuration path
		overwrite_conflict: Whether to overwrite a conflicting existing server definition
		appdata_override: Optional override for APPDATA directory
		userprofile_override: Optional override for USERPROFILE directory

	Returns:
		Tuple of (success: bool, message: str)
	"""
	if not companion_exe_path.is_file():
		return False, f"Companion executable does not exist at {companion_exe_path}. Please install companion first."

	resolved_exe = str(companion_exe_path.resolve())
	adapter = get_adapter_for_client(client_id)

	if config_path_override:
		target_path = Path(config_path_override)
	else:
		candidates = get_default_client_candidates(appdata_override, userprofile_override)
		matched = [c for c in candidates if c[0] == client_id]
		if not matched:
			return False, f"Unknown client ID: {client_id}"
		target_path = matched[0][2]

	data: Dict[str, Any] = {}
	if target_path.is_file():
		try:
			raw_content = target_path.read_text(encoding="utf-8").strip()
			if raw_content:
				data = json.loads(raw_content)
			if not isinstance(data, dict):
				return False, f"Config at {target_path} is not a JSON object"
		except Exception as e:
			return False, f"Failed to parse existing JSON at {target_path}: {e}"

	is_configured, has_conflict, existing_cmd = adapter.inspect_entry(data, resolved_exe)
	if is_configured:
		return True, f"{target_path.name} is already configured to use this companion executable"

	if has_conflict and not overwrite_conflict:
		return False, (
			f"Conflict: {target_path.name} already contains an 'nvda' server pointing to "
			f"'{existing_cmd}'. Pass overwrite_conflict=True or confirm replacement to overwrite it."
		)

	# Create backup before modifying existing file
	target_path.parent.mkdir(parents=True, exist_ok=True)
	if target_path.is_file():
		timestamp = int(time.time())
		backup_path = target_path.with_suffix(f".json.bak.{timestamp}")
		try:
			shutil.copy2(target_path, backup_path)
			logHandler.log.info(f"Created configuration backup at {backup_path}")
		except Exception as e:
			return False, f"Failed to create configuration backup: {e}"

	# Apply client-specific configuration structure
	try:
		adapter.apply_entry(data, resolved_exe)
	except Exception as e:
		return False, f"Failed to apply configuration schema: {e}"

	# Atomic write: write to .tmp, validate JSON, then rename
	tmp_path = target_path.with_suffix(".json.tmp")
	try:
		serialized = json.dumps(data, indent=2)
		# Validate in-memory before writing
		json.loads(serialized)
		tmp_path.write_text(serialized, encoding="utf-8")

		# Validate read back from disk
		verify_data = json.loads(tmp_path.read_text(encoding="utf-8"))
		assert adapter.config_key in verify_data and "nvda" in verify_data[adapter.config_key]

		tmp_path.replace(target_path)
		logHandler.log.info(f"Successfully configured NVDA MCP companion in {target_path}")
		return True, f"Successfully configured NVDA MCP server in {target_path}"
	except Exception as e:
		if tmp_path.is_file():
			try:
				tmp_path.unlink()
			except Exception:
				pass
		return False, f"Failed to write configuration: {e}"
