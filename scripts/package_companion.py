#!/usr/bin/env python3
"""Package the compiled Rust MCP companion binary into the NVDA add-on staging directory.

Locates the compiled nvda-mcp-server.exe, calculates its SHA-256 checksum,
copies it to addon/globalPlugins/nvdaMcp-bridge/companion/windows-x64/,
and generates a companion manifest.json.
"""

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPANION_DEST_DIR = (
    REPO_ROOT
    / "addon"
    / "globalPlugins"
    / "nvdaMcp-bridge"
    / "companion"
    / "windows-x64"
)


def find_release_binary() -> Path:
    """Find the compiled release binary for nvda-mcp-server.exe."""
    candidates = [
        REPO_ROOT / "rust_mcp_bridge" / "target" / "release" / "nvda-mcp-server.exe",
        REPO_ROOT
        / "rust_mcp_bridge"
        / "target"
        / "x86_64-pc-windows-msvc"
        / "release"
        / "nvda-mcp-server.exe",
        REPO_ROOT / "rust_mcp_bridge" / "target" / "debug" / "nvda-mcp-server.exe",
        REPO_ROOT
        / "rust_mcp_bridge"
        / "target"
        / "x86_64-pc-windows-msvc"
        / "debug"
        / "nvda-mcp-server.exe",
    ]

    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 1024:
            return candidate

    # Search anywhere under target
    target_dir = REPO_ROOT / "rust_mcp_bridge" / "target"
    if target_dir.is_dir():
        for p in target_dir.rglob("nvda-mcp-server.exe"):
            if "release" in str(p) and p.stat().st_size > 1024:
                return p
        for p in target_dir.rglob("nvda-mcp-server.exe"):
            if p.stat().st_size > 1024:
                return p

    raise FileNotFoundError(
        "nvda-mcp-server.exe not found in rust_mcp_bridge/target. "
        "Run 'cargo build --release --manifest-path rust_mcp_bridge/Cargo.toml' first."
    )


def calculate_sha256(file_path: Path) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest().lower()


def package_companion() -> None:
    src_binary = find_release_binary()
    print(f"Found companion binary: {src_binary} ({src_binary.stat().st_size} bytes)")

    # Verify binary starts with PE header (b'MZ')
    with open(src_binary, "rb") as f:
        header = f.read(2)
        if header != b"MZ":
            sys.exit(f"Error: {src_binary} is not a valid Windows PE binary (header: {header!r})")

    sha256 = calculate_sha256(src_binary)
    print(f"SHA-256: {sha256}")

    COMPANION_DEST_DIR.mkdir(parents=True, exist_ok=True)
    dest_binary = COMPANION_DEST_DIR / "nvda-mcp-server.exe"

    shutil.copy2(src_binary, dest_binary)
    print(f"Copied companion binary to: {dest_binary}")

    manifest_data = {
        "binary_name": "nvda-mcp-server.exe",
        "version": "1.0.0",
        "architecture": "windows-x64",
        "min_bridge_api_level": 1,
        "sha256": sha256,
        "package_compatibility": {
            "min_nvda_version": "2026.1.0",
            "target_os": "windows-x64",
        },
    }

    manifest_path = COMPANION_DEST_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
    print(f"Wrote companion manifest to: {manifest_path}")
    print("Companion packaged successfully.")


if __name__ == "__main__":
    package_companion()
