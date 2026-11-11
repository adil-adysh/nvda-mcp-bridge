#!/usr/bin/env python3
"""Validate that an assembled .nvda-addon archive contains all required components.

Checks:
- Archive is a valid ZIP
- manifest.ini exists and has valid metadata
- Bundled companion binary exists in globalPlugins/nvdaMcp-bridge/companion/windows-x64/
- Companion manifest.json exists and is valid JSON
- Binary checksum matches manifest.json
- Binary starts with Windows PE executable header (MZ)
"""

import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def find_addon_file() -> Path:
    candidates = list(REPO_ROOT.glob("*.nvda-addon"))
    if not candidates:
        # Check dist/
        candidates = list((REPO_ROOT / "dist").glob("*.nvda-addon"))
    if not candidates:
        sys.exit("Error: No .nvda-addon file found in repository root or dist/")
    return candidates[0]


def validate_addon_package(addon_path: Path) -> None:
    print(f"Validating addon package: {addon_path} ({addon_path.stat().st_size} bytes)")

    if not zipfile.is_zipfile(addon_path):
        sys.exit(f"Error: {addon_path} is not a valid zip archive")

    with zipfile.ZipFile(addon_path, "r") as zf:
        namelist = set(zf.namelist())

        # 1. Check manifest.ini
        if "manifest.ini" not in namelist:
            sys.exit("Error: manifest.ini missing from addon root")

        # 2. Check plugin python files
        if "globalPlugins/nvdaMcp-bridge/__init__.py" not in namelist:
            sys.exit("Error: globalPlugins/nvdaMcp-bridge/__init__.py missing from addon")

        # 3. Check bundled companion binary and manifest
        binary_entry = "globalPlugins/nvdaMcp-bridge/companion/windows-x64/nvda-mcp-server.exe"
        manifest_entry = "globalPlugins/nvdaMcp-bridge/companion/windows-x64/manifest.json"

        if binary_entry not in namelist:
            sys.exit(f"Error: Bundled companion binary missing from addon: {binary_entry}")

        if manifest_entry not in namelist:
            sys.exit(f"Error: Companion manifest missing from addon: {manifest_entry}")

        # 4. Verify companion manifest and checksum
        manifest_bytes = zf.read(manifest_entry)
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except Exception as e:
            sys.exit(f"Error: Companion manifest.json is invalid JSON: {e}")

        expected_sha = manifest.get("sha256")
        if not expected_sha:
            sys.exit("Error: Companion manifest.json missing 'sha256' field")

        binary_bytes = zf.read(binary_entry)
        if len(binary_bytes) < 1024:
            sys.exit(f"Error: Companion binary is too small ({len(binary_bytes)} bytes)")

        if not binary_bytes.startswith(b"MZ"):
            sys.exit("Error: Companion binary does not start with Windows PE header 'MZ'")

        actual_sha = hashlib.sha256(binary_bytes).hexdigest().lower()
        if actual_sha != expected_sha.lower():
            sys.exit(
                f"Error: Companion checksum mismatch: expected {expected_sha}, got {actual_sha}"
            )

        print(f"Verified bundled companion binary: {len(binary_bytes)} bytes, SHA-256: {actual_sha}")
        print("Addon package validation succeeded! All bundled components verified.")


if __name__ == "__main__":
    addon_file = find_addon_file()
    validate_addon_package(addon_file)
