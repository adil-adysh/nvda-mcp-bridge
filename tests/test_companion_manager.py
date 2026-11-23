# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Unit tests for CompanionManager: installation, PE verification, rollback, and locking."""

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
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

from nvdaMcpBridge.companion_manager import (  # noqa: E402
    CompanionManager,
    calculate_sha256,
    is_companion_image_path,
    validate_pe_binary,
)


def make_mock_pe_binary(machine: int = 0x8664) -> bytes:
    """Generate a valid minimal mock PE binary with specified machine architecture."""
    dos = bytearray(64)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 128)  # pe_offset = 128
    pe = bytearray(2048)
    pe[0:4] = b"PE\x00\x00"
    struct.pack_into("<H", pe, 4, machine)  # machine type (0x8664 = x64, 0x014c = x86)
    return bytes(dos) + b"\x00" * 64 + bytes(pe)


class TestCompanionManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bundled_dir = self.root / "bundled"
        self.install_dir = self.root / "installed"
        self.bundled_dir.mkdir(parents=True)
        self.install_dir.mkdir(parents=True)

        # Create a valid mock x64 companion binary
        self.binary_content = make_mock_pe_binary(0x8664)
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
        self.assertTrue(valid, msg)
        self.assertIn("verified successfully", msg)

    def test_missing_binary(self):
        """Verify error when binary is missing from bundled directory."""
        self.mock_exe.unlink()
        valid, msg = self.mgr.verify_bundled_artifact()
        self.assertFalse(valid)
        self.assertIn("missing", msg)

    def test_corrupt_bundled_artifact_checksum_mismatch(self):
        """Verify detection of corrupt bundled binary with mismatched checksum."""
        # Alter content while keeping valid PE header
        alt_binary = make_mock_pe_binary(0x8664) + b"extra_tampered_bytes"
        self.mock_exe.write_bytes(alt_binary)
        valid, msg = self.mgr.verify_bundled_artifact()
        self.assertFalse(valid)
        self.assertIn("checksum mismatch", msg)

    def test_incompatible_architecture_rejected(self):
        """Verify 32-bit x86 binary is rejected as incompatible for x64 architecture."""
        x86_binary = make_mock_pe_binary(0x014C)  # IMAGE_FILE_MACHINE_I386
        self.mock_exe.write_bytes(x86_binary)
        self.manifest_data["sha256"] = hashlib.sha256(x86_binary).hexdigest().lower()
        self.manifest_file.write_text(json.dumps(self.manifest_data), encoding="utf-8")

        valid, msg = self.mgr.verify_bundled_artifact()
        self.assertFalse(valid)
        self.assertIn("Incompatible architecture", msg)

    def test_missing_manifest(self):
        """Verify error when manifest.json is missing."""
        self.manifest_file.unlink()
        valid, msg = self.mgr.verify_bundled_artifact()
        self.assertFalse(valid)
        self.assertIn("manifest missing", msg)

    def test_clean_installation(self):
        """Verify clean first-time installation creates binary and installed_manifest."""
        status_before = self.mgr.inspect_installed(verify_hash=False)
        self.assertFalse(status_before.installed)
        self.assertEqual(status_before.integrity_state, "missing")

        success, msg = self.mgr.install_or_repair()
        self.assertTrue(success, msg)
        self.assertIn("Successfully installed", msg)

        status_after = self.mgr.inspect_installed(verify_hash=True)
        self.assertTrue(status_after.installed)
        self.assertTrue(status_after.compatible)
        self.assertEqual(status_after.integrity_state, "installed")
        self.assertEqual(status_after.installed_version, "1.0.0")

        # Verify installed file
        self.assertTrue(self.mgr.installed_exe_path.is_file())
        self.assertEqual(calculate_sha256(self.mgr.installed_exe_path), self.binary_sha)

    def test_install_refreshes_token_only_after_verified_activation(self):
        refresh_token = mock.Mock(return_value="rotated-token")
        with mock.patch.dict(os.environ, {"NVDA_BRIDGE_TOKEN": ""}):
            success, msg = self.mgr.install_or_repair(refresh_token=refresh_token)
        self.assertTrue(success, msg)
        refresh_token.assert_called_once_with()
        self.assertIn("authentication token refreshed", msg.lower())

    def test_token_refresh_failure_rolls_back_the_previous_companion(self):
        self.mgr.install_or_repair()
        original_binary = self.mgr.installed_exe_path.read_bytes()
        original_manifest = self.mgr.installed_manifest_path.read_bytes()

        def fail_refresh():
            raise RuntimeError("token refresh failed")

        with mock.patch.dict(os.environ, {"NVDA_BRIDGE_TOKEN": ""}):
            success, msg = self.mgr.install_or_repair(force=True, refresh_token=fail_refresh)
        self.assertFalse(success)
        self.assertIn("token refresh failed", msg.lower())
        self.assertEqual(self.mgr.installed_exe_path.read_bytes(), original_binary)
        self.assertEqual(self.mgr.installed_manifest_path.read_bytes(), original_manifest)

    def test_environment_managed_token_aborts_before_stopping_or_replacing_companion(self):
        refresh_token = mock.Mock(return_value="rotated-token")
        self.mgr.stop_running_companions = mock.Mock(return_value=([123], []))
        with mock.patch.dict(os.environ, {"NVDA_BRIDGE_TOKEN": "managed-outside-addon"}):
            success, msg = self.mgr.install_or_repair(force=True, refresh_token=refresh_token)

        self.assertFalse(success)
        self.assertIn("NVDA_BRIDGE_TOKEN", msg)
        self.assertFalse(self.mgr.installed_exe_path.exists())
        self.mgr.stop_running_companions.assert_not_called()
        refresh_token.assert_not_called()

    def test_repeated_installation_up_to_date(self):
        """Verify repeated installation reports already up to date without redundant work."""
        self.mgr.install_or_repair()
        success, msg = self.mgr.install_or_repair(force=False)
        self.assertTrue(success)
        self.assertIn("already installed and verified up to date", msg)

    def test_repair_corrupt_installed_binary(self):
        """Verify repair replaces corrupt installed binary."""
        self.mgr.install_or_repair()

        # Corrupt installed binary by replacing with invalid data
        self.mgr.installed_exe_path.write_bytes(b"corrupted_binary_data")

        # In-depth inspection should detect corruption
        status_corrupt = self.mgr.inspect_installed(verify_hash=True)
        self.assertFalse(status_corrupt.compatible)
        self.assertEqual(status_corrupt.integrity_state, "corrupt")

        # Force repair should restore working binary
        success, msg = self.mgr.install_or_repair(force=True)
        self.assertTrue(success)
        self.assertEqual(calculate_sha256(self.mgr.installed_exe_path), self.binary_sha)

        # Status after repair should be clean
        status_repaired = self.mgr.inspect_installed(verify_hash=True)
        self.assertTrue(status_repaired.compatible)
        self.assertEqual(status_repaired.integrity_state, "installed")

    def test_failed_installation_and_rollback_on_activation(self):
        """Verify rollback restores previous working version if stage activation fails."""
        # 1. Install initial version
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

    def test_failed_active_verification_triggers_rollback(self):
        """Verify rollback occurs if active binary fails checksum verification after placement."""
        # 1. Install initial version
        self.mgr.install_or_repair()
        orig_content = self.mgr.installed_exe_path.read_bytes()

        # 2. Simulate silent disk corruption by tampering with staged binary after verification
        # By corrupting bundled artifact temporarily during a forced upgrade
        bad_pe = make_mock_pe_binary(0x8664) + b"corrupted_after_check"
        bad_sha = hashlib.sha256(bad_pe).hexdigest().lower()

        # Update manifest to expect bad_sha so stage passes, but mock calculate_sha256 to fail on active verification
        self.mgr.get_bundled_manifest = lambda: {
            "binary_name": "nvda-mcp-server.exe",
            "version": "1.0.1",
            "architecture": "windows-x64",
            "sha256": bad_sha,
        }
        self.mock_exe.write_bytes(bad_pe)

        real_calc = calculate_sha256

        # Make active verification return mismatched sha
        def mock_calc(path):
            if path == self.mgr.installed_exe_path:
                return "0000000000000000000000000000000000000000000000000000000000000000"
            return real_calc(path)

        import nvdaMcpBridge.companion_manager as cm_mod
        orig_fn = cm_mod.calculate_sha256
        cm_mod.calculate_sha256 = mock_calc

        try:
            success, msg = self.mgr.install_or_repair(force=True)
            self.assertFalse(success)
            self.assertIn("rolled back", msg)
            # Original binary was restored
            self.assertTrue(self.mgr.installed_exe_path.is_file())
            self.assertEqual(self.mgr.installed_exe_path.read_bytes(), orig_content)
        finally:
            cm_mod.calculate_sha256 = orig_fn

    def test_cancellation_before_activation(self):
        """Verify installation aborts cleanly without altering state when cancelled."""
        success, msg = self.mgr.install_or_repair(is_cancelled=lambda: True)
        self.assertFalse(success)
        self.assertIn("cancelled", msg)
        self.assertFalse(self.mgr.installed_exe_path.is_file())

    def test_uninstall_cleanly_removes_files(self):
        """Verify uninstall removes binary and manifest."""
        self.mgr.install_or_repair()
        self.assertTrue(self.mgr.installed_exe_path.is_file())

        success, msg = self.mgr.uninstall()
        self.assertTrue(success)
        self.assertFalse(self.mgr.installed_exe_path.is_file())
        self.assertFalse(self.mgr.installed_manifest_path.is_file())


class RunningCompanionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.install_dir = self.root / "installed"
        self.bundled_dir = self.root / "bundled"
        self.install_dir.mkdir(parents=True)
        self.bundled_dir.mkdir(parents=True)
        binary = make_mock_pe_binary(0x8664)
        (self.bundled_dir / "nvda-mcp-server.exe").write_bytes(binary)
        (self.bundled_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "binary_name": "nvda-mcp-server.exe",
                    "version": "1.0.0",
                    "architecture": "windows-x64",
                    "sha256": hashlib.sha256(binary).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        self.mgr = CompanionManager(install_dir=self.install_dir, bundled_dir=self.bundled_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_only_images_in_this_install_directory_match(self):
        install = self.install_dir
        self.assertTrue(is_companion_image_path(install / "nvda-mcp-server.exe", install))
        self.assertTrue(is_companion_image_path(install / "nvda-mcp-server.exe.old-0.10.0", install))
        self.assertFalse(is_companion_image_path(self.root / "other" / "nvda-mcp-server.exe", install))
        self.assertFalse(is_companion_image_path(install / "unrelated.exe", install))
        self.assertFalse(is_companion_image_path("", install))

    def test_install_stops_running_companions_and_reports_them(self):
        self.mgr.stop_running_companions = lambda: ([4242], [])
        success, msg = self.mgr.install_or_repair(force=True)
        self.assertTrue(success, msg)
        self.assertIn("Stopped 1 running companion process", msg)
        self.assertIn("must reconnect", msg)

    def test_a_process_that_cannot_be_stopped_aborts_before_replacement(self):
        self.mgr.stop_running_companions = lambda: ([], [(999, "access denied")])
        success, msg = self.mgr.install_or_repair(force=True)
        self.assertFalse(success)
        self.assertIn("PID 999", msg)
        self.assertFalse(self.mgr.installed_exe_path.exists(), "a failed stop must not leave a partial install")

    def test_an_unreadable_process_list_aborts_before_replacement(self):
        def fail():
            raise OSError("snapshot failed")

        self.mgr.find_running_companion_pids = fail
        success, msg = self.mgr.install_or_repair(force=True)
        self.assertFalse(success)
        self.assertIn("could not enumerate processes", msg)
        self.assertFalse(self.mgr.installed_exe_path.exists())

    def test_pid_reuse_during_shutdown_does_not_terminate_a_different_image(self):
        import ctypes

        import nvdaMcpBridge.companion_manager as cm_mod

        pid = 4242
        companion_image = str(self.install_dir / "nvda-mcp-server.exe")
        unrelated_image = str(self.root / "other" / "unrelated.exe")

        class FakeFunction:
            def __init__(self, callback):
                self.callback = callback

            def __call__(self, *args):
                return self.callback(*args)

        class FakeKernel32:
            def __init__(self):
                self.opened_handles = []
                self.queried_handles = []
                self.terminated_handles = []
                self.closed_handles = []
                self.images = {101: companion_image, 102: unrelated_image}
                self.OpenProcess = FakeFunction(self.open_process)
                self.QueryFullProcessImageNameW = FakeFunction(self.query_image)
                self.TerminateProcess = FakeFunction(self.terminate_process)
                self.WaitForSingleObject = FakeFunction(lambda handle, timeout: 0)
                self.CloseHandle = FakeFunction(self.close_handle)

            def open_process(self, access, inherit, requested_pid):
                handle = 101 if not self.opened_handles else 102
                self.opened_handles.append(handle)
                return handle

            def query_image(self, handle, flags, buffer, size):
                self.queried_handles.append(handle)
                buffer.value = self.images[handle]
                size._obj.value = len(buffer.value)
                return True

            def terminate_process(self, handle, exit_code):
                self.terminated_handles.append(handle)
                return True

            def close_handle(self, handle):
                self.closed_handles.append(handle)
                return True

        kernel32 = FakeKernel32()

        def find_candidate_pid():
            image = cm_mod._process_image_path(pid)
            return [pid] if cm_mod.is_companion_image_path(image, self.install_dir) else []

        self.mgr.find_running_companion_pids = find_candidate_pid
        with (
            mock.patch.object(cm_mod.os, "name", "nt"),
            mock.patch.object(ctypes, "WinDLL", return_value=kernel32, create=True),
        ):
            stopped, failures = self.mgr.stop_running_companions()

        self.assertEqual(kernel32.opened_handles, [101, 102])
        self.assertEqual(kernel32.terminated_handles, [])
        self.assertEqual(kernel32.queried_handles, [101, 102])
        self.assertEqual(kernel32.closed_handles, [101, 102])
        self.assertNotIn(pid, stopped)
        self.assertEqual(failures, [(pid, "process image changed since discovery")])

    @unittest.skipUnless(os.name == "nt", "Windows process enumeration")
    def test_a_real_process_from_the_install_directory_is_found_and_stopped(self):
        source = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "ping.exe"
        if not source.is_file():
            self.skipTest("ping.exe not available")
        dummy = self.install_dir / "nvda-mcp-server.exe"
        shutil.copy2(source, dummy)
        process = subprocess.Popen(
            [str(dummy), "-n", "30", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and process.pid not in self.mgr.find_running_companion_pids():
                time.sleep(0.05)
            self.assertIn(process.pid, self.mgr.find_running_companion_pids())
            stopped, failures = self.mgr.stop_running_companions()
            self.assertIn(process.pid, stopped)
            self.assertEqual(failures, [])
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    @unittest.skipUnless(os.name == "nt", "Windows process enumeration")
    def test_install_replaces_a_binary_while_its_process_is_running(self):
        source = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "ping.exe"
        if not source.is_file():
            self.skipTest("ping.exe not available")
        self.mgr.install_or_repair()
        shutil.copy2(source, self.mgr.installed_exe_path)
        process = subprocess.Popen(
            [str(self.mgr.installed_exe_path), "-n", "30", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and process.pid not in self.mgr.find_running_companion_pids():
                time.sleep(0.05)
            success, msg = self.mgr.install_or_repair(force=True)
            self.assertTrue(success, msg)
            self.assertIn("Stopped 1 running companion process", msg)
            self.assertIsNotNone(process.poll(), "the old process must be gone")
            self.assertEqual(calculate_sha256(self.mgr.installed_exe_path), hashlib.sha256(make_mock_pe_binary()).hexdigest())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
