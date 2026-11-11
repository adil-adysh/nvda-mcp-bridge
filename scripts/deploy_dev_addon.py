"""Deploy the add-on source tree into the installed add-on, and verify it.

This script used to copy a hand-maintained list of "changed" files and verify
only those. That is how a half-updated tree shipped: ``http_bridge.py`` was
copied while ``server/security.py`` was not, so the add-on died on import with
``cannot import name 'load_developer_ttl_seconds'`` and the bridge never
started. A list of files that must agree cannot be maintained by hand.

It now walks the whole tree: copies every file whose content differs, verifies
every destination by hash, and reports anything on either side with no
counterpart. It exits non-zero if anything is missing or mismatched, so
"deployed" cannot be printed for a tree that would fail to import.

    python scripts/deploy_dev_addon.py [--source DIR] [--target DIR]
"""

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO / "addon" / "globalPlugins" / "nvdaMcp-bridge"
DEFAULT_TARGET = (
    Path(os.environ.get("APPDATA", ""))
    / "nvda"
    / "addons"
    / "nvdaMcpBridge"
    / "globalPlugins"
    / "nvdaMcp-bridge"
)

#: Build artefacts and editor leftovers that must never be deployed.
IGNORED_DIRS = {"__pycache__", ".pytest_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def deployable_files(root: Path) -> dict[Path, Path]:
    """Map every deployable file under ``root`` to its path relative to it."""
    files = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if path.suffix in IGNORED_SUFFIXES:
            continue
        files[path.relative_to(root)] = path
    return files


def deploy(source_root: Path, target_root: Path) -> dict:
    """Copy what differs, then verify the whole tree.

    Returns a report with ``copied``, ``unchanged``, ``extra`` and ``failures``.
    Verification covers every source file, not just the ones this run touched,
    because the failure being guarded against is a stale file nobody looked at.
    """
    sources = deployable_files(source_root)
    report = {"source_count": len(sources), "copied": [], "unchanged": [], "extra": [], "failures": []}

    for relative, source in sorted(sources.items()):
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if target.is_file() and digest(source) == digest(target):
                report["unchanged"].append(relative)
                continue
            shutil.copy2(source, target)
        except OSError as error:
            report["failures"].append(f"{relative}: {error}")
            continue
        report["copied"].append(relative)

    for relative, source in sorted(sources.items()):
        target = target_root / relative
        if not target.is_file():
            report["failures"].append(f"{relative}: missing at destination")
        elif digest(source) != digest(target):
            report["failures"].append(f"{relative}: stale at destination")

    report["extra"] = sorted(
        relative
        for relative in deployable_files(target_root)
        if relative not in sources
    )
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    args = parser.parse_args(argv)

    if not args.source.is_dir():
        print(f"source tree missing: {args.source}")
        return 2
    if not args.target.is_dir():
        print(f"target add-on tree missing: {args.target}")
        return 2

    report = deploy(args.source, args.target)

    print(f"source files: {report['source_count']}")
    print(f"copied:       {len(report['copied'])}")
    for relative in report["copied"]:
        print(f"  + {relative}")
    print(f"unchanged:    {len(report['unchanged'])}")
    if report["extra"]:
        print(f"only at destination (not deleted): {len(report['extra'])}")
        for relative in report["extra"]:
            print(f"  ? {relative}")

    if report["failures"]:
        print(f"FAILED: {len(report['failures'])} problem(s)")
        for failure in report["failures"]:
            print(f"  ! {failure}")
        return 1

    print(f"verified: every file under {args.target} matches the source tree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
