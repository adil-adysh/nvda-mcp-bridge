"""MCP Client Configurator for NVDA MCP Bridge.

Provides accessible, safe detection, preview, and configuration of supported MCP clients:
- Visual Studio Code (user mcp.json using official 'servers' schema)
- Claude Desktop (using 'mcpServers' schema)
- Claude Code (user-scope mcpServers in ~/.claude.json)
- Codex CLI (mcp_servers tables in ~/.codex/config.toml)
- Cursor (global and user-storage mcp.json)
- VS Code Roo Code / Cline extension
- Google Antigravity / Gemini CLI (global mcp_config.json)

Security and safety guarantees:
- Schema compliance tailored to each client's official specification
- Explicit user approval and conflict confirmation before writing
- Atomic file writing via temporary files with round-trip verification
- Automatic backups (.bak.<timestamp>) before modifying any existing configuration
- Non-destructive merging: preserves all existing server configurations and settings
- Never writes bridge authentication tokens into client configuration files
- Uses the stable installed companion executable path
- Refuses to write a file another process changed while it was being read
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import logHandler

try:
	import tomllib
except ImportError:  # pragma: no cover - NVDA ships Python 3.11 or newer
	tomllib = None


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
	"""Base class for MCP client configuration format adapters.

	The JSON clients share one implementation: parse the file, merge the 'nvda'
	server into ``config_key``, serialise the whole document back. Clients whose
	configuration is not JSON (Codex uses TOML) override ``load``, ``render`` and
	``inspect`` and leave the dictionary helpers unused.
	"""

	config_format = "json"

	def __init__(self, client_id: str, display_name: str, config_key: str):
		self.client_id = client_id
		self.display_name = display_name
		self.config_key = config_key

	def load(self, text: str) -> Dict[str, Any]:
		"""Parse configuration text into a dictionary."""
		if not text or not text.strip():
			return {}
		data = json.loads(text)
		if not isinstance(data, dict):
			raise ValueError("configuration top level is not a JSON object")
		return data

	def dump(self, data: Dict[str, Any]) -> str:
		"""Serialise a configuration dictionary back to text."""
		return json.dumps(data, indent=2)

	def inspect(self, text: str, resolved_exe: str) -> Tuple[bool, bool, Optional[str]]:
		"""Report whether the nvda server is present and correct in this text."""
		return self.inspect_entry(self.load(text), resolved_exe)

	def render(self, text: str, resolved_exe: str) -> str:
		"""Return text with the nvda server definition inserted or updated."""
		data = self.load(text)
		self.apply_entry(data, resolved_exe)
		return self.dump(data)

	def format_entry(self, entry: Dict[str, Any]) -> str:
		"""Render a server definition the way it will appear in this client's file."""
		return json.dumps({self.config_key: {"nvda": entry}}, indent=2)

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


class ClaudeCodeAdapter(StandardMcpAdapter):
	"""Claude Code user-scope configuration (``~/.claude.json``).

	Claude Code keeps every scope in one file: user-scope servers sit in the
	top-level ``mcpServers`` object, local (project) scope under
	``projects[<path>].mcpServers``, and project scope in a checked-in
	``.mcp.json``. The entry is a stdio server, and the companion reads the bridge
	token from disk itself, so no environment or headers belong in this file.

	The file also holds account and history state and is rewritten by the client
	while it runs, which is why ``configure_client`` refuses to replace a file
	that changed under it.
	"""

	def __init__(self):
		super().__init__("claude_code", "Claude Code (CLI)")

	def build_entry(self, resolved_exe: str) -> Dict[str, Any]:
		return {
			"type": "stdio",
			"command": resolved_exe,
			"args": [],
			"env": {},
		}


