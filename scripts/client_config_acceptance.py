#!/usr/bin/env python3
"""Acceptance for MCP client configuration, checked against the clients themselves.

Unit tests prove the merge logic. This proves the *format*: for each CLI agent the
script has the vendor's own command write its configuration into a throwaway home
directory, has our configurator write the same server into a second throwaway
home, and compares the two. Matching the vendor byte-for-byte is the only claim
worth making about a file format someone else owns.

Then it looks at the real files: the entry we wrote must be the one the client
resolves, and Claude Code must actually connect to the companion through it.

Run with no arguments. Sections that need a client which is not installed are
reported as skipped rather than failed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "addon" / "globalPlugins" / "nvdaMcp-bridge"))

# Client output carries symbols such as the health checkmark, which the default
# console code page cannot encode; a failed print must not end an acceptance run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _install_addon_module_path() -> None:
    """Import the add-on's configurator the way NVDA does."""
    import types

    addon_dir = REPO / "addon" / "globalPlugins" / "nvdaMcp-bridge"
    if "logHandler" not in sys.modules:
        quiet = types.SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None,
                                      warning=lambda *a, **k: None, error=lambda *a, **k: None)
        sys.modules["logHandler"] = types.SimpleNamespace(log=quiet)
    if "nvdaMcpBridge" not in sys.modules:
        package = types.ModuleType("nvdaMcpBridge")
        package.__path__ = [str(addon_dir)]
        sys.modules["nvdaMcpBridge"] = package


_install_addon_module_path()

from live_acceptance import Report  # noqa: E402
from nvdaMcpBridge.client_config import (  # noqa: E402
    configure_client,
    detect_supported_clients,
    preview_client_configuration,
)

CLIENTS = ("claude_code", "codex")


def companion_exe() -> Path:
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "NVDA MCP Bridge" / "bin" / "nvda-mcp-server.exe",
        Path.home() / "AppData" / "Local" / "Programs" / "NVDA MCP Bridge" / "bin" / "nvda-mcp-server.exe",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise SystemExit("The companion executable is not installed; run the add-on's companion setup first.")


def run(command: str, env_overrides: dict, timeout: int = 120) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run(
        command, shell=True, capture_output=True, text=True, timeout=timeout, env=env,
        encoding="utf-8", errors="replace",
    )


def vendor_entry(client_id: str, exe: Path, home: Path) -> dict:
    """Ask the vendor's own CLI to write an entry, then read it back."""
    if client_id == "claude_code":
        claude = shutil.which("claude")
        if not claude:
            raise FileNotFoundError("claude")
        result = run(f'"{claude}" mcp add -s user nvda -- "{exe}"', {"CLAUDE_CONFIG_DIR": str(home)})
        if result.returncode != 0:
            raise RuntimeError(f"claude mcp add failed: {result.stdout}{result.stderr}")
        data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        return data["mcpServers"]["nvda"]

    codex = shutil.which("codex")
    if not codex:
        raise FileNotFoundError("codex")
    result = run(f'"{codex}" mcp add nvda -- "{exe}"', {"CODEX_HOME": str(home)})
    if result.returncode != 0:
        raise RuntimeError(f"codex mcp add failed: {result.stdout}{result.stderr}")
    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    return data["mcp_servers"]["nvda"]


