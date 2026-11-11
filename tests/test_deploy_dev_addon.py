# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Tests for the dev-addon deployer.

The bug this pins: the deployer copied a hand-maintained list of "changed" files
and verified only those, so it happily reported success for a tree where
``http_bridge.py`` imported a symbol that the un-copied ``security.py`` did not
define. NVDA then failed to load the add-on and the bridge never started.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "deploy_dev_addon.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("deploy_dev_addon", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deployer = _load_module()


class DeployTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.source = root / "source"
        self.target = root / "target"
        self.source.mkdir()
        self.target.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, root: Path, relative: str, text: str) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_copies_every_file_into_an_empty_target(self):
        self._write(self.source, "server/security.py", "VALUE = 1\n")
        self._write(self.source, "server/http_bridge.py", "from .security import VALUE\n")

        report = deployer.deploy(self.source, self.target)

        self.assertEqual(report["failures"], [])
        self.assertEqual(len(report["copied"]), 2)
        self.assertTrue((self.target / "server" / "security.py").is_file())

    def test_the_regression_stale_module_is_copied(self):
        """A file nobody listed must still be updated, or the tree cannot import."""
        self._write(self.source, "server/security.py", "def new_symbol():\n    return 1\n")
        self._write(self.source, "server/http_bridge.py", "from .security import new_symbol\n")
        # Target already has the new http_bridge but an OLD security module.
        self._write(self.target, "server/http_bridge.py", "from .security import new_symbol\n")
        self._write(self.target, "server/security.py", "def old_symbol():\n    return 1\n")

        report = deployer.deploy(self.source, self.target)

        self.assertEqual(report["failures"], [])
        self.assertIn(Path("server/security.py"), report["copied"])
        self.assertEqual(
            (self.target / "server/security.py").read_text(encoding="utf-8"),
            "def new_symbol():\n    return 1\n",
        )

    def test_identical_files_are_not_rewritten(self):
        self._write(self.source, "a.py", "x = 1\n")
        self._write(self.target, "a.py", "x = 1\n")

        report = deployer.deploy(self.source, self.target)

        self.assertEqual(report["copied"], [])
        self.assertEqual(report["unchanged"], [Path("a.py")])

    def test_ignores_bytecode(self):
        self._write(self.source, "a.py", "x = 1\n")
        self._write(self.source, "__pycache__/a.cpython-313.pyc", "not source")

        report = deployer.deploy(self.source, self.target)

        self.assertEqual(report["source_count"], 1)
        self.assertFalse((self.target / "__pycache__").exists())

    def test_reports_destination_only_files_without_deleting_them(self):
        self._write(self.source, "a.py", "x = 1\n")
        stray = self._write(self.target, "stale.py", "old = 1\n")

        report = deployer.deploy(self.source, self.target)

        self.assertEqual(report["extra"], [Path("stale.py")])
        self.assertTrue(stray.is_file(), "extra files are reported, never deleted")

    def test_fails_when_a_source_file_cannot_be_verified(self):
        self._write(self.source, "a.py", "x = 1\n")

        # Delete the destination between the copy and the verification pass.
        original_copy2 = deployer.shutil.copy2
        deployer.shutil.copy2 = lambda source, target: None
        try:
            report = deployer.deploy(self.source, self.target)
        finally:
            deployer.shutil.copy2 = original_copy2

        self.assertTrue(report["failures"])
        self.assertIn("missing at destination", report["failures"][0])

    def test_main_exits_zero_for_a_clean_tree(self):
        self._write(self.source, "a.py", "x = 1\n")

        exit_code = deployer.main(["--source", str(self.source), "--target", str(self.target)])

        self.assertEqual(exit_code, 0)

    def test_main_exits_two_when_the_target_is_missing(self):
        self._write(self.source, "a.py", "x = 1\n")

        exit_code = deployer.main(["--source", str(self.source), "--target", str(self.target / "nope")])

        self.assertEqual(exit_code, 2)


if __name__ == "__main__":
    unittest.main()
