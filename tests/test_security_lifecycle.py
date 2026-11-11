# pyright: reportMissingImports=false, reportUnusedCallResult=false

import json
import os
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

# Setup paths and NVDA stubs
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

_queue_handler = types.ModuleType("queueHandler")
_queue_handler.queueFunction = lambda queue, func, *args, **kwargs: func(*args, **kwargs)
sys.modules["queueHandler"] = _queue_handler

_PACKAGE_NAME = "nvdaMcpBridge"
if _PACKAGE_NAME not in sys.modules:
    _package = types.ModuleType(_PACKAGE_NAME)
    _package.__path__ = [_ADDON_DIR]
    sys.modules[_PACKAGE_NAME] = _package

import importlib
security_module = importlib.import_module(f"{_PACKAGE_NAME}.server.security")
http_bridge_module = importlib.import_module(f"{_PACKAGE_NAME}.server.http_bridge")
base_module = importlib.import_module(f"{_PACKAGE_NAME}.core.base")

PERMISSION_DEVELOPER = security_module.PERMISSION_DEVELOPER
PERMISSION_READ_ONLY = security_module.PERMISSION_READ_ONLY
PERMISSION_NVDA_LIFECYCLE = security_module.PERMISSION_NVDA_LIFECYCLE
DeveloperModeManager = security_module.DeveloperModeManager
check_action_permissions = security_module.check_action_permissions
constant_time_compare = security_module.constant_time_compare
get_or_create_bridge_token = security_module.get_or_create_bridge_token

NVDAHTTPBridge = http_bridge_module.NVDAHTTPBridge
ToolMetadata = base_module.ToolMetadata


class SecurityLifecycleTests(unittest.TestCase):
    def test_developer_mode_manager_lifecycle(self):
        mgr = DeveloperModeManager()
        self.assertFalse(mgr.is_enabled, "Developer mode must be disabled by default")

        mgr.enable()
        self.assertTrue(mgr.is_enabled, "Developer mode should be enabled after enable()")

        mgr.disable()
        self.assertFalse(mgr.is_enabled, "Developer mode should be disabled after disable()")

        mgr.enable()
        mgr.reset()
        self.assertFalse(mgr.is_enabled, "Developer mode should be reset to disabled")

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

        # Normal desktop, dev mode disabled -> BLOCKED (4003)
        allowed, code, msg = check_action_permissions(
            action_name="execute_python",
            metadata=dummy_meta,
            nvda_status_snapshot={"secure_screen": False, "modal_blocking": False, "menu_open": False},
            dev_mode_enabled=False,
        )
        self.assertFalse(allowed)
        self.assertEqual(code, 4003)

        # Normal desktop, dev mode enabled -> ALLOWED
        allowed, _, _ = check_action_permissions(
            action_name="execute_python",
            metadata=dummy_meta,
            nvda_status_snapshot={"secure_screen": False, "modal_blocking": False, "menu_open": False},
            dev_mode_enabled=True,
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

        allowed, code, msg = check_action_permissions(
            action_name="execute_python",
            metadata=dummy_meta,
            nvda_status_snapshot={"secure_screen": True, "modal_blocking": False, "menu_open": False},
            dev_mode_enabled=True,
        )
        self.assertFalse(allowed)
        self.assertEqual(code, 4201)
        self.assertIn("secure desktop", msg.lower())

    def test_token_generation_creates_strong_key_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            orig_appdata = os.environ.get("APPDATA")
            orig_token = os.environ.get("NVDA_BRIDGE_TOKEN")
            try:
                os.environ["APPDATA"] = temp_dir
                os.environ.pop("NVDA_BRIDGE_TOKEN", None)
                token = get_or_create_bridge_token()
                self.assertGreaterEqual(len(token), 32)

                token_file = Path(temp_dir) / "nvda" / "nvda-mcp-bridge" / "token.key"
                self.assertTrue(token_file.exists())
                self.assertEqual(token_file.read_text(encoding="utf-8").strip(), token)

                # Second call should load the same saved token
                token2 = get_or_create_bridge_token()
                self.assertEqual(token, token2)
            finally:
                if orig_appdata is not None:
                    os.environ["APPDATA"] = orig_appdata
                else:
                    os.environ.pop("APPDATA", None)
                if orig_token is not None:
                    os.environ["NVDA_BRIDGE_TOKEN"] = orig_token


class HttpSecurityIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import HTTPServer
        NVDABridgeRequestHandler = http_bridge_module.NVDABridgeRequestHandler

        cls.token = "secure-test-token-abcdef1234567890"
        cls.ready_event = threading.Event()
        cls.bridge = NVDAHTTPBridge(host="127.0.0.1", port=0, bridge_token=cls.token)

        def _run_server():
            cls.bridge.server = HTTPServer((cls.bridge.host, 0), NVDABridgeRequestHandler)
            cls.bridge.server.timeout = 1.0
            cls.bridge.server.bridge_token = cls.bridge.bridge_token
            cls.bridge.server.developer_mode_manager = cls.bridge.developer_mode_manager
            cls.bridge.server.execute_action = cls.bridge.execute_action
            cls.bridge.server.list_tools = cls.bridge._list_tools
            cls.bridge.server.list_output_types = cls.bridge._list_output_types
            cls.bridge.server.get_nvda_status = cls.bridge.get_nvda_status
            cls.bridge.server.get_nvda_status_snapshot = cls.bridge.get_nvda_status_snapshot
            cls.bridge.server.get_health_payload = cls.bridge.get_health_payload
            cls.port = cls.bridge.server.server_port
            cls.base_url = f"http://127.0.0.1:{cls.port}"
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

    def test_developer_mode_toggle_endpoint(self):
        # By default developer mode is disabled
        self.assertFalse(self.bridge.is_developer_mode_enabled)

        # Enable developer mode via /admin/developer-mode
        req = urllib.request.Request(
            f"{self.base_url}/admin/developer-mode",
            data=json.dumps({"enabled": True}).encode("utf-8"),
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

        # Disable developer mode
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


if __name__ == "__main__":
    unittest.main()
