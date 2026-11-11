"""Comprehensive security, authentication, and permission lifecycle tests.

Verifies:
1. Per-installation token generation and fail-closed persistence
2. Developer mode lifecycle, independent local approval, and TTL expiration
3. Capability-based permission policy enforcement and runtime revocation
4. Secure desktop boundary enforcement
5. HTTP authentication, authorization, Host/Origin headers, and endpoint isolation
"""

import importlib
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

# Setup synthetic package root for relative imports
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ADDON_DIR = _PROJECT_ROOT / "addon" / "globalPlugins" / "nvdaMcp-bridge"
_PACKAGE_NAME = "nvdaMcpBridge"

if "logHandler" not in sys.modules:
    _fake_logger = types.SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )
    sys.modules["logHandler"] = types.SimpleNamespace(log=_fake_logger)

if "queueHandler" not in sys.modules:
    _queue_handler = types.ModuleType("queueHandler")
    _queue_handler.eventQueue = "dummy-queue"
    _queue_handler.queueFunction = lambda queue, func, *args, **kwargs: func(*args, **kwargs)
    sys.modules["queueHandler"] = _queue_handler

if _PACKAGE_NAME not in sys.modules:
    pkg = types.ModuleType(_PACKAGE_NAME)
    pkg.__path__ = [str(_ADDON_DIR)]
    sys.modules[_PACKAGE_NAME] = pkg

security_module = importlib.import_module(f"{_PACKAGE_NAME}.server.security")
http_bridge_module = importlib.import_module(f"{_PACKAGE_NAME}.server.http_bridge")
base_module = importlib.import_module(f"{_PACKAGE_NAME}.core.base")

PERMISSION_READ_ONLY = security_module.PERMISSION_READ_ONLY
PERMISSION_INPUT_CONTROL = security_module.PERMISSION_INPUT_CONTROL
PERMISSION_SPEECH_OUTPUT = security_module.PERMISSION_SPEECH_OUTPUT
PERMISSION_DIAGNOSTICS = security_module.PERMISSION_DIAGNOSTICS
PERMISSION_NVDA_LIFECYCLE = security_module.PERMISSION_NVDA_LIFECYCLE
PERMISSION_DEVELOPER = security_module.PERMISSION_DEVELOPER
ALL_PERMISSIONS = security_module.ALL_PERMISSIONS
DEFAULT_PERMISSIONS = security_module.DEFAULT_PERMISSIONS

PermissionPolicy = security_module.PermissionPolicy
DeveloperModeManager = security_module.DeveloperModeManager
check_action_permissions = security_module.check_action_permissions
constant_time_compare = security_module.constant_time_compare
get_or_create_bridge_token = security_module.get_or_create_bridge_token
resolve_token_file_path = security_module.resolve_token_file_path

NVDAHTTPBridge = http_bridge_module.NVDAHTTPBridge
ToolMetadata = base_module.ToolMetadata


