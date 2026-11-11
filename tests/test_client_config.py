# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Unit tests for ClientConfigurator: MCP client detection, merging, backups, and conflicts."""

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

from nvdaMcpBridge.client_config import (  # noqa: E402
    configure_client,
    detect_supported_clients,
    get_default_client_candidates,
)


class TestClientConfig(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.appdata = self.root / "AppData" / "Roaming"
        self.userprofile = self.root / "UserProfile"
        self.appdata.mkdir(parents=True)
        self.userprofile.mkdir(parents=True)

        self.mock_exe = self.root / "bin" / "nvda-mcp-server.exe"
        self.mock_exe.parent.mkdir(parents=True)
        self.mock_exe.write_bytes(b"MZ" + b"\x00" * 2000)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_detection_of_installed_clients(self):
        """Verify detect_supported_clients detects existing client configurations."""
        # Create Claude Desktop directory
        claude_dir = self.appdata / "Claude"
        claude_dir.mkdir(parents=True)

        clients = detect_supported_clients(
            appdata_override=self.appdata,
            userprofile_override=self.userprofile,
            expected_companion_exe=self.mock_exe,
        )

        claude = next((c for c in clients if c.client_id == "claude_desktop"), None)
        self.assertIsNotNone(claude)
        self.assertTrue(claude.detected)
        self.assertFalse(claude.configured)

    def test_configure_client_creates_valid_json_and_preserves_other_servers(self):
        """Verify configure_client merges nvda server without modifying existing servers."""
        config_file = self.appdata / "Claude" / "claude_desktop_config.json"
        config_file.parent.mkdir(parents=True)

        initial_config = {
            "mcpServers": {
                "fetch": {
                    "command": "uvx",
                    "args": ["mcp-server-fetch"],
                }
            },
            "unrelatedSetting": "preserved",
        }
        config_file.write_text(json.dumps(initial_config, indent=2), encoding="utf-8")

        success, msg = configure_client(
            client_id="claude_desktop",
            companion_exe_path=self.mock_exe,
            appdata_override=self.appdata,
            userprofile_override=self.userprofile,
        )
        self.assertTrue(success)

        # Read back and verify
        updated = json.loads(config_file.read_text(encoding="utf-8"))
        self.assertEqual(updated["unrelatedSetting"], "preserved")
        self.assertIn("fetch", updated["mcpServers"])
        self.assertIn("nvda", updated["mcpServers"])
        self.assertEqual(updated["mcpServers"]["nvda"]["command"], str(self.mock_exe.resolve()))
        self.assertEqual(updated["mcpServers"]["nvda"]["args"], [])

        # Verify backup was created
        backups = list(config_file.parent.glob("claude_desktop_config.json.bak.*"))
        self.assertEqual(len(backups), 1)

    def test_conflicting_server_rejected_without_overwrite_flag(self):
        """Verify conflicting 'nvda' server definition is rejected when overwrite_conflict=False."""
        config_file = self.appdata / "Code" / "User" / "mcp.json"
        config_file.parent.mkdir(parents=True)

        conflict_config = {
            "mcpServers": {
                "nvda": {
                    "command": "C:\\custom\\other-binary.exe",
                    "args": ["--foo"],
                }
            }
        }
        config_file.write_text(json.dumps(conflict_config), encoding="utf-8")

        success, msg = configure_client(
            client_id="vscode",
            companion_exe_path=self.mock_exe,
            appdata_override=self.appdata,
            userprofile_override=self.userprofile,
            overwrite_conflict=False,
        )
        self.assertFalse(success)
        self.assertIn("Conflict", msg)

        # With overwrite_conflict=True, it should succeed
        success2, msg2 = configure_client(
            client_id="vscode",
            companion_exe_path=self.mock_exe,
            appdata_override=self.appdata,
            userprofile_override=self.userprofile,
            overwrite_conflict=True,
        )
        self.assertTrue(success2)
        updated = json.loads(config_file.read_text(encoding="utf-8"))
        self.assertEqual(updated["mcpServers"]["nvda"]["command"], str(self.mock_exe.resolve()))

    def test_invalid_json_fails_closed(self):
        """Verify invalid existing JSON is not silently destroyed."""
        config_file = self.appdata / "Claude" / "claude_desktop_config.json"
        config_file.parent.mkdir(parents=True)
        config_file.write_text("{ malformed: json, here", encoding="utf-8")

        success, msg = configure_client(
            client_id="claude_desktop",
            companion_exe_path=self.mock_exe,
            appdata_override=self.appdata,
            userprofile_override=self.userprofile,
        )
        self.assertFalse(success)
        self.assertIn("Failed to parse existing JSON", msg)


if __name__ == "__main__":
    unittest.main()
