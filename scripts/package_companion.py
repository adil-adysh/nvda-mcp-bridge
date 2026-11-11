#!/usr/bin/env python3
"""Stage and verify the bundled Windows Rust MCP companion in NVDA add-on archives."""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path
import sys
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_REL = Path("globalPlugins/nvdaMcp-bridge/companion/windows-x64")
EXECUTABLE = "nvda-mcp-server.exe"
MANIFEST = "manifest.json"


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def expected_version() -> str:
    with (ROOT / "rust_mcp_bridge/Cargo.toml").open("rb") as source:
        return tomllib.load(source)["package"]["version"]


def require_windows_x64(binary: bytes) -> None:
    """Reject non-PE and non-x64 binaries before distribution."""
    if len(binary) < 1024 or binary[:2] != b"MZ":
        raise RuntimeError("Companion is not a valid Windows PE executable")
    offset = struct.unpack_from("<I", binary, 0x3C)[0]
    if offset + 6 > len(binary) or binary[offset:offset + 4] != b"PE\\x00\\x00":
        raise RuntimeError("Companion has an invalid PE header")
    machine = struct.unpack_from("<H", binary, offset + 4)[0]
    if machine != 0x8664:
        raise RuntimeError(f"Expected Windows x64 companion, PE machine is 0x{machine:04x}")


def stage(binary: Path) -> None:
    if not binary.is_file() or binary.stat().st_size < 1024:
        raise RuntimeError(f"Missing or unexpectedly small Rust companion: {binary}")
    require_windows_x64(binary.read_bytes())
    import shutil
    target = ROOT / "addon" / PACKAGE_REL
    target.mkdir(parents=True, exist_ok=True)
    dest = target / EXECUTABLE
    shutil.copy2(binary, dest)
    info = {
        "schema_version": 1,
        "name": "nvda-mcp-server",
        "version": expected_version(),
        "platform": "windows-x64",
        "executable": EXECUTABLE,
        "sha256": file_sha256(dest),
        "min_bridge_api_level": 1,
    }
    (target / MANIFEST).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    print(f"Staged companion: {dest} ({dest.stat().st_size} bytes); SHA256 {info['sha256']}")


def verify(archive: Path) -> None:
    if not archive.is_file():
        raise RuntimeError(f"Add-on archive not found: {archive}")
    root = PACKAGE_REL.as_posix() + "/"
    with zipfile.ZipFile(archive) as zf:
        with zf.open(root + MANIFEST) as source:
            info = json.load(source)
        with zf.open(root + EXECUTABLE) as source:
            binary = source.read()
    if info.get("schema_version") != 1 or info.get("platform") != "windows-x64":
        raise RuntimeError("Bundled companion manifest is invalid")
    if info.get("version") != expected_version():
        raise RuntimeError("Bundled companion version does not match Cargo.toml")
    if info.get("executable") != EXECUTABLE or info.get("min_bridge_api_level") != 1:
        raise RuntimeError("Bundled companion metadata is inconsistent")
    require_windows_x64(binary)
    digest = hashlib.sha256(binary).hexdigest()
    if digest != info.get("sha256"):
        raise RuntimeError(f"Bundled companion digest mismatch: {digest}")
    print(f"Verified {archive}: embedded {EXECUTABLE} ({len(binary)} bytes), SHA256 {digest}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_stage = sub.add_parser("stage")
    p_stage.add_argument("--binary", type=Path, default=ROOT / "rust_mcp_bridge/target/release/nvda-mcp-server.exe")
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("archive", type=Path)
    args = parser.parse_args()
    if args.command == "stage":
        stage(args.binary)
    else:
        verify(args.archive)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, zipfile.BadZipFile) as error:
        print(f"Companion packaging error: {error}", file=sys.stderr)
        sys.exit(1)
