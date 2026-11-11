# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Unit tests for CompanionManager: installation, verification, rollback, and locking."""

import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

_ADDON_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "addon", "globalPlugins", "nvdaMcp-bridge")
)
if _ADDON_DIR not in sys.path:
    sys.path.insert(0, _ADDON_DIR)

_fake_logger = types.SimpleNamespace(
    debug=lambda *args, **kwargs: None,
    info=lambda *args, **kwargs: None,
    warning=lambda *args, **kwargs: None,
    error=lambda *args, **kwargs: None,
)
sys.modules["logHandler"] = types.SimpleNamespace(log=_fake_logger)

_PACKAGE_NAME = "nvdaMcpBridge"
if _PACKAGE_NAME not in sys.modules:
    _package = types.ModuleType(_PACKAGE_NAME)
    _package.__path__ = [_ADDON_DIR]
    sys.modules[_PACKAGE_NAME] = _package

from nvdaMcpBridge.companion_manager import CompanionManager, calculate_sha256  # noqa: E402


class TestCompanionManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bundled_dir = self.root / "bundled"
        self.install_dir = self.root / "installed"
        self.bundled_dir.mkdir(parents=True)
        self.install_dir.mkdir(parents=True)

        # Create a valid mock companion binary
        self.binary_content = b"MZ\x90\x00" + b"X" * 2048
        self.binary_sha = hashlib.sha256(self.binary_content).hexdigest().lower()

        self.mock_exe = self.bundled_dir / "nvda-mcp-server.exe"
        self.mock_exe.write_bytes(self.binary_content)

        self.manifest_data = {
            "binary_name": "nvda-mcp-server.exe",
            "version": "1.0.0",
            "architecture": "windows-x64",
            "min_bridge_api_level": 1,
            "sha256": self.binary_sha,
            "package_compatibility": {"min_nvda_version": "2026.1.0"},
        }
        self.manifest_file = self.bundled_dir / "manifest.json"
        self.manifest_file.write_text(json.dumps(self.manifest_data), encoding="utf-8")

        self.mgr = CompanionManager(install_dir=self.install_dir, bundled_dir=self.bundled_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_verify_bundled_artifact_success(self):
        """Verify valid bundled companion artifact passes verification."""
        valid, msg = self.mgr.verify_bundled_artifact()
        self.assertTrue(valid)
        self.assertIn("verified successfully", msg)

    def test_missing_binary(self):
        """Verify error when binary is missing from bundled directory."""
        self.mock_exe.unlink()
        valid, msg = self.mgr.verify_bundled_artifact()
        self.assertFalse(valid)
        self.assertIn("missing", msg)

    def test_corrupt_bundled_artifact_checksum_mismatch(self):
        """Verify detection of corrupt bundled binary with mismatched checksum."""
        # Alter content of binary
        self.mock_exe.write_bytes(b"MZ\x00corrupted_payload_here" + b"Y" * 2048)
        valid, msg = self.mgr.verify_bundled_artifact()
        self.assertFalse(valid)
        self.assertIn("checksum mismatch", msg)

    def test_missing_manifest(self):
        """Verify error when manifest.json is missing."""
        self.manifest_file.unlink()
        valid, msg = self.mgr.verify_bundled_artifact()
        self.assertFalse(valid)
        self.assertIn("manifest missing", msg)

    def test_clean_installation(self):
        """Verify clean first-time installation creates binary and installed_manifest."""
        status_before = self.mgr.inspect_installed()
        self.assertFalse(status_before.installed)

        success, msg = self.mgr.install_or_repair()
        self.assertTrue(success)
        self.assertIn("Successfully installed", msg)

        status_after = self.mgr.inspect_installed()
        self.assertTrue(status_after.installed)
        self.assertTrue(status_after.compatible)
        self.assertEqual(status_after.installed_version, "1.0.0")

        # Verify installed file
        self.assertTrue(self.mgr.installed_exe_path.is_file())
        self.assertEqual(calculate_sha256(self.mgr.installed_exe_path), self.binary_sha)

    def test_repeated_installation_up_to_date(self):
        """Verify repeated installation reports already up to date without redundant work."""
        self.mgr.install_or_repair()
        success, msg = self.mgr.install_or_repair(force=False)
        self.assertTrue(success)
        self.assertIn("already installed and up to date", msg)

    def test_repair_corrupt_installed_binary(self):
        """Verify repair replaces corrupt installed binary."""
        self.mgr.install_or_repair()
        # Corrupt installed binary
        self.mgr.installed_exe_path.write_bytes(b"bad")

        success, msg = self.mgr.install_or_repair(force=True)
        self.assertTrue(success)
        self.assertEqual(calculate_sha256(self.mgr.installed_exe_path), self.binary_sha)

    def test_failed_installation_and_rollback(self):
        """Verify rollback restores previous working version if stage activation fails."""
        # 1. Install version 1.0.0
        self.mgr.install_or_repair()
        orig_content = self.mgr.installed_exe_path.read_bytes()

        # 2. Simulate failure during replacement by making rename raise an exception
        real_rename = Path.rename

        def mock_rename(src, dst):
            if "nvda-mcp-server.exe.tmp" in str(src):
                raise OSError("Simulated disk error during activation")
            return real_rename(src, dst)

        Path.rename = mock_rename
        try:
            success, msg = self.mgr.install_or_repair(force=True)
            self.assertFalse(success)
            self.assertIn("Failed to activate", msg)
            # Verify rollback restored original binary
            self.assertTrue(self.mgr.installed_exe_path.is_file())
            self.assertEqual(self.mgr.installed_exe_path.read_bytes(), orig_content)
        finally:
            Path.rename = real_rename

    def test_uninstall_cleanly_removes_files(self):
        """Verify uninstall removes binary and manifest."""
        self.mgr.install_or_repair()
        self.assertTrue(self.mgr.installed_exe_path.is_file())

        success, msg = self.mgr.uninstall()
        self.assertTrue(success)
        self.assertFalse(self.mgr.installed_exe_path.is_file())
        self.assertFalse(self.mgr.installed_manifest_path.is_file())


if __name__ == "__main__":
    unittest.main()
