# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Unit tests for ClientConfigurator: MCP client detection, merging, backups, schemas, and conflicts."""

import json
import os
import struct
import sys
import tempfile
import tomllib
import types
import unittest
from pathlib import Path
from unittest import mock

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

from nvdaMcpBridge import client_config  # noqa: E402
from nvdaMcpBridge.client_config import (  # noqa: E402
    configure_client,
    detect_supported_clients,
    format_preview_text,
    preview_client_configuration,
)


def make_mock_pe_binary() -> bytes:
    """Generate a valid minimal mock x64 PE binary."""
    dos = bytearray(64)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 128)  # pe_offset = 128
    pe = bytearray(2048)
    pe[0:4] = b"PE\x00\x00"
    struct.pack_into("<H", pe, 4, 0x8664)  # machine = IMAGE_FILE_MACHINE_AMD64 (x86_64)
    return bytes(dos) + b"\x00" * 64 + bytes(pe)


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
        self.mock_exe.write_bytes(make_mock_pe_binary())

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
        self.assertEqual(claude.target_key, "mcpServers")

        vscode = next((c for c in clients if c.client_id == "vscode"), None)
        self.assertIsNotNone(vscode)
        self.assertEqual(vscode.target_key, "servers")

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

    def test_vscode_official_schema_uses_servers_and_stdio_type(self):
        """Verify VS Code configuration uses official 'servers' key with type: 'stdio'."""
        config_file = self.appdata / "Code" / "User" / "mcp.json"
        config_file.parent.mkdir(parents=True)

        initial_config = {
            "servers": {
                "git": {
                    "type": "stdio",
                    "command": "git-mcp",
                }
            }
        }
        config_file.write_text(json.dumps(initial_config, indent=2), encoding="utf-8")

        success, msg = configure_client(
            client_id="vscode",
            companion_exe_path=self.mock_exe,
            appdata_override=self.appdata,
            userprofile_override=self.userprofile,
        )
        self.assertTrue(success)

        updated = json.loads(config_file.read_text(encoding="utf-8"))
        self.assertIn("servers", updated)
        self.assertIn("git", updated["servers"])
        self.assertIn("nvda", updated["servers"])
        self.assertEqual(updated["servers"]["nvda"]["type"], "stdio")
        self.assertEqual(updated["servers"]["nvda"]["command"], str(self.mock_exe.resolve()))

    def test_conflicting_server_rejected_without_overwrite_flag(self):
        """Verify conflicting 'nvda' server definition is rejected when overwrite_conflict=False."""
        config_file = self.appdata / "Code" / "User" / "mcp.json"
        config_file.parent.mkdir(parents=True)

        conflict_config = {
            "servers": {
                "nvda": {
                    "type": "stdio",
                    "command": "C:\\custom\\other-binary.exe",
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
        self.assertEqual(updated["servers"]["nvda"]["command"], str(self.mock_exe.resolve()))
        self.assertEqual(updated["servers"]["nvda"]["type"], "stdio")

    def test_preview_generation_and_format(self):
        """Verify preview_client_configuration returns accurate diff and backup information."""
        config_file = self.appdata / "Claude" / "claude_desktop_config.json"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

        ok, preview, msg = preview_client_configuration(
            client_id="claude_desktop",
            companion_exe_path=self.mock_exe,
            appdata_override=self.appdata,
            userprofile_override=self.userprofile,
        )
        self.assertTrue(ok)
        self.assertEqual(preview.name, "Claude Desktop")
        self.assertEqual(preview.target_key, "mcpServers")
        self.assertFalse(preview.is_new_file)
        self.assertFalse(preview.has_conflict)
        self.assertIsNotNone(preview.backup_path)

        preview_text = format_preview_text(preview)
        self.assertIn("Claude Desktop", preview_text)
        self.assertIn("mcpServers", preview_text)
        self.assertIn("Proposed Server Entry", preview_text)

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
        self.assertIn("Failed to parse existing configuration", msg)


class TestCliAgentClients(unittest.TestCase):
    """Claude Code and Codex CLI both keep other state in the file we edit."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.appdata = self.root / "AppData" / "Roaming"
        self.userprofile = self.root / "UserProfile"
        self.appdata.mkdir(parents=True)
        self.userprofile.mkdir(parents=True)

        self.mock_exe = self.root / "bin" / "nvda-mcp-server.exe"
        self.mock_exe.parent.mkdir(parents=True)
        self.mock_exe.write_bytes(make_mock_pe_binary())
        self.resolved_exe = str(self.mock_exe.resolve())

    def tearDown(self):
        self.temp_dir.cleanup()

    def configure(self, client_id: str, **kwargs):
        return configure_client(
            client_id=client_id,
            companion_exe_path=self.mock_exe,
            appdata_override=self.appdata,
            userprofile_override=self.userprofile,
            **kwargs,
        )

    def test_claude_code_writes_user_scope_entry_and_keeps_history(self):
        """The top-level mcpServers object is user scope; everything else survives."""
        claude_json = self.userprofile / ".claude.json"
        original = {
            "numStartups": 7,
            "userID": "abc123",
            "projects": {"D:/repo": {"mcpServers": {}}},
        }
        claude_json.write_text(json.dumps(original), encoding="utf-8")

        success, msg = self.configure("claude_code")
        self.assertTrue(success, msg)

        updated = json.loads(claude_json.read_text(encoding="utf-8"))
        self.assertEqual(updated["mcpServers"]["nvda"]["command"], self.resolved_exe)
        self.assertEqual(updated["mcpServers"]["nvda"]["type"], "stdio")
        self.assertEqual(updated["mcpServers"]["nvda"]["args"], [])
        self.assertEqual(updated["mcpServers"]["nvda"]["env"], {})
        self.assertEqual(updated["numStartups"], 7)
        self.assertEqual(updated["userID"], "abc123")
        self.assertEqual(updated["projects"]["D:/repo"]["mcpServers"], {})

        backups = list(claude_json.parent.glob(".claude.json.bak.*"))
        self.assertEqual(len(backups), 1)

    def test_claude_code_detected_and_idempotent(self):
        claude_json = self.userprofile / ".claude.json"
        claude_json.write_text("{}", encoding="utf-8")

        detected = detect_supported_clients(
            appdata_override=self.appdata,
            userprofile_override=self.userprofile,
            expected_companion_exe=self.mock_exe,
        )
        claude_code = next(c for c in detected if c.client_id == "claude_code")
        self.assertTrue(claude_code.detected)
        self.assertFalse(claude_code.configured)

        self.assertTrue(self.configure("claude_code")[0])
        success, msg = self.configure("claude_code")
        self.assertTrue(success)
        self.assertIn("already configured", msg)

        after = next(
            c
            for c in detect_supported_clients(
                appdata_override=self.appdata,
                userprofile_override=self.userprofile,
                expected_companion_exe=self.mock_exe,
            )
            if c.client_id == "claude_code"
        )
        self.assertTrue(after.configured)

    def test_codex_appends_table_and_preserves_other_settings(self):
        config = self.userprofile / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            'model = "gpt-6.1-sol"\n'
            "\n"
            "# trust settings worth keeping\n"
            "[projects.'d:\\repo']\n"
            'trust_level = "trusted"\n'
            "\n"
            "[mcp_servers.other]\n"
            "command = 'other.exe'\n",
            encoding="utf-8",
        )

        success, msg = self.configure("codex")
        self.assertTrue(success, msg)

        text = config.read_text(encoding="utf-8")
        self.assertIn('model = "gpt-6.1-sol"', text)
        self.assertIn("# trust settings worth keeping", text)
        self.assertIn("[mcp_servers.other]", text)

        data = tomllib.loads(text)
        self.assertEqual(data["mcp_servers"]["nvda"]["command"], self.resolved_exe)
        self.assertNotIn("args", data["mcp_servers"]["nvda"])
        self.assertEqual(data["mcp_servers"]["other"]["command"], "other.exe")
        self.assertEqual(data["projects"]["d:\\repo"]["trust_level"], "trusted")
        self.assertEqual(data["model"], "gpt-6.1-sol")

    def test_codex_replaces_only_the_nvda_tables(self):
        config = self.userprofile / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            "[mcp_servers.nvda]\n"
            'command = "old-nvda.exe"\n'
            'args = ["--stale"]\n'
            "\n"
            "[mcp_servers.nvda.env]\n"
            'STALE = "1"\n'
            "\n"
            "[mcp_servers.other]\n"
            "command = 'other.exe'\n",
            encoding="utf-8",
        )

        success, msg = self.configure("codex", overwrite_conflict=True)
        self.assertTrue(success, msg)

        data = tomllib.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(data["mcp_servers"]["nvda"]["command"], self.resolved_exe)
        self.assertNotIn("args", data["mcp_servers"]["nvda"])
        self.assertNotIn("env", data["mcp_servers"]["nvda"])
        self.assertEqual(data["mcp_servers"]["other"]["command"], "other.exe")

    def test_codex_conflict_is_refused_without_overwrite(self):
        config = self.userprofile / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        original = "[mcp_servers.nvda]\ncommand = 'somewhere-else.exe'\n"
        config.write_text(original, encoding="utf-8")

        success, msg = self.configure("codex")
        self.assertFalse(success)
        self.assertIn("Conflict", msg)
        self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_codex_creates_file_when_only_the_directory_exists(self):
        (self.userprofile / ".codex").mkdir(parents=True)

        success, msg = self.configure("codex")
        self.assertTrue(success, msg)

        data = tomllib.loads((self.userprofile / ".codex" / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["mcp_servers"]["nvda"]["command"], self.resolved_exe)

    def test_codex_preview_renders_toml_not_json(self):
        config = self.userprofile / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('model = "gpt-6.1-sol"\n', encoding="utf-8")

        ok, preview, msg = preview_client_configuration(
            client_id="codex",
            companion_exe_path=self.mock_exe,
            appdata_override=self.appdata,
            userprofile_override=self.userprofile,
        )
        self.assertTrue(ok, msg)
        self.assertEqual(preview.name, "Codex CLI")
        text = format_preview_text(preview)
        self.assertIn("[mcp_servers.nvda]", text)
        self.assertIn(f"command = '{self.resolved_exe}'", text)

    def test_codex_invalid_toml_fails_closed(self):
        config = self.userprofile / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("[mcp_servers.nvda\ncommand = broken\n", encoding="utf-8")

        success, msg = self.configure("codex")
        self.assertFalse(success)
        self.assertIn("Failed to parse existing configuration", msg)

    def test_write_refused_when_file_changes_during_read(self):
        """A client rewriting its own state file must not be clobbered."""
        config = self.appdata / "Claude" / "claude_desktop_config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

        adapter = client_config.get_adapter_for_client("claude_desktop")
        real_render = adapter.render

        def racing_render(text, resolved_exe):
            config.write_text('{"mcpServers": {}, "touched": true}', encoding="utf-8")
            return real_render(text, resolved_exe)

        with mock.patch.object(adapter, "render", side_effect=racing_render):
            success, msg = self.configure("claude_desktop")

        self.assertFalse(success)
        self.assertIn("changed while it was being read", msg)
        self.assertEqual(
            json.loads(config.read_text(encoding="utf-8")),
            {"mcpServers": {}, "touched": True},
        )


if __name__ == "__main__":
    unittest.main()
