"""Build the Rust MCP companion and stage it inside the add-on.

A release of this project is three artefacts that have to agree: the Python
bridge, the Rust companion bundled at ``companion/windows-x64``, and the
manifest whose checksum names that binary. Built by hand, one of the three is
eventually stale — which is exactly what happened when the protocol moved to
api_level 3 and the bundled companion still spoke 2: the bridge was fine, the
add-on installed, and the companion refused the catalog and offered no tools.

So ``scons`` owns all three. This module is the implementation; the SCons action
and ``scripts/package_companion.py`` are both thin callers, so the two paths
cannot drift apart.

Everything here is importable without SCons, NVDA or a Rust toolchain, so the
parts that are easy to get wrong (which version is the crate's, which binary is
the newest, what the manifest says) are unit-tested.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parent

CARGO_MANIFEST = Path("rust_mcp_bridge") / "Cargo.toml"
PROTOCOL_SOURCE = Path("rust_mcp_bridge") / "src" / "protocol.rs"
TARGET_ROOT = Path("rust_mcp_bridge") / "target"

DEST_DIR = Path("addon") / "globalPlugins" / "nvdaMcp-bridge" / "companion" / "windows-x64"
BINARY_NAME = "nvda-mcp-server.exe"
ARCHITECTURE = "windows-x64"

#: Where cargo puts a release binary: the plain target dir, or a triple-specific
#: one when the crate is built with an explicit `--target`.
RELEASE_DIRS = (
	TARGET_ROOT / "release",
	TARGET_ROOT / "x86_64-pc-windows-msvc" / "release",
)

#: Enough of cargo's output to explain a failure without pasting a whole build.
FAILURE_TAIL_LINES = 40


class BuildError(RuntimeError):
	"""The companion could not be built or staged.

	Raised rather than exited so the SCons action, the packaging script and the
	tests all see the same failure with the same message.
	"""


def crate_version(manifest_text: str) -> str:
	"""The version declared under ``[package]``, not a dependency's.

	A Cargo.toml is full of ``version = "..."`` lines — dependencies' ranges,
	workspace packages, the edition — so the only safe answer is the one in the
	package section. ``version.workspace = true`` is refused rather than guessed:
	silently shipping a manifest with someone else's version is worse than a
	build failure that says which file to fix.
	"""
	section = ""
	for line in manifest_text.splitlines():
		stripped = line.strip()
		if stripped.startswith("[") and stripped.endswith("]"):
			section = stripped
			continue
		if section != "[package]":
			continue
		match = re.match(r'^version\s*=\s*"([^"]+)"', stripped)
		if match:
			return match.group(1)
		if re.match(r"^version\s*\.\s*workspace\s*=", stripped):
			raise BuildError(
				f"{CARGO_MANIFEST} takes its version from a workspace; declare one under [package] "
				"so the companion manifest can name it",
			)
	raise BuildError(f"{CARGO_MANIFEST} has no [package] version")


def companion_api_level(protocol_text: str) -> int:
	"""The protocol level the companion enforces, read from its source.

	The manifest's ``min_bridge_api_level`` is not documentation: the companion
	refuses a catalog stamped with anything else, so the number has to come from
	the same constant the binary was compiled with.
	"""
	match = re.search(r"pub const API_LEVEL:\s*u32\s*=\s*(\d+)\s*;", protocol_text)
	if match is None:
		raise BuildError(f"no API_LEVEL constant found in {PROTOCOL_SOURCE}")
	return int(match.group(1))


def rust_sources(root: Path = REPO_ROOT) -> list[Path]:
	"""Every file a change to which means the companion must be rebuilt."""
	root = Path(root)
	manifest = root / CARGO_MANIFEST
	if not manifest.is_file():
		raise BuildError(f"no Rust crate at {manifest}")
	sources = [manifest]
	for relative in (Path("rust_mcp_bridge") / "Cargo.lock", Path("rust_mcp_bridge") / "build.rs"):
		candidate = root / relative
		if candidate.is_file():
			sources.append(candidate)
	source_dir = root / "rust_mcp_bridge" / "src"
	if source_dir.is_dir():
		sources.extend(sorted(source_dir.rglob("*.rs")))
	return sources


def find_cargo() -> str:
	"""The cargo executable, or a failure that says how to proceed without it."""
	executable = shutil.which("cargo")
	if not executable:
		raise BuildError(
			"cargo was not found on PATH. Install the Rust toolchain "
			"(https://rustup.rs), or build the add-on without the companion: "
			"scons companion=0",
		)
	return executable


def find_release_binary(root: Path = REPO_ROOT) -> Path:
	"""The newest release binary cargo produced.

	Newest rather than "first that exists": a stale binary from an earlier
	layout is exactly what would be bundled if this picked by path order.
	"""
	existing = [root / relative / BINARY_NAME for relative in RELEASE_DIRS if (root / relative / BINARY_NAME).is_file()]
	existing = [path for path in existing if path.stat().st_size > 1024]
	if not existing:
		raise BuildError(
			f"no {BINARY_NAME} under {root / TARGET_ROOT}. "
			"Run 'cargo build --release --manifest-path rust_mcp_bridge/Cargo.toml'.",
		)
	return max(existing, key=lambda path: path.stat().st_mtime)


def sha256_of(path: Path) -> str:
	hasher = hashlib.sha256()
	with open(path, "rb") as handle:
		while chunk := handle.read(65536):
			hasher.update(chunk)
	return hasher.hexdigest().lower()


def build(
	root: Path = REPO_ROOT,
	runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Path:
	"""Run ``cargo build --release`` and return the binary."""
	cargo = find_cargo()
	command = [cargo, "build", "--release", "--manifest-path", str(root / CARGO_MANIFEST)]
	result = runner(command, cwd=str(root), capture_output=True, text=True)
	if result.returncode != 0:
		output = (result.stderr or result.stdout or "").strip().splitlines()
		tail = "\n".join(output[-FAILURE_TAIL_LINES:])
		raise BuildError(f"cargo build failed (exit {result.returncode}):\n{tail}")
	return find_release_binary(root)


def stage(
	root: Path,
	binary: Path,
	version: str,
	api_level: int,
) -> tuple[Path, Path]:
	"""Copy the binary into the add-on and write the manifest beside it."""
	with open(binary, "rb") as handle:
		header = handle.read(2)
	if header != b"MZ":
		raise BuildError(f"{binary} is not a Windows executable (header {header!r})")

	destination = root / DEST_DIR
	destination.mkdir(parents=True, exist_ok=True)
	staged_binary = destination / BINARY_NAME
	shutil.copy2(binary, staged_binary)

	manifest = {
		"binary_name": BINARY_NAME,
		"version": version,
		"architecture": ARCHITECTURE,
		"min_bridge_api_level": api_level,
		"sha256": sha256_of(staged_binary),
		"package_compatibility": {
			"min_nvda_version": "2026.1.0",
			"target_os": ARCHITECTURE,
		},
	}
	manifest_path = destination / "manifest.json"
	manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
	return staged_binary, manifest_path


def build_and_package(root: Path = REPO_ROOT) -> tuple[Path, Path]:
	"""Build the companion, stage it, and describe it. The whole job."""
	binary = build(root)
	version = crate_version((root / CARGO_MANIFEST).read_text(encoding="utf-8"))
	api_level = companion_api_level((root / PROTOCOL_SOURCE).read_text(encoding="utf-8"))
	return stage(root, binary, version, api_level)


def main(argv: Sequence[str] | None = None) -> int:
	"""CLI: build and stage, or stage an existing build with ``--no-build``."""
	import argparse

	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--no-build",
		action="store_true",
		help="stage the binary cargo already produced, without running cargo",
	)
	args = parser.parse_args(argv)

	try:
		if args.no_build:
			binary = find_release_binary(REPO_ROOT)
			version = crate_version((REPO_ROOT / CARGO_MANIFEST).read_text(encoding="utf-8"))
			api_level = companion_api_level((REPO_ROOT / PROTOCOL_SOURCE).read_text(encoding="utf-8"))
			staged = stage(REPO_ROOT, binary, version, api_level)
		else:
			staged = build_and_package(REPO_ROOT)
	except BuildError as error:
		print(f"companion: {error}", file=__import__("sys").stderr)
		return 1

	binary_path, manifest_path = staged
	print(f"companion: {binary_path} ({binary_path.stat().st_size} bytes)")
	print(f"companion: {manifest_path}")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
