# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Tests for `build_companion`: the parts of the release that go stale quietly.

`scons` is supposed to produce an add-on whose bundled companion matches the
manifest inside it. Three things can go wrong without any test noticing until a
user installs the add-on and sees an empty tool list:

* the manifest claims a version that is not the crate's (Cargo.toml has a dozen
  `version =` lines and only one of them is the package);
* the packaged binary is an old build, left behind by a previous layout;
* `min_bridge_api_level` disagrees with the constant the companion was compiled
  with, so the companion refuses the bridge's catalog.

So those are tested here, with a temporary tree and a fake cargo.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

import build_companion  # noqa: E402


def make_crate(root: Path, version: str = "1.2.3", api_level: int = 3) -> None:
	(root / "rust_mcp_bridge" / "src").mkdir(parents=True, exist_ok=True)
	(root / "rust_mcp_bridge" / "Cargo.toml").write_text(
		f'[package]\nname = "nvda-mcp-server"\nversion = "{version}"\nedition = "2021"\n',
		encoding="utf-8",
	)
	(root / "rust_mcp_bridge" / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
	(root / "rust_mcp_bridge" / "src" / "protocol.rs").write_text(
		f"pub const API_LEVEL: u32 = {api_level};\n",
		encoding="utf-8",
	)


def write_binary(path: Path, payload: bytes = b"MZ" + b"\0" * 4096) -> Path:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_bytes(payload)
	return path


class CrateVersionTests(unittest.TestCase):
	def test_the_package_version_is_the_one_under_package(self):
		text = """[package]
name = "x"
version = "9.8.7"

[dependencies]
serde = { version = "1.0.0" }
"""
		self.assertEqual(build_companion.crate_version(text), "9.8.7")

	def test_a_workspace_version_is_refused_rather_than_guessed(self):
		with self.assertRaises(build_companion.BuildError) as caught:
			build_companion.crate_version('[package]\nversion.workspace = true\n')
		self.assertIn("workspace", str(caught.exception))

	def test_a_manifest_without_a_version_fails_loudly(self):
		with self.assertRaises(build_companion.BuildError):
			build_companion.crate_version('[package]\nname = "x"\n')


class ApiLevelTests(unittest.TestCase):
	def test_the_level_comes_from_the_compiled_constant(self):
		self.assertEqual(
			build_companion.companion_api_level("pub const API_LEVEL: u32 = 7;"),
			7,
		)

	def test_a_missing_constant_is_an_error_not_a_default(self):
		# A silent default here would ship a manifest claiming a level the
		# binary does not enforce, which is how the companion came to refuse the
		# bridge's catalog while everything looked built.
		with self.assertRaises(build_companion.BuildError):
			build_companion.companion_api_level("// the constant moved to a build script\n")


class SourcesTests(unittest.TestCase):
	def test_every_rust_file_is_a_dependency(self):
		with tempfile.TemporaryDirectory() as raw:
			root = Path(raw)
			make_crate(root)
			sources = [path.relative_to(root).as_posix() for path in build_companion.rust_sources(root)]

		self.assertIn("rust_mcp_bridge/Cargo.toml", sources)
		self.assertIn("rust_mcp_bridge/src/main.rs", sources)
		self.assertIn("rust_mcp_bridge/src/protocol.rs", sources)

	def test_a_missing_crate_is_an_error(self):
		with tempfile.TemporaryDirectory() as raw:
			with self.assertRaises(build_companion.BuildError):
				build_companion.rust_sources(Path(raw))


class CargoDiscoveryTests(unittest.TestCase):
	def test_no_cargo_says_how_to_proceed_without_it(self):
		original = shutil.which
		shutil.which = lambda _name: None  # type: ignore[assignment]
		try:
			with self.assertRaises(build_companion.BuildError) as caught:
				build_companion.find_cargo()
		finally:
			shutil.which = original  # type: ignore[assignment]
		self.assertIn("companion=0", str(caught.exception))


class ReleaseBinaryTests(unittest.TestCase):
	def test_the_newest_binary_wins(self):
		with tempfile.TemporaryDirectory() as raw:
			root = Path(raw)
			old = write_binary(root / "rust_mcp_bridge" / "target" / "release" / "nvda-mcp-server.exe")
			new = write_binary(
				root / "rust_mcp_bridge" / "target" / "x86_64-pc-windows-msvc" / "release" / "nvda-mcp-server.exe"
			)
			import os

			os.utime(old, (1_000_000, 1_000_000))
			os.utime(new, (2_000_000, 2_000_000))
			self.assertEqual(build_companion.find_release_binary(root), new)

	def test_no_build_is_an_error(self):
		with tempfile.TemporaryDirectory() as raw:
			with self.assertRaises(build_companion.BuildError) as caught:
				build_companion.find_release_binary(Path(raw))
		self.assertIn("cargo build --release", str(caught.exception))


class BuildTests(unittest.TestCase):
	def test_the_command_is_a_release_build_of_the_crate(self):
		with tempfile.TemporaryDirectory() as raw:
			root = Path(raw)
			make_crate(root)
			calls = []

			def runner(command, **kwargs):
				calls.append((command, kwargs))
				write_binary(root / "rust_mcp_bridge" / "target" / "release" / "nvda-mcp-server.exe")
				return subprocess.CompletedProcess(command, 0, "", "")

			original = shutil.which
			shutil.which = lambda _name: "cargo.exe"  # type: ignore[assignment]
			try:
				binary = build_companion.build(root, runner=runner)
			finally:
				shutil.which = original  # type: ignore[assignment]

		command = calls[0][0]
		self.assertIn("--release", command)
		self.assertIn("--manifest-path", command)
		self.assertTrue(binary.name.endswith("nvda-mcp-server.exe"))

	def test_a_failed_build_reports_tail_of_cargo_output(self):
		with tempfile.TemporaryDirectory() as raw:
			root = Path(raw)
			make_crate(root)

			def runner(command, **kwargs):
				return subprocess.CompletedProcess(command, 101, "", "error[E0425]: cannot find value\n")

			original = shutil.which
			shutil.which = lambda _name: "cargo.exe"  # type: ignore[assignment]
			try:
				with self.assertRaises(build_companion.BuildError) as caught:
					build_companion.build(root, runner=runner)
			finally:
				shutil.which = original  # type: ignore[assignment]

		self.assertIn("exit 101", str(caught.exception))
		self.assertIn("E0425", str(caught.exception))


class StagingTests(unittest.TestCase):
	def test_the_manifest_describes_the_binary_next_to_it(self):
		with tempfile.TemporaryDirectory() as raw:
			root = Path(raw)
			make_crate(root)
			binary = write_binary(root / "build" / "nvda-mcp-server.exe", b"MZ" + b"payload" * 100)

			staged_binary, manifest_path = build_companion.stage(root, binary, "1.2.3", 3)

			import json

			manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
			self.assertEqual(manifest["version"], "1.2.3")
			self.assertEqual(manifest["min_bridge_api_level"], 3)
			self.assertEqual(manifest["sha256"], build_companion.sha256_of(staged_binary))
			self.assertEqual(manifest["binary_name"], staged_binary.name)
			self.assertEqual(manifest_path.parent, staged_binary.parent)

	def test_a_non_windows_payload_is_refused(self):
		with tempfile.TemporaryDirectory() as raw:
			root = Path(raw)
			binary = write_binary(root / "build" / "nvda-mcp-server.exe", b"#!/bin/sh\n")

			with self.assertRaises(build_companion.BuildError) as caught:
				build_companion.stage(root, binary, "1.2.3", 3)
			self.assertIn("not a Windows executable", str(caught.exception))


if __name__ == "__main__":
	unittest.main()