def our_entry(client_id: str, exe: Path, home: Path) -> tuple[dict, str]:
    """Write the entry with the add-on's configurator into a throwaway home."""
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / (".claude.json" if client_id == "claude_code" else "config.toml")

    ok, message = configure_client(
        client_id=client_id,
        companion_exe_path=exe,
        config_path_override=config_path,
    )
    if not ok:
        raise RuntimeError(message)

    if client_id == "claude_code":
        return json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]["nvda"], message
    return tomllib.loads(config_path.read_text(encoding="utf-8"))["mcp_servers"]["nvda"], message


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    report = Report(verbose=args.verbose)
    exe = companion_exe()
    resolved = str(exe.resolve())

    print(f"companion: {exe}")
    print(f"claude:    {shutil.which('claude') or 'not installed'}")
    print(f"codex:     {shutil.which('codex') or 'not installed'}\n")

    report.section("vendor equivalence")

    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
        for client_id in CLIENTS:
            try:
                expected = vendor_entry(client_id, exe, Path(first))
            except FileNotFoundError:
                report.skip(f"{client_id}: vendor CLI available", "not installed")
                continue
            except Exception as error:  # noqa: BLE001 - reported, not raised
                report.check(f"{client_id}: vendor CLI writes an entry", False, str(error)[:200])
                continue

            actual, _ = our_entry(client_id, exe, Path(second))
            report.check(
                f"{client_id}: our entry matches the vendor's field for field",
                actual == expected,
                f"ours={actual} vendor={expected}",
            )
            report.check(
                f"{client_id}: entry points at the installed companion",
                str(actual.get("command", "")).lower() == resolved.lower(),
            )

            # A fresh file should be byte-identical too, which proves we are not
            # reformatting or dropping keys the client cares about.
            vendor_file = Path(first) / (".claude.json" if client_id == "claude_code" else "config.toml")
            our_file = Path(second) / (".claude.json" if client_id == "claude_code" else "config.toml")
            if client_id == "codex" and vendor_file.is_file() and our_file.is_file():
                report.check(
                    "codex: the whole file is byte-identical to the vendor's",
                    vendor_file.read_text(encoding="utf-8").strip() == our_file.read_text(encoding="utf-8").strip(),
                    "the file holds no other settings yet, so any difference is ours",
                )

    report.section("real configuration")

    clients = detect_supported_clients(expected_companion_exe=exe)
    by_id = {c.client_id: c for c in clients}
    for client_id in CLIENTS:
        info = by_id.get(client_id)
        if info is None:
            report.check(f"{client_id}: is a known client", False)
        elif not info.detected:
            report.skip(f"{client_id}: detected on this machine", "not installed")
        else:
            report.check(
                f"{client_id}: configured to use this companion",
                info.configured,
                f"{info.config_path}",
            )

    # Second run must be a no-op: the file the client may be watching is not touched.
    for client_id in CLIENTS:
        info = by_id.get(client_id)
        if info is None or not info.detected:
            continue
        before = info.config_path.stat().st_mtime_ns
        ok, message = configure_client(client_id, exe)
        after = info.config_path.stat().st_mtime_ns
        report.check(
            f"{client_id}: configuring twice changes nothing",
            ok and before == after,
            message,
        )

    report.section("the client agrees")

    claude = shutil.which("claude")
    if not claude:
        report.skip("claude code connects through the entry", "not installed")
    elif not by_id.get("claude_code") or not by_id["claude_code"].configured:
        report.skip("claude code connects through the entry", "not configured")
    else:
        result = run(f'"{claude}" mcp list', {}, timeout=180)
        output = (result.stdout or "") + (result.stderr or "")
        report.check(
            "claude code lists the nvda server",
            "nvda:" in output,
            output.strip().splitlines()[-1] if output.strip() else "no output",
        )
        report.check(
            "claude code connects to the companion",
            "Connected" in output,
            output.strip().splitlines()[-1] if output.strip() else "no output",
        )

    codex = shutil.which("codex")
    if not codex:
        report.skip("codex resolves the entry", "not installed")
    elif not by_id.get("codex") or not by_id["codex"].configured:
        report.skip("codex resolves the entry", "not configured")
    else:
        result = run(f'"{codex}" mcp get nvda', {}, timeout=120)
        output = (result.stdout or "") + (result.stderr or "")
        report.check("codex resolves the server", "stdio" in output, output.strip().splitlines()[0] if output.strip() else "")
        report.check(
            "codex resolves the same executable",
            resolved.lower() in output.lower().replace("\\\\", "\\"),
        )

    report.section("a stale entry is still recognised")

    for client_id in CLIENTS:
        info = by_id.get(client_id)
        if info is None or not info.detected:
            continue
        stale = exe.with_name("nvda-mcp-server-old.exe")
        ok, preview, message = preview_client_configuration(client_id, stale)
        report.check(
            f"{client_id}: preview notices the entry points elsewhere",
            ok and preview.has_conflict,
            message,
        )

    failed = report.failed
    total = len(report.checks)
    print(f"\n{'PASS' if not failed else 'FAIL'} {total - len(failed)}/{total} checks")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
