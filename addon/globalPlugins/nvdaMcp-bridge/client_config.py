"""MCP Client Configurator for NVDA MCP Bridge.

Provides accessible, safe detection and configuration of supported MCP clients:
- Visual Studio Code (user mcp.json and Cline/Roo Code extensions)
- Claude Desktop
- Cursor

Security and safety guarantees:
- Atomic JSON file writing via temporary files
- Automatic backups (.bak.<timestamp>) before modifying any existing configuration
- Non-destructive merging: preserves all existing server configurations and settings
- Silent overwriting of conflicting user-defined 'nvda' servers is rejected without approval
- Never writes the bridge authentication token into client config files
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
	existing_command: Optional[str] = None


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

		if not is_detected:
			results.append(
				ClientInfo(
					client_id=client_id,
					name=display_name,
					config_path=config_path,
					detected=False,
					configured=False,
					has_conflict=False,
				)
			)
			continue

		is_configured = False
		has_conflict = False
		existing_cmd = None

		if config_path.is_file():
			try:
				data = json.loads(config_path.read_text(encoding="utf-8"))
				mcp_servers = data.get("mcpServers") if isinstance(data, dict) else None
				if isinstance(mcp_servers, dict) and "nvda" in mcp_servers:
					nvda_entry = mcp_servers["nvda"]
					if isinstance(nvda_entry, dict):
						existing_cmd = str(nvda_entry.get("command", ""))
						if expected_cmd and existing_cmd.lower() == expected_cmd:
							is_configured = True
						elif existing_cmd:
							has_conflict = True
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
				existing_command=existing_cmd,
			)
		)

	return results


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

	Returns:
		Tuple of (success: bool, message: str)
	"""
	if not companion_exe_path.is_file():
		return False, f"Companion executable does not exist at {companion_exe_path}. Please install companion first."

	resolved_exe = str(companion_exe_path.resolve())

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

	servers = data.setdefault("mcpServers", {})
	if not isinstance(servers, dict):
		return False, f"'mcpServers' in {target_path} is not a JSON dictionary"

	if "nvda" in servers:
		existing = servers["nvda"]
		if isinstance(existing, dict):
			curr_cmd = str(existing.get("command", ""))
			if curr_cmd.lower() == resolved_exe.lower():
				return True, f"{target_path.name} is already configured to use this companion executable"
			if not overwrite_conflict:
				return False, (
					f"Conflict: {target_path.name} already contains an 'nvda' server pointing to "
					f"'{curr_cmd}'. Pass overwrite_conflict=True to replace it."
				)

	# Create backup before modifying
	target_path.parent.mkdir(parents=True, exist_ok=True)
	if target_path.is_file():
		timestamp = int(time.time())
		backup_path = target_path.with_suffix(f".json.bak.{timestamp}")
		try:
			shutil.copy2(target_path, backup_path)
			logHandler.log.info(f"Created configuration backup at {backup_path}")
		except Exception as e:
			return False, f"Failed to create configuration backup: {e}"

	# Register nvda server entry (WITHOUT token)
	servers["nvda"] = {
		"command": resolved_exe,
		"args": [],
	}

	# Atomic write: write to .tmp, validate JSON, then rename
	tmp_path = target_path.with_suffix(".json.tmp")
	try:
		serialized = json.dumps(data, indent=2)
		# Validate in-memory before writing
		json.loads(serialized)
		tmp_path.write_text(serialized, encoding="utf-8")

		# Validate read back from disk
		verify_data = json.loads(tmp_path.read_text(encoding="utf-8"))
		assert "mcpServers" in verify_data and "nvda" in verify_data["mcpServers"]

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
