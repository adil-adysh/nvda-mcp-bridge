# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""read must never return a bare object-replacement character."""

import importlib
import os
import sys
import types
import unittest

_ADDON_DIR = os.path.normpath(
	os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "addon", "globalPlugins", "nvdaMcp-bridge")
)
sys.path.insert(0, _ADDON_DIR)

_TEXT_INFOS = types.SimpleNamespace(
	POSITION_SELECTION=1,
	POSITION_CARET=2,
	POSITION_FIRST=3,
	POSITION_ALL=4,
	UNIT_LINE=5,
)

_STUBBED = {}
for _name, _module in {
	"api": types.ModuleType("api"),
	"textInfos": _TEXT_INFOS,
	"logHandler": types.SimpleNamespace(
		log=types.SimpleNamespace(
			debug=lambda *a, **k: None,
			info=lambda *a, **k: None,
			warning=lambda *a, **k: None,
			error=lambda *a, **k: None,
		)
	),
	"keyboardHandler": types.SimpleNamespace(),
	"queueHandler": types.SimpleNamespace(eventQueue=None, queueFunction=lambda *a, **k: None),
	"ui": types.ModuleType("ui"),
	"controlTypes": types.ModuleType("controlTypes"),
	"speech": types.ModuleType("speech"),
	"core": types.ModuleType("core"),
	"versionInfo": types.ModuleType("versionInfo"),
	"globalVars": types.ModuleType("globalVars"),
	"config": types.ModuleType("config"),
	"winUser": types.ModuleType("winUser"),
	"tones": types.ModuleType("tones"),
	"speechDictHandler": types.ModuleType("speechDictHandler"),
	"addonHandler": types.ModuleType("addonHandler"),
}.items():
	if _name == "textInfos":
		# The stub must carry the POSITION_* constants even when an earlier test
		# module installed an empty ``textInfos`` module.
		_STUBBED[_name] = sys.modules.get(_name)
		sys.modules[_name] = _module
	elif _name not in sys.modules:
		_STUBBED[_name] = None
		sys.modules[_name] = _module
	else:
		_STUBBED[_name] = sys.modules[_name]

_PACKAGE = "nvdaMcpBridge"
if _PACKAGE not in sys.modules:
	package = types.ModuleType(_PACKAGE)
	package.__path__ = [_ADDON_DIR]
	sys.modules[_PACKAGE] = package

actions = importlib.import_module(f"{_PACKAGE}.nvda_bridge.actions")

# Importing the package is the only thing that needs the NVDA stubs. Put the
# process-wide modules back so this test does not change what later test modules
# see; ``actions`` keeps its own ``textInfos`` reference for the calls below.
for _name, _previous in _STUBBED.items():
	if _name == "textInfos":
		continue
	if _previous is None:
		sys.modules.pop(_name, None)
	else:
		sys.modules[_name] = _previous


class _Info:
	def __init__(self, text):
		self.text = text

	def expand(self, _unit):
		return None


class _Text:
	def __init__(self, name):
		self.name = name
		self.value = None
		self.childCount = 0
		self.states = ()
		self.location = None

	def makeTextInfo(self, _position):
		return _Info("\ufffc")

	def getChild(self, _index):
		return None

	def invalidateCache(self):
		return None


class _Doc(_Text):
	def __init__(self, child_name):
		super().__init__("document")
		self.childCount = 1
		self._child = _Text(child_name)

	def getChild(self, _index):
		return self._child


class ReadFallbackTests(unittest.TestCase):
	def test_replacement_character_falls_back_to_the_tree(self):
		result = actions.read_text(_Doc("Real content"), scope="document", max_chars=100)
		self.assertIn("Real content", result["text"])
		self.assertEqual(result["source"], "tree")

	def test_no_text_anywhere_is_reported_as_unavailable(self):
		result = actions.read_text(_Text(""), scope="document", max_chars=100)
		self.assertEqual(result["text"], "")
		self.assertEqual(result["source"], "unavailable")


if __name__ == "__main__":
	unittest.main()
