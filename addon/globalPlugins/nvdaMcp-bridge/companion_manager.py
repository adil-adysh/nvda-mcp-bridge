"""Companion Manager for NVDA MCP Bridge.

Manages the complete lifecycle of the bundled Rust MCP companion executable:
- Verifies bundled artifacts against manifest (SHA-256, version, PE headers, x64 architecture)
- Fast non-blocking inspection during NVDA startup
- In-depth verification distinguishing missing, installed, outdated, corrupt, and incompatible states
- Complete 9-step atomic installation transaction with rollback protection
- Threaded/cancellable background execution support for non-blocking UI
- Running/locked binary detection and clean uninstall
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

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


def validate_pe_binary(file_path: Path, expected_arch: str = "windows-x64") -> Tuple[bool, str]:
	"""Validate that a file is a well-formed Windows PE binary matching the expected architecture."""
	if not file_path.is_file():
		return False, f"File does not exist: {file_path}"

	size = file_path.stat().st_size
	if size < 1024:
		return False, f"File too small ({size} bytes) for valid PE binary"

	try:
		with open(file_path, "rb") as f:
			dos_header = f.read(64)
			if len(dos_header) < 64 or dos_header[:2] != b"MZ":
				return False, "File is not a valid PE executable (missing MZ signature)"

			pe_offset = struct.unpack("<I", dos_header[0x3C:0x40])[0]
			if pe_offset < 64 or pe_offset > size - 24:
				return False, f"Corrupt PE header offset: {pe_offset}"

			f.seek(pe_offset)
			pe_sig = f.read(4)
			if pe_sig != b"PE\x00\x00":
				return False, "File is not a valid PE executable (missing PE signature)"

			machine = struct.unpack("<H", f.read(2))[0]
			# 0x8664 is IMAGE_FILE_MACHINE_AMD64 (x86_64)
			if expected_arch == "windows-x64" and machine != 0x8664:
				return False, f"Incompatible architecture: binary machine type {hex(machine)}, expected x64 (0x8664)"

		return True, "Valid Windows x64 PE executable"
	except Exception as e:
		return False, f"Failed to parse PE binary: {e}"


@dataclass
class CompanionStatus:
	installed: bool
	installed_version: Optional[str]
	installed_path: Optional[Path]
	bundled_version: Optional[str]
	bundled_available: bool
	compatible: bool
	integrity_state: str  # "missing", "installed", "outdated", "corrupt", "incompatible"
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
		"""Verify that the bundled companion binary exists, is valid PE, and matches manifest SHA-256."""
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

		pe_valid, pe_msg = validate_pe_binary(binary_path, manifest.get("architecture", "windows-x64"))
		if not pe_valid:
			return False, f"Bundled binary validation failed: {pe_msg}"

		actual_sha = calculate_sha256(binary_path)
		if actual_sha != expected_sha:
			return False, (
				f"Bundled companion checksum mismatch: expected {expected_sha[:12]}..., got {actual_sha[:12]}..."
			)

		return True, "Bundled companion artifact verified successfully"

	def verify_installed_artifact(self) -> Tuple[bool, str, str]:
		"""Deep verification of the installed companion binary.

		Returns:
			Tuple of (is_valid: bool, status_message: str, integrity_state: str)
			Where integrity_state is one of:
			- "missing": executable does not exist
			- "installed": executable exists, valid PE, matches bundled or recorded manifest
			- "outdated": executable exists and valid, but version/sha older than bundled
			- "corrupt": executable exists but failed PE validation or checksum mismatch
			- "incompatible": executable exists but machine architecture does not match x64
		"""
		if not self.installed_exe_path.is_file():
			return False, "Companion executable is not installed", "missing"

		bundled_manifest = self.get_bundled_manifest()
		expected_arch = bundled_manifest.get("architecture", "windows-x64") if bundled_manifest else "windows-x64"

		pe_valid, pe_msg = validate_pe_binary(self.installed_exe_path, expected_arch=expected_arch)
		if not pe_valid:
			if "Incompatible architecture" in pe_msg:
				return False, f"Installed companion has incompatible architecture: {pe_msg}", "incompatible"
			return False, f"Installed companion is corrupt: {pe_msg}", "corrupt"

		actual_sha = calculate_sha256(self.installed_exe_path)

		if bundled_manifest:
			bundled_sha = str(bundled_manifest.get("sha256", "")).strip().lower()
			bundled_ver = bundled_manifest.get("version", "1.0.0")
			if actual_sha == bundled_sha:
				return True, f"Installed companion (v{bundled_ver}) is valid and up to date", "installed"

		# If different from bundled, check installed_manifest
		if self.installed_manifest_path.is_file():
			try:
				inst_meta = json.loads(self.installed_manifest_path.read_text(encoding="utf-8"))
				recorded_sha = str(inst_meta.get("sha256", "")).strip().lower()
				inst_ver = inst_meta.get("version", "unknown")
				if recorded_sha and actual_sha == recorded_sha:
					# Valid installed binary, but older than bundled
					return True, f"Installed companion (v{inst_ver}) is valid but outdated", "outdated"
			except Exception:
				pass

		return False, "Installed companion binary checksum mismatch (file is modified or corrupt)", "corrupt"

	def inspect_installed(self, verify_hash: bool = False) -> CompanionStatus:
		"""Perform companion status inspection.

		Args:
			verify_hash: When False (default for fast NVDA startup), performs fast metadata
				and PE header check without hashing the entire binary. When True, calculates
				full SHA-256 for thorough verification.
		"""
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
				integrity_state="missing",
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

		if verify_hash:
			is_valid, msg, state = self.verify_installed_artifact()
			return CompanionStatus(
				installed=True,
				installed_version=installed_ver,
				installed_path=self.installed_exe_path,
				bundled_version=bundled_ver,
				bundled_available=bundled_avail,
				compatible=is_valid,
				integrity_state=state,
				status_message=msg,
			)

		# Fast check for NVDA startup: check size and PE signature
		pe_valid, pe_msg = validate_pe_binary(self.installed_exe_path)
		if not pe_valid:
			state = "incompatible" if "architecture" in pe_msg else "corrupt"
			return CompanionStatus(
				installed=True,
				installed_version=installed_ver,
				installed_path=self.installed_exe_path,
				bundled_version=bundled_ver,
				bundled_available=bundled_avail,
				compatible=False,
				integrity_state=state,
				status_message=f"Installed companion is {state}: {pe_msg}",
			)

		state = "installed"
		if bundled_ver and installed_ver and installed_ver != bundled_ver:
			state = "outdated"

		return CompanionStatus(
			installed=True,
			installed_version=installed_ver,
			installed_path=self.installed_exe_path,
			bundled_version=bundled_ver,
			bundled_available=bundled_avail,
			compatible=True,
			integrity_state=state,
			status_message=f"Installed (v{installed_ver})",
		)

	def install_or_repair(
		self,
		force: bool = False,
		is_cancelled: Optional[Callable[[], bool]] = None,
		on_progress: Optional[Callable[[int, int, str], None]] = None,
	) -> Tuple[bool, str]:
		"""Safely install or repair the companion executable with complete 9-stage transaction.

		Transaction stages:
		1. Verify bundled executable and manifest
		2. Stage new binary to .tmp
		3. Verify staged integrity
		4. Stage installation metadata to .tmp
		5. Preserve previous installation (.old)
		6. Activate new installation
		7. Verify active installation on disk (triggers rollback on failure)
		8. Commit installation state
		9. Remove old backups only when safe

		Args:
			force: Whether to overwrite even if the same version is already installed.
			is_cancelled: Optional callback to check for user cancellation before activation.
			on_progress: Optional callback taking (current_stage, total_stages, message).

		Returns:
			Tuple of (success: bool, message: str)
		"""
		def report_progress(stage: int, msg: str):
			if on_progress:
				try:
					on_progress(stage, 9, msg)
				except Exception:
					pass

		# Stage 1: Verify bundled executable and manifest
		report_progress(1, "Verifying bundled companion artifact...")
		if is_cancelled and is_cancelled():
			return False, "Installation cancelled by user"

		valid, err_msg = self.verify_bundled_artifact()
		if not valid:
			return False, f"Bundled artifact verification failed: {err_msg}"

		manifest = self.get_bundled_manifest()
		assert manifest is not None
		binary_name = manifest.get("binary_name", "nvda-mcp-server.exe")
		bundled_binary_path = self.bundled_dir / binary_name
		version = manifest.get("version", "1.0.0")
		expected_sha = str(manifest.get("sha256", "")).strip().lower()

		# Check if already installed and verified up to date
		if not force:
			is_inst_valid, _, inst_state = self.verify_installed_artifact()
			if is_inst_valid and inst_state == "installed":
				return True, f"Companion v{version} is already installed and verified up to date"

		try:
			self.install_dir.mkdir(parents=True, exist_ok=True)
		except Exception as e:
			return False, f"Failed to create installation directory: {e}"

		staged_bin_path = self.install_dir / "nvda-mcp-server.exe.tmp"
		staged_manifest_path = self.install_dir / "installed_manifest.json.tmp"
		backup_bin_path = self.install_dir / "nvda-mcp-server.exe.old"
		backup_manifest_path = self.install_dir / "installed_manifest.json.old"

		# Clean up any leftover temp files
		for p in (staged_bin_path, staged_manifest_path):
			if p.is_file():
				try:
					p.unlink()
				except Exception:
					pass

		# Stage 2: Stage new binary to .tmp
		report_progress(2, "Staging new companion binary...")
		if is_cancelled and is_cancelled():
			return False, "Installation cancelled by user"

		try:
			shutil.copy2(bundled_binary_path, staged_bin_path)
		except Exception as e:
			return False, f"Failed to stage companion binary: {e}"

		# Stage 3: Verify staged integrity
		report_progress(3, "Verifying staged binary integrity...")
		staged_pe_valid, staged_pe_msg = validate_pe_binary(staged_bin_path, manifest.get("architecture", "windows-x64"))
		if not staged_pe_valid:
			staged_bin_path.unlink(missing_ok=True)
			return False, f"Staged binary validation failed: {staged_pe_msg}"

		staged_sha = calculate_sha256(staged_bin_path)
		if staged_sha != expected_sha:
			staged_bin_path.unlink(missing_ok=True)
			return False, "Staged binary checksum does not match bundled manifest"

		# Stage 4: Stage installation metadata to .tmp
		report_progress(4, "Staging installation metadata...")
		installed_meta = {
			"version": version,
			"architecture": manifest.get("architecture", "windows-x64"),
			"sha256": expected_sha,
			"installed_at_monotonic": time.monotonic(),
			"binary_path": str(self.installed_exe_path),
		}
		try:
			staged_manifest_path.write_text(json.dumps(installed_meta, indent=2), encoding="utf-8")
		except Exception as e:
			staged_bin_path.unlink(missing_ok=True)
			return False, f"Failed to stage installation metadata: {e}"

		# Stage 5: Preserve previous installation (.old)
		report_progress(5, "Preserving existing installation for rollback safety...")
		had_existing_bin = self.installed_exe_path.is_file()
		had_existing_manifest = self.installed_manifest_path.is_file()

		if backup_bin_path.is_file():
			try:
				backup_bin_path.unlink()
			except Exception:
				pass
		if backup_manifest_path.is_file():
			try:
				backup_manifest_path.unlink()
			except Exception:
				pass

		if had_existing_bin:
			try:
				self.installed_exe_path.rename(backup_bin_path)
			except (PermissionError, OSError) as e:
				staged_bin_path.unlink(missing_ok=True)
				staged_manifest_path.unlink(missing_ok=True)
				return False, (
					f"Cannot update companion: executable is locked by another running process ({e}). "
					"Please close any running MCP clients and try again."
				)

		if had_existing_manifest:
			try:
				self.installed_manifest_path.rename(backup_manifest_path)
			except Exception:
				pass

		# Stage 6: Activate new installation
		report_progress(6, "Activating companion binary...")
		try:
			staged_bin_path.rename(self.installed_exe_path)
		except Exception as activate_err:
			# Immediate rollback
			logHandler.log.error(f"Failed to activate staged companion: {activate_err}", exc_info=True)
			if had_existing_bin and backup_bin_path.is_file():
				try:
					backup_bin_path.rename(self.installed_exe_path)
				except Exception as rb_err:
					logHandler.log.error(f"Critical rollback failure on binary: {rb_err}")
			if had_existing_manifest and backup_manifest_path.is_file():
				try:
					backup_manifest_path.rename(self.installed_manifest_path)
				except Exception:
					pass
			staged_manifest_path.unlink(missing_ok=True)
			return False, f"Failed to activate companion binary: {activate_err}"

		# Stage 7: Verify active installation on disk
		report_progress(7, "Verifying active companion on disk...")
		active_pe_valid, active_pe_msg = validate_pe_binary(self.installed_exe_path, manifest.get("architecture", "windows-x64"))
		active_sha = calculate_sha256(self.installed_exe_path) if self.installed_exe_path.is_file() else ""

		if not active_pe_valid or active_sha != expected_sha:
			logHandler.log.error(
				f"Active installation verification failed: {active_pe_msg} (sha={active_sha[:8]} vs {expected_sha[:8]}); rolling back"
			)
			# Rollback to original files
			try:
				if self.installed_exe_path.is_file():
					self.installed_exe_path.unlink()
				if had_existing_bin and backup_bin_path.is_file():
					backup_bin_path.rename(self.installed_exe_path)
				if had_existing_manifest and backup_manifest_path.is_file():
					backup_manifest_path.rename(self.installed_manifest_path)
			except Exception as rb_err:
				logHandler.log.error(f"Rollback error: {rb_err}")
			staged_manifest_path.unlink(missing_ok=True)
			return False, "Active companion failed verification after placement; rolled back to previous installation."

		# Stage 8: Commit installation state
		report_progress(8, "Committing installation metadata...")
		try:
			staged_manifest_path.replace(self.installed_manifest_path)
		except Exception as e:
			logHandler.log.warning(f"Failed to commit installed companion manifest: {e}")

		# Stage 9: Remove old backups only when safe
		report_progress(9, "Finalizing installation...")
		for p in (backup_bin_path, backup_manifest_path):
			if p.is_file():
				try:
					p.unlink()
				except Exception:
					pass  # OS may clean up later if handles are releasing

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
