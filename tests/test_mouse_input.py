"""Regression coverage for logical mouse buttons on Windows."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import patch

import pytest


@pytest.fixture
def win_input():
	path = (
		Path(__file__).resolve().parents[1]
		/ "addon" / "globalPlugins" / "nvdaMcp-bridge" / "nvda_bridge" / "win_input.py"
	)
	spec = importlib.util.spec_from_file_location("test_win_input", path)
	module = importlib.util.module_from_spec(spec)
	with patch.dict(sys.modules, {"logHandler": ModuleType("logHandler")}):
		spec.loader.exec_module(module)
	return module


@pytest.mark.parametrize(
	("swapped", "button", "expected"),
	[
		(0, "left", "left"),
		(0, "right", "right"),
		(1, "left", "right"),
		(1, "right", "left"),
		(1, "middle", "middle"),
	],
)
def test_click_sends_the_native_flags_for_the_logical_button(win_input, swapped, button, expected):
	metrics = {23: swapped, 76: 0, 77: 0, 78: 1920, 79: 1080}
	with (
		patch.object(win_input._USER32, "GetSystemMetrics", side_effect=metrics.__getitem__),
		patch.object(win_input._USER32, "GetCursorPos", return_value=True),
		patch.object(win_input.time, "sleep"),
		patch.object(win_input, "_send") as send,
	):
		win_input.click(0, 0, button)

	button_events = send.call_args_list[1].args[0]
	assert tuple(event.mi.dwFlags for event in button_events) == win_input._BUTTON_FLAGS[expected]