class SecurityLifecycleTests(unittest.TestCase):
    def test_developer_mode_manager_lifecycle_and_expiration(self):
        mgr = DeveloperModeManager(default_ttl_seconds=1)
        self.assertFalse(mgr.is_enabled, "Developer mode must be disabled by default")

        # Local approval enables it
        mgr.approve_locally(duration_seconds=1)
        self.assertTrue(mgr.is_enabled, "Developer mode should be active after approval")
        self.assertGreater(mgr.time_remaining_seconds, 0.0)

        # Immediate disable works
        mgr.disable()
        self.assertFalse(mgr.is_enabled, "Developer mode should be disabled after disable()")

        # Reset works
        mgr.approve_locally(duration_seconds=10)
        mgr.reset()
        self.assertFalse(mgr.is_enabled, "Developer mode should be reset to disabled")

        # TTL expiration works
        mgr.approve_locally(duration_seconds=1)
        self.assertTrue(mgr.is_enabled)
        time.sleep(1.1)
        self.assertFalse(mgr.is_enabled, "Developer mode should automatically expire after TTL")

    def test_developer_mode_token_approval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = DeveloperModeManager()
            token = mgr.generate_local_approval_token(ttl_seconds=60, custom_dir=tmpdir)
            self.assertTrue(token and len(token) >= 16)

            # Approving with wrong token fails
            self.assertFalse(mgr.approve_with_token("wrong-token"))
            self.assertFalse(mgr.is_enabled)

            # Approving with correct token succeeds and activates
            self.assertTrue(mgr.approve_with_token(token, duration_seconds=60))
            self.assertTrue(mgr.is_enabled)

            # Single-use: cannot be reused
            self.assertFalse(mgr.approve_with_token(token))

    def test_permission_policy_lifecycle(self):
        policy = PermissionPolicy()
        # Defaults to read_only and speech_output
        self.assertTrue(policy.is_granted(PERMISSION_READ_ONLY))
        self.assertTrue(policy.is_granted(PERMISSION_SPEECH_OUTPUT))
        self.assertFalse(policy.is_granted(PERMISSION_INPUT_CONTROL))
        self.assertFalse(policy.is_granted(PERMISSION_DIAGNOSTICS))
        self.assertFalse(policy.is_granted(PERMISSION_NVDA_LIFECYCLE))
        self.assertFalse(policy.is_granted(PERMISSION_DEVELOPER))

        # Explicit grant
        policy.grant(PERMISSION_INPUT_CONTROL)
        self.assertTrue(policy.is_granted(PERMISSION_INPUT_CONTROL))

        # Explicit revoke takes immediate effect
        policy.revoke(PERMISSION_INPUT_CONTROL)
        self.assertFalse(policy.is_granted(PERMISSION_INPUT_CONTROL))

        # Unknown permission raises
        with self.assertRaises(ValueError):
            policy.grant("super_admin_all_access")

    def test_constant_time_compare(self):
        self.assertTrue(constant_time_compare("correct-token-12345", "correct-token-12345"))
        self.assertFalse(constant_time_compare("correct-token-12345", "wrong-token-99999"))
        self.assertFalse(constant_time_compare("short", "short-but-longer"))
        self.assertFalse(constant_time_compare("", "token"))
        self.assertFalse(constant_time_compare("token", ""))

    def test_execute_python_blocked_without_dev_mode(self):
        dummy_meta = ToolMetadata(
            name="execute_python",
            version="1.0.0",
            description="Run arbitrary Python",
            category="developer",
            required_permission=PERMISSION_DEVELOPER,
            destructive=True,
            allowed_nvda_states=("normal",),
        )

        policy = PermissionPolicy(granted_permissions=ALL_PERMISSIONS)

        # Normal desktop, dev mode disabled -> BLOCKED (4003)
        allowed, code, msg = check_action_permissions(
            action_name="execute_python",
            metadata=dummy_meta,
            nvda_status_snapshot={"secure_screen": False, "modal_blocking": False, "menu_open": False},
            dev_mode_enabled=False,
            permission_policy=policy,
        )
        self.assertFalse(allowed)
        self.assertEqual(code, 4003)

        # Dev mode enabled -> ALLOWED
        allowed, code, msg = check_action_permissions(
            action_name="execute_python",
            metadata=dummy_meta,
            nvda_status_snapshot={"secure_screen": False, "modal_blocking": False, "menu_open": False},
            dev_mode_enabled=True,
            permission_policy=policy,
        )
        self.assertTrue(allowed)

    def test_execute_python_blocked_on_secure_desktop_even_if_dev_mode_enabled(self):
        dummy_meta = ToolMetadata(
            name="execute_python",
            version="1.0.0",
            description="Run arbitrary Python",
            category="developer",
            required_permission=PERMISSION_DEVELOPER,
            destructive=True,
            allowed_nvda_states=("normal",),
        )
        policy = PermissionPolicy(granted_permissions=ALL_PERMISSIONS)

        # Secure desktop active -> MUST BE BLOCKED (4201) even if dev mode is enabled
        allowed, code, msg = check_action_permissions(
            action_name="execute_python",
            metadata=dummy_meta,
            nvda_status_snapshot={"secure_screen": True, "modal_blocking": False, "menu_open": False},
            dev_mode_enabled=True,
            permission_policy=policy,
        )
        self.assertFalse(allowed)
        self.assertEqual(code, 4201)

    def test_permission_policy_enforcement_at_bridge_boundary(self):
        kb_meta = ToolMetadata(
            name="send_keyboard",
            version="1.0.0",
            description="Send keyboard input",
            category="input",
            required_permission=PERMISSION_INPUT_CONTROL,
            allowed_nvda_states=("normal",),
        )

        policy = PermissionPolicy()  # default: only read_only and speech_output
        snapshot = {"secure_screen": False, "modal_blocking": False, "menu_open": False}

        # Blocked without grant
        allowed, code, msg = check_action_permissions(
            action_name="send_keyboard",
            metadata=kb_meta,
            nvda_status_snapshot=snapshot,
            dev_mode_enabled=False,
            permission_policy=policy,
        )
        self.assertFalse(allowed)
        self.assertEqual(code, 4003)
        self.assertIn("input_control", msg)

        # Allowed after grant
        policy.grant(PERMISSION_INPUT_CONTROL)
        allowed, code, msg = check_action_permissions(
            action_name="send_keyboard",
            metadata=kb_meta,
            nvda_status_snapshot=snapshot,
            dev_mode_enabled=False,
            permission_policy=policy,
        )
        self.assertTrue(allowed)

        # Blocked immediately after revocation
        policy.revoke(PERMISSION_INPUT_CONTROL)
        allowed, code, msg = check_action_permissions(
            action_name="send_keyboard",
            metadata=kb_meta,
            nvda_status_snapshot=snapshot,
            dev_mode_enabled=False,
            permission_policy=policy,
        )
        self.assertFalse(allowed)

    def test_token_generation_creates_strong_key_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            token = get_or_create_bridge_token(custom_dir=tmpdir)
            self.assertTrue(isinstance(token, str))
            self.assertGreaterEqual(len(token), 32)
            self.assertNotEqual(token, "nvda-bridge-secret-change-me")

            key_file = Path(tmpdir) / "token.key"
            self.assertTrue(key_file.is_file())
            content = key_file.read_text(encoding="utf-8").strip()
            self.assertEqual(token, content)

            # Re-read returns identical token without re-generating
            second_read = get_or_create_bridge_token(custom_dir=tmpdir)
            self.assertEqual(token, second_read)

    def test_token_generation_fails_closed_on_unwritable_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_blocker = Path(tmpdir) / "file_blocker.txt"
            file_blocker.write_text("i am a file", encoding="utf-8")
            invalid_path = file_blocker / "sub_dir"
            with self.assertRaises(RuntimeError):
                get_or_create_bridge_token(custom_dir=invalid_path)


class HttpSecurityIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.token = "test-secret-token-xyz-987654321"
        cls.ready_event = threading.Event()
        cls.port = 18765
        cls.base_url = f"http://127.0.0.1:{cls.port}"

        # Grant read_only, speech_output, and input_control for integration testing
        policy = PermissionPolicy(
            granted_permissions=[PERMISSION_READ_ONLY, PERMISSION_SPEECH_OUTPUT, PERMISSION_INPUT_CONTROL]
        )

        cls.bridge = NVDAHTTPBridge(
            host="127.0.0.1",
            port=cls.port,
            bridge_token=cls.token,
            developer_mode=False,
            permission_policy=policy,
        )

        def _run_server():
            from http.server import HTTPServer
            cls.bridge.server = HTTPServer((cls.bridge.host, cls.bridge.port), http_bridge_module.NVDABridgeRequestHandler)
            cls.bridge.server.bridge_token = cls.bridge.bridge_token
            cls.bridge.server.developer_mode_manager = cls.bridge.developer_mode_manager
            cls.bridge.server.permission_policy = cls.bridge.permission_policy
            cls.bridge.server.execute_action = cls.bridge.execute_action
            cls.bridge.server.list_tools = cls.bridge._list_tools
            cls.bridge.server.list_output_types = cls.bridge._list_output_types
            cls.bridge.server.get_nvda_status = cls.bridge.get_nvda_status
            cls.bridge.server.get_nvda_status_snapshot = cls.bridge.get_nvda_status_snapshot
            cls.bridge.server.get_health_payload = cls.bridge.get_health_payload
            cls.ready_event.set()

            while not cls.bridge.shutdown_event.is_set():
                try:
                    cls.bridge.server.handle_request()
                except Exception:
                    pass

        cls.server_thread = threading.Thread(target=_run_server, daemon=True)
        cls.server_thread.start()
        cls.ready_event.wait(timeout=5.0)

    @classmethod
    def tearDownClass(cls):
        cls.bridge.shutdown_event.set()
        if cls.bridge.server:
            try:
                req = urllib.request.Request(f"{cls.base_url}/health")
                urllib.request.urlopen(req, timeout=0.2)
            except Exception:
                pass
        cls.server_thread.join(timeout=2.0)

    def test_unauthorized_access_rejected_with_401(self):
        # 1. No Authorization header
        req = urllib.request.Request(f"{self.base_url}/tools")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 401)

        # 2. Invalid Authorization header
        req = urllib.request.Request(
            f"{self.base_url}/tools",
            headers={"Authorization": "Bearer wrong-token-xyz"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 401)

    def test_authorized_access_accepted(self):
        req = urllib.request.Request(
            f"{self.base_url}/tools",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["protocol"], "nvda-bridge-protocol")

    def test_forbidden_host_header_rejected_with_403(self):
        req = urllib.request.Request(
            f"{self.base_url}/tools",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Host": "attacker.com",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 403)

    def test_forbidden_origin_header_rejected_with_403(self):
        req = urllib.request.Request(
            f"{self.base_url}/action",
            data=json.dumps({"action": "get_current_focus", "params": {}}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Origin": "http://malicious-website.com",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 403)

    def test_developer_mode_requires_independent_local_approval(self):
        # 1. By default developer mode is disabled
        self.assertFalse(self.bridge.is_developer_mode_enabled)

        # 2. Ordinary bridge token CANNOT self-enable developer mode -> 403 Forbidden
        req = urllib.request.Request(
            f"{self.base_url}/admin/developer-mode",
            data=json.dumps({"enabled": True}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 403)
        self.assertFalse(self.bridge.is_developer_mode_enabled)

        # 3. Generating a local approval token allows elevation
        with tempfile.TemporaryDirectory() as tmpdir:
            approval_token = self.bridge.developer_mode_manager.generate_local_approval_token(
                ttl_seconds=60, custom_dir=tmpdir
            )

            req = urllib.request.Request(
                f"{self.base_url}/admin/developer-mode",
                data=json.dumps({"enabled": True, "local_approval_token": approval_token}).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                res = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(res["developer_mode"])

            self.assertTrue(self.bridge.is_developer_mode_enabled)

            # 4. Disabling developer mode via HTTP is always permitted
            req = urllib.request.Request(
                f"{self.base_url}/admin/developer-mode",
                data=json.dumps({"enabled": False}).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                res = json.loads(resp.read().decode("utf-8"))
                self.assertFalse(res["developer_mode"])

            self.assertFalse(self.bridge.is_developer_mode_enabled)

    def test_tools_discovery_reflects_developer_mode(self):
        # When developer mode is disabled, execute_python is omitted
        self.bridge.disable_developer_mode()
        tools_disabled = self.bridge._build_tools_list()
        tool_names = [t["name"] for t in tools_disabled]
        self.assertNotIn("execute_python", tool_names)

        # When developer mode is enabled, execute_python is exposed
        self.bridge.developer_mode_manager.approve_locally(duration_seconds=60)
        tools_enabled = self.bridge._build_tools_list()
        tool_names = [t["name"] for t in tools_enabled]
        self.assertIn("execute_python", tool_names)

        # Clean up
        self.bridge.disable_developer_mode()


if __name__ == "__main__":
    unittest.main()
