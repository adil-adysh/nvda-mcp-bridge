"""Shared bootstrap for tests that exercise the native log diagnostics package."""

from __future__ import annotations

import importlib
import os
import sys
import types

ADDON_DIR = os.path.normpath(
	os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "addon", "globalPlugins", "nvdaMcp-bridge")
)
PACKAGE_NAME = "nvdaMcpBridge"
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "nvda_sample.log")

if ADDON_DIR not in sys.path:
	sys.path.insert(0, ADDON_DIR)
if "logHandler" not in sys.modules:
	sys.modules["logHandler"] = types.SimpleNamespace(
		log=types.SimpleNamespace(
			debug=lambda *args, **kwargs: None,
			info=lambda *args, **kwargs: None,
			warning=lambda *args, **kwargs: None,
			error=lambda *args, **kwargs: None,
		)
	)
if PACKAGE_NAME not in sys.modules:
	package = types.ModuleType(PACKAGE_NAME)
	package.__path__ = [ADDON_DIR]
	sys.modules[PACKAGE_NAME] = package

log_diagnostics = importlib.import_module(f"{PACKAGE_NAME}.server.log_diagnostics")
