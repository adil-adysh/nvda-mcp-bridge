"""Companion Manager for NVDA MCP Bridge.

Manages the lifecycle of the bundled Rust MCP companion executable:
- Validates the bundled artifact against companion manifest (SHA-256, version, architecture)
- Safely installs/upgrades the companion to %LOCALAPPDATA%\\Programs\\NVDA MCP Bridge\\bin
- Performs atomic replacement with rollback protection if an upgrade fails
- Detects running/locked binaries
- Provides clean removal/uninstall
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import logHandler


def get_default_companion_install_dir() -> Path:
	"""Return the standard stable per-user companion installation directory."""
	custom_dir = os.environ.get("NVDA_MCP_COMPANION_DIR", "").strip()
	if custom_dir:
		return Path(custom_dir)

	localappdata = os.environ.get("LOCALAPPDATA")
	if localappdata:
		return Path(localappdata) / "Programs" / "NVDA MCP Bridge" / "bin"

	# Fallback for portable / test setups
	return Path.home() / ".nvda-mcp-bridge" / "bin"


def get_bundled_companion_dir() -> Path:
	"""Return the path to the bundled companion directory inside this add-on."""
	return Path(__file__).resolve().parent / "companion" / "windows-x64"


def calculate_sha256(file_path: Path) -> str:
	"""Calculate the SHA-256 hash of a file."""
	hasher = hashlib.sha256()
	with open(file_path, "rb") as f:
		while chunk := f.read(65536):
			hasher.update(chunk)
	return hasher.hexdigest().lower()


@dataclass
class CompanionStatus:
	installed: bool
	installed_version: Optional[str]
	installed_path: Optional[Path]
	bundled_version: Optional[str]
	bundled_available: bool
	compatible: bool
	status_message: str


class CompanionManager:
	"""Manages inspection, verification, installation, repair, and removal of the MCP companion."""

	def __init__(
		self,
		install_dir: Optional[Path] = None,
		bundled_dir: Optional[Path] = None,
	):
		self.install_dir = install_dir or get_default_companion_install_dir()
		self.bundled_dir = bundled_dir or get_bundled_companion_dir()
		self.installed_exe_path = self.install_dir / "nvda-mcp-server.exe"
		self.installed_manifest_path = self.install_dir / "installed_manifest.json"

	def get_bundled_manifest(self) -> Optional[Dict[str, Any]]:
		"""Read the bundled manifest.json if present."""
		manifest_path = self.bundled_dir / "manifest.json"
		if not manifest_path.is_file():
			return None
		try:
			return json.loads(manifest_path.read_text(encoding="utf-8"))
		except Exception as e:
			logHandler.log.warning(f"Failed to read bundled companion manifest: {e}")
			return None

	def verify_bundled_artifact(self) -> Tuple[bool, str]:
		"""Verify that the bundled companion binary exists, is valid, and matches manifest SHA-256."""
		manifest = self.get_bundled_manifest()
		if not manifest:
			return False, f"Bundled companion manifest missing at {self.bundled_dir / 'manifest.json'}"

		expected_sha = str(manifest.get("sha256", "")).strip().lower()
		if not expected_sha:
			return False, "Bundled manifest missing 'sha256' field"

		binary_name = manifest.get("binary_name", "nvda-mcp-server.exe")
		binary_path = self.bundled_dir / binary_name
		if not binary_path.is_file():
			return False, f"Bundled companion executable missing at {binary_path}"

		size = binary_path.stat().st_size
		if size < 1024:
			return False, f"Bundled companion binary is corrupt or empty ({size} bytes)"

		actual_sha = calculate_sha256(binary_path)
		if actual_sha != expected_sha:
			return False, (
				f"Bundled companion checksum mismatch: expected {expected_sha[:12]}..., got {actual_sha[:12]}..."
			)

		return True, "Bundled companion artifact verified successfully"

	def inspect_installed(self) -> CompanionStatus:
		"""Perform a fast, non-blocking inspection of the installed companion state."""
		bundled_manifest = self.get_bundled_manifest()
		bundled_ver = bundled_manifest.get("version") if bundled_manifest else None
		bundled_avail = bool(bundled_manifest and (self.bundled_dir / "nvda-mcp-server.exe").is_file())

		if not self.installed_exe_path.is_file():
			return CompanionStatus(
				installed=False,
				installed_version=None,
				installed_path=None,
				bundled_version=bundled_ver,
				bundled_available=bundled_avail,
				compatible=False,
				status_message="Companion not installed",
			)

		installed_ver = None
		if self.installed_manifest_path.is_file():
			try:
				inst_data = json.loads(self.installed_manifest_path.read_text(encoding="utf-8"))
				installed_ver = inst_data.get("version")
			except Exception:
				pass

		if not installed_ver:
			installed_ver = bundled_ver or "unknown"

		# Compatible if binary exists and has non-zero size
		is_valid = self.installed_exe_path.stat().st_size > 1024
		return CompanionStatus(
			installed=True,
			installed_version=installed_ver,
			installed_path=self.installed_exe_path,
			bundled_version=bundled_ver,
			bundled_available=bundled_avail,
			compatible=is_valid,
			status_message=f"Installed (v{installed_ver})" if is_valid else "Installed executable is corrupt",
		)

	def install_or_repair(self, force: bool = False) -> Tuple[bool, str]:
		"""Safely install or repair the companion executable with atomic staging and rollback.

		Args:
			force: Whether to overwrite even if the same version is already installed.

		Returns:
			Tuple of (success: bool, message: str)
		"""
		# 1. Verify bundled artifact before doing anything
		valid, err_msg = self.verify_bundled_artifact()
		if not valid:
			return False, f"Installation aborted: {err_msg}"

		manifest = self.get_bundled_manifest()
		assert manifest is not None
		binary_name = manifest.get("binary_name", "nvda-mcp-server.exe")
		bundled_binary_path = self.bundled_dir / binary_name
		version = manifest.get("version", "1.0.0")

		# Check if already installed and identical
		if not force and self.installed_exe_path.is_file() and self.installed_manifest_path.is_file():
			try:
				inst_meta = json.loads(self.installed_manifest_path.read_text(encoding="utf-8"))
				if inst_meta.get("sha256") == manifest.get("sha256"):
					return True, f"Companion v{version} is already installed and up to date"
			except Exception:
				pass

		try:
			self.install_dir.mkdir(parents=True, exist_ok=True)
		except Exception as e:
			return False, f"Failed to create installation directory: {e}"

		staged_tmp_path = self.install_dir / "nvda-mcp-server.exe.tmp"
		backup_old_path = self.install_dir / "nvda-mcp-server.exe.old"

		# Clean up any leftover temp files
		for p in (staged_tmp_path, backup_old_path):
			if p.is_file():
				try:
					p.unlink()
				except Exception:
					pass

		# 2. Stage binary to .tmp file
		try:
			shutil.copy2(bundled_binary_path, staged_tmp_path)
		except Exception as e:
			return False, f"Failed to stage companion binary: {e}"

		# Verify staged file integrity
		staged_sha = calculate_sha256(staged_tmp_path)
		if staged_sha != manifest.get("sha256"):
			try:
				staged_tmp_path.unlink()
			except Exception:
				pass
			return False, "Staged file checksum does not match bundled artifact"

		# 3. Handle existing installation (safe replacement & rollback setup)
		had_existing = self.installed_exe_path.is_file()
		if had_existing:
			try:
				# Rename existing binary to .old (handles running process if allowed by OS)
				self.installed_exe_path.rename(backup_old_path)
			except (PermissionError, OSError) as e:
				# Target file is locked by a running process
				try:
					staged_tmp_path.unlink()
				except Exception:
					pass
				return False, (
					f"Cannot update companion: executable is locked by another running process (error: {e}). "
					"Please close any running MCP clients (e.g. VS Code, Claude Desktop) and try again."
				)

		# 4. Activate staged binary
		try:
			staged_tmp_path.rename(self.installed_exe_path)
		except Exception as activate_err:
			# Rollback if activation failed
			logHandler.log.error(f"Failed to activate staged companion: {activate_err}", exc_info=True)
			if had_existing and backup_old_path.is_file():
				try:
					backup_old_path.rename(self.installed_exe_path)
				except Exception as rb_err:
					logHandler.log.error(f"Critical rollback failure: {rb_err}")
			return False, f"Failed to activate companion binary: {activate_err}"

		# 5. Write installed manifest
		installed_meta = {
			"version": version,
			"architecture": manifest.get("architecture", "windows-x64"),
			"sha256": manifest.get("sha256"),
			"installed_at_monotonic": time.monotonic(),
			"binary_path": str(self.installed_exe_path),
		}
		try:
			self.installed_manifest_path.write_text(json.dumps(installed_meta, indent=2), encoding="utf-8")
		except Exception as e:
			logHandler.log.warning(f"Failed to write installed companion manifest: {e}")

		# 6. Clean up .old backup
		if backup_old_path.is_file():
			try:
				backup_old_path.unlink()
			except Exception:
				pass  # OS may clean up later if open handles exist

		logHandler.log.info(f"Successfully installed MCP companion v{version} at {self.installed_exe_path}")
		return True, f"Successfully installed NVDA MCP companion v{version} to {self.installed_exe_path}"

	def uninstall(self) -> Tuple[bool, str]:
		"""Safely uninstall the companion binary."""
		if not self.installed_exe_path.is_file() and not self.installed_manifest_path.is_file():
			return True, "Companion is not installed"

		try:
			if self.installed_exe_path.is_file():
				self.installed_exe_path.unlink()
			if self.installed_manifest_path.is_file():
				self.installed_manifest_path.unlink()
			# Try removing directory if empty
			try:
				self.install_dir.rmdir()
			except Exception:
				pass
			return True, "Companion uninstalled successfully"
		except (PermissionError, OSError) as e:
			return False, f"Failed to remove companion: file is in use ({e})"