class CodexAdapter(BaseClientAdapter):
	"""Codex CLI configuration (``~/.codex/config.toml``).

	Codex defines servers as TOML tables::

		[mcp_servers.nvda]
		command = 'C:\\Users\\you\\nvda-mcp-server.exe'
		args = []

	That file also carries the user's model, project trust, plugin and shell
	settings, and the standard library has no TOML writer. So this adapter edits
	text rather than re-serialising a document: it replaces exactly the
	``[mcp_servers.nvda]`` table (including any sub-tables such as
	``[mcp_servers.nvda.env]``) or appends one, leaving every other byte - comments
	included - untouched. The result is re-parsed before it is written.
	"""

	config_format = "toml"
	_HEADER_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
	_TABLE = "mcp_servers.nvda"

	def __init__(self):
		super().__init__("codex", "Codex CLI", "mcp_servers")

	def load(self, text: str) -> Dict[str, Any]:
		if tomllib is None:
			raise ValueError("This Python lacks tomllib, so Codex configuration cannot be parsed")
		if not text or not text.strip():
			return {}
		return tomllib.loads(text)

	def build_entry(self, resolved_exe: str) -> Dict[str, Any]:
		return {"command": resolved_exe}

	def inspect(self, text: str, resolved_exe: str) -> Tuple[bool, bool, Optional[str]]:
		data = self.load(text)
		servers = data.get("mcp_servers") if isinstance(data, dict) else None
		entry = servers.get("nvda") if isinstance(servers, dict) else None
		if not isinstance(entry, dict):
			return False, False, None
		cmd = str(entry.get("command", "")).strip()
		if cmd.lower() == resolved_exe.lower():
			return True, False, cmd
		return False, bool(cmd), cmd

	@staticmethod
	def _toml_string(value: str) -> str:
		"""Quote a value as a TOML string, preferring a literal string for paths."""
		if "'" not in value and "\n" not in value:
			return f"'{value}'"
		escaped = value.replace("\\", "\\\\").replace('"', '\\"')
		return f'"{escaped}"'

	def _block(self, resolved_exe: str) -> str:
		return "\n".join(
			[
				f"[{self._TABLE}]",
				f"command = {self._toml_string(resolved_exe)}",
			]
		)

	def format_entry(self, entry: Dict[str, Any]) -> str:
		return self._block(str(entry.get("command", "")))

	def render(self, text: str, resolved_exe: str) -> str:
		text = text or ""
		lines = text.splitlines()
		block = self._block(resolved_exe)

		start = None
		for index, line in enumerate(lines):
			match = self._HEADER_RE.match(line)
			if match and match.group(1).strip() == self._TABLE:
				start = index
				break

		if start is None:
			# A server defined with dotted keys or an inline table has no header to
			# replace; rewriting it would need a real TOML writer, so say so instead.
			data = self.load(text)
			servers = data.get("mcp_servers") if isinstance(data, dict) else None
			if isinstance(servers, dict) and "nvda" in servers:
				raise ValueError(
					"'mcp_servers.nvda' is defined without a table header; "
					"edit the Codex configuration by hand"
				)
			body = text.rstrip("\n")
			return (body + "\n\n" if body else "") + block + "\n"

		# The table runs until the next header that is not one of its own sub-tables,
		# so a stale [mcp_servers.nvda.env] goes with it instead of being orphaned
		# onto the entry we just wrote.
		end = len(lines)
		for index in range(start + 1, len(lines)):
			match = self._HEADER_RE.match(lines[index])
			if not match:
				continue
			name = match.group(1).strip()
			if name == self._TABLE or name.startswith(self._TABLE + "."):
				continue
			end = index
			break

		return "\n".join(lines[:start] + block.splitlines() + lines[end:]) + "\n"


