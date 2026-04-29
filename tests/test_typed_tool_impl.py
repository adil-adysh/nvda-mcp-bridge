# pyright: reportMissingImports=false, reportUnusedCallResult=false

import importlib
import os
import sys
import types
import unittest
from dataclasses import dataclass

# Add addon directory to path for imports
_ADDON_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "addon", "globalPlugins", "nvdaMcp-bridge")
)

# Build synthetic package namespace for the addon to support relative imports.
_PACKAGE_NAME = "nvdaMcpBridge"
_package = types.ModuleType(_PACKAGE_NAME)
_package.__path__ = [_ADDON_DIR]
sys.modules[_PACKAGE_NAME] = _package

if _ADDON_DIR not in sys.path:
    sys.path.insert(0, _ADDON_DIR)

core = importlib.import_module(f"{_PACKAGE_NAME}.core")

TypedToolImpl = core.TypedToolImpl
ToolMetadata = core.ToolMetadata
ToolExecutor = core.ToolExecutor


@dataclass(frozen=True)
class TestInput:
    text: str


@dataclass(frozen=True)
class TestOutput:
    value: str


class TypedToolImplTests(unittest.TestCase):
    def test_from_inputless_accepts_raw_dict_output(self):
        tool = TypedToolImpl.from_inputless(
            ToolMetadata(name="test_inputless", version="1.0.0", description="Test inputless tool", category="test"),
            TestOutput,
            lambda: {"value": "ok"},
        )

        result = ToolExecutor().execute(tool, {})

        self.assertTrue(result.success)
        self.assertEqual(result.output.value, "ok")

    def test_from_inputless_filters_extra_output_fields(self):
        tool = TypedToolImpl.from_inputless(
            ToolMetadata(name="test_inputless_extra", version="1.0.0", description="Test inputless tool with extra fields", category="test"),
            TestOutput,
            lambda: {"value": "ok", "extra": "ignore me"},
        )

        result = ToolExecutor().execute(tool, {})

        self.assertTrue(result.success)
        self.assertEqual(result.output.value, "ok")

    def test_from_handler_accepts_raw_dict_output(self):
        tool = TypedToolImpl.from_handler(
            ToolMetadata(name="test_handler", version="1.0.0", description="Test handler tool", category="test"),
            TestInput,
            TestOutput,
            lambda input_obj: {"value": input_obj.text.upper()},
        )

        result = ToolExecutor().execute(tool, {"text": "hello"})

        self.assertTrue(result.success)
        self.assertEqual(result.output.value, "HELLO")


if __name__ == "__main__":
    _ = unittest.main()