_CLIENT_ADAPTERS: Dict[str, BaseClientAdapter] = {
	"vscode": VSCodeAdapter(),
	"claude_desktop": StandardMcpAdapter("claude_desktop", "Claude Desktop"),
	"claude_code": ClaudeCodeAdapter(),
	"codex": CodexAdapter(),
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
) -> List[Tuple[str, str, Path, Path]]:
	"""Return candidates for supported MCP client configuration files.

	Returns:
		List of tuples: (client_id, display_name, config_path, probe_path).
		The probe is what proves the client is installed: a configuration file, a
		settings directory, or a marker directory the client creates. Probing the
		parent of the config path is wrong for files that live directly in the
		home directory, where that parent always exists.
	"""
	appdata = appdata_override or (Path(os.environ["APPDATA"]) if "APPDATA" in os.environ else None)
	userprofile = userprofile_override or (Path(os.environ["USERPROFILE"]) if "USERPROFILE" in os.environ else Path.home())

	candidates: List[Tuple[str, str, Path, Path]] = []

	if appdata:
		# Claude Desktop
		candidates.append((
			"claude_desktop",
			"Claude Desktop",
			appdata / "Claude" / "claude_desktop_config.json",
			appdata / "Claude",
		))

		# VS Code standard User MCP configuration
		candidates.append((
			"vscode",
			"Visual Studio Code",
			appdata / "Code" / "User" / "mcp.json",
			appdata / "Code" / "User",
		))

		# VS Code Roo Code / Cline extension MCP settings
		candidates.append((
			"vscode_roo",
			"VS Code (Roo Code)",
			appdata / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline" / "settings" / "cline_mcp_settings.json",
			appdata / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline" / "settings",
		))

		# Cursor user storage MCP settings
		candidates.append((
			"cursor_storage",
			"Cursor (App Storage)",
			appdata / "Cursor" / "User" / "globalStorage" / "cursor.mcp" / "mcp.json",
			appdata / "Cursor",
		))

	if userprofile:
		# Cursor standard user profile MCP settings
		candidates.append((
			"cursor",
			"Cursor",
			userprofile / ".cursor" / "mcp.json",
			userprofile / ".cursor",
		))

		# Google Antigravity / Gemini global MCP configuration
		candidates.append((
			"antigravity",
			"Google Antigravity",
			userprofile / ".gemini" / "config" / "mcp_config.json",
			userprofile / ".gemini" / "config",
		))

		# Claude Code keeps user-scope servers in its main state file
		candidates.append((
			"claude_code",
			"Claude Code (CLI)",
			userprofile / ".claude.json",
			userprofile / ".claude.json",
		))

		# Codex CLI configuration is TOML, next to its sessions and plugins
		candidates.append((
			"codex",
			"Codex CLI",
			userprofile / ".codex" / "config.toml",
			userprofile / ".codex",
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

	for client_id, display_name, config_path, probe_path in candidates:
		is_detected = probe_path.exists() or config_path.is_file()
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
				is_configured, has_conflict, existing_cmd = adapter.inspect(
					config_path.read_text(encoding="utf-8"), expected_cmd or ""
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
		backup_path = target_path.with_name(f"{target_path.name}.bak.{timestamp}")
		try:
			_, has_conflict, existing_cmd = adapter.inspect(
				target_path.read_text(encoding="utf-8"), resolved_exe
			)
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
			return False, empty_preview, f"Existing configuration could not be parsed: {e}"

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
	lines.append(get_adapter_for_client(preview.client_id).format_entry(preview.proposed_entry))
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

	original_text = ""
	original_stat = None
	if target_path.is_file():
		try:
			original_text = target_path.read_text(encoding="utf-8")
			original_stat = target_path.stat()
		except Exception as e:
			return False, f"Failed to read existing configuration at {target_path}: {e}"

	try:
		is_configured, has_conflict, existing_cmd = adapter.inspect(original_text, resolved_exe)
	except Exception as e:
		return False, f"Failed to parse existing configuration at {target_path}: {e}"

	if is_configured:
		return True, f"{target_path.name} is already configured to use this companion executable"

	if has_conflict and not overwrite_conflict:
		return False, (
			f"Conflict: {target_path.name} already contains an 'nvda' server pointing to "
			f"'{existing_cmd}'. Pass overwrite_conflict=True or confirm replacement to overwrite it."
		)

	# Create backup before modifying existing file
	target_path.parent.mkdir(parents=True, exist_ok=True)
	if original_stat is not None:
		timestamp = int(time.time())
		backup_path = target_path.with_name(f"{target_path.name}.bak.{timestamp}")
		try:
			shutil.copy2(target_path, backup_path)
			logHandler.log.info(f"Created configuration backup at {backup_path}")
		except Exception as e:
			return False, f"Failed to create configuration backup: {e}"

	# Apply client-specific configuration structure
	try:
		updated_text = adapter.render(original_text, resolved_exe)
	except Exception as e:
		return False, f"Failed to apply configuration schema: {e}"

	# Atomic write: write to .tmp, validate it, then rename
	tmp_path = target_path.with_name(target_path.name + ".tmp")
	try:
		if original_stat is not None:
			current = target_path.stat()
			if current.st_mtime_ns != original_stat.st_mtime_ns or current.st_size != original_stat.st_size:
				return False, (
					f"{target_path.name} changed while it was being read, most likely because the "
					"client is running. Close the client and try again."
				)

		tmp_path.write_text(updated_text, encoding="utf-8")

		# Validate read back from disk
		verified, _, written = adapter.inspect(tmp_path.read_text(encoding="utf-8"), resolved_exe)
		if not verified:
			raise ValueError(
				f"written configuration does not point at {resolved_exe} (found '{written}')"
			)

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
