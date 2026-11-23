# NVDA MCP Bridge

**Give AI assistants and other MCP clients safe, structured access to a running NVDA screen reader.**

NVDA MCP Bridge connects the [Model Context Protocol](https://modelcontextprotocol.io/) to NVDA. It has two parts:

- **NVDA add-on** — a local, authenticated HTTP bridge inside NVDA, listening on `127.0.0.1:8765`.
- **Bundled MCP server** — `nvda-mcp-server.exe`, a Rust companion that speaks MCP over stdio and translates tool calls into bridge requests.

The add-on queues every NVDA call on NVDA's main thread, so tools behave like key presses and scripts NVDA itself runs. The companion is checksum-verified and installed with the add-on; no separate download is needed.

| | |
|---|---|
| Add-on version | `0.1.0` (first public preview) |
| Bridge API level | `1` |
| Bridge protocol | `nvda-bridge-protocol` `1.0.0` |
| Minimum NVDA | `2026.1.0` |
| Platform | Windows 10/11, x64 |
| License | GPL v2 |

## What you can do

- **Observe** the focused control, dialog, foreground window and their references.
- **Read** a line, selection, document or object subtree, including Word, PDF and browser documents.
- **Act** on controls by reference: invoke, focus, set values, press keys, scroll, click and dismiss dialogs.
- **Navigate** documents with headings, links, landmarks, form fields and tables.
- **Inspect** NVDA objects, app modules, loaded add-ons, bridge health and action traces.
- **Search logs** with structured, paginated queries and bounded summaries.
- **Speak** text and show messages through NVDA.
- **Observe speech** with the separate, opt-in `wait_for_speech` tool after enabling its permission in NVDA Settings.
- **Control NVDA** with restart and plug-in reload actions when explicitly permitted.
- **Run Python** in NVDA's process only when developer mode is enabled.

## Requirements

- Windows 10 or Windows 11, 64-bit.
- NVDA `2026.1` or later.
- An MCP client: Visual Studio Code, Claude Desktop, Claude Code, Codex, Cursor, Google Antigravity, Roo Code, or any client that launches a stdio MCP server.

## Install

### 1. Install the add-on

1. Download `nvdaMcpBridge-0.1.0.nvda-addon` from the [v0.1.0 release](https://github.com/adil-adysh/nvda-mcp-bridge/releases/tag/v0.1.0).
2. Open the file with NVDA, or use **NVDA Menu → Tools → Add-on Store → Install from file**.
3. Restart NVDA when prompted.

### 2. Install the bundled MCP companion

1. In NVDA, open **NVDA Menu → Tools → NVDA MCP Bridge → Install or Repair MCP Companion...**.
2. Confirm the prompt. The add-on verifies the bundled binary and installs it to:
   ```
   %LOCALAPPDATA%\Programs\NVDA MCP Bridge\bin\nvda-mcp-server.exe
   ```
3. No administrator rights are required.

Installing or repairing stops companion processes started from that install folder, refreshes the bridge authentication token, and replaces the binary. Restart MCP clients after the operation so their companions read the new token. Companion processes installed in other folders are left running, but their old token will no longer authenticate. If `NVDA_BRIDGE_TOKEN` is set as an environment override, install/repair fails before changing files because the add-on cannot rotate an externally managed token.

### 3. Connect your MCP client

The recommended path is automatic:

1. Open **NVDA Menu → Tools → NVDA MCP Bridge → Configure MCP Clients...**.
2. Select the clients you want, review the preview, and choose **Configure Selected**.
3. The configurator merges an `nvda` server entry into each client's own configuration format. It preserves existing settings and other servers, and writes a timestamped `.bak` backup before changing a file.
4. Restart the selected clients so they pick up the new server.

If you prefer to configure a client by hand, see [Manual client configuration](#manual-client-configuration).

> **First public preview:** `0.1.0` is the first GitHub release. The `0.10.0`–`0.12.0` entries in the changelog are internal development builds. Package version, API level `1`, and protocol version `1.0.0` are separate version lines.

## Manual client configuration

Use the full path to the installed companion. Replace `<you>` with your Windows user name.

```text
C:\Users\<you>\AppData\Local\Programs\NVDA MCP Bridge\bin\nvda-mcp-server.exe
```

You do **not** need to copy the bridge token into client configuration. The companion discovers it from `%APPDATA%\nvda\nvda-mcp-bridge\token.key` using the current Windows user's ACLs.

### Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "nvda": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\Programs\\NVDA MCP Bridge\\bin\\nvda-mcp-server.exe",
      "args": []
    }
  }
}
```

### Visual Studio Code

Edit `%APPDATA%\Code\User\mcp.json`, or a workspace `.vscode/mcp.json`:

```json
{
  "servers": {
    "nvda": {
      "type": "stdio",
      "command": "C:\\Users\\<you>\\AppData\\Local\\Programs\\NVDA MCP Bridge\\bin\\nvda-mcp-server.exe",
      "args": []
    }
  }
}
```

### Cursor

Edit `%USERPROFILE%\.cursor\mcp.json`:

```json
{
  "mcpServers": {
    "nvda": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\Programs\\NVDA MCP Bridge\\bin\\nvda-mcp-server.exe",
      "args": []
    }
  }
}
```

### Claude Code

Claude Code can write the user-scope entry itself:

```powershell
claude mcp add -s user nvda -- "$env:LOCALAPPDATA\Programs\NVDA MCP Bridge\bin\nvda-mcp-server.exe"
```

### Codex

The desktop app and CLI read the same `%USERPROFILE%\.codex\config.toml`:

```powershell
codex mcp add nvda -- "$env:LOCALAPPDATA\Programs\NVDA MCP Bridge\bin\nvda-mcp-server.exe"
```

The equivalent table is:

```toml
[mcp_servers.nvda]
command = 'C:\Users\<you>\AppData\Local\Programs\NVDA MCP Bridge\bin\nvda-mcp-server.exe'
```

Restart the Codex app if it is already running.

## Verify the installation

### Check the bridge

With NVDA running and the add-on enabled:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health | ConvertTo-Json -Depth 6
```

The response should report:

- `bridge_version`: `0.1.0`
- `api_level`: `1`
- `supported_api_levels`: `[1]`

`/health` is unauthenticated. `/tools`, `/state`, `/events` and `/action` require the bridge token.

### Ask your client for one safe action

Start with observation, which needs only the default `read_only` permission:

```text
Use the nvda tool `observe` with no arguments and tell me what has focus.
```

Speech output is enabled by default. Reading NVDA's spoken announcements uses a separate permission and is off by default:

```text
Enable Allow MCP Speech Observation in NVDA MCP Bridge settings, then use the nvda tool `wait_for_speech`.
```

For example, to speak text:

```text
Use the nvda tool `speak_text` to say "NVDA MCP Bridge is connected".
```

If the client lists the `nvda` tools and `observe` returns a focus object and references, the whole path is working:

```text
MCP client → nvda-mcp-server.exe → 127.0.0.1:8765 → NVDA main thread
```

## Permissions and security

The bridge binds to loopback only and authenticates every protected request with `X-NVDA-Bridge-Token`. The token is generated per installation, stored under `%APPDATA%\nvda\nvda-mcp-bridge\token.key`, and protected with a current-user-only ACL. A successful companion install or repair rotates the token atomically; the running bridge switches to it immediately.

Defaults are intentionally conservative:

| Capability | Default | Unlocks |
|---|---|---|
| `read_only` | **Enabled** | `observe`, `read`, `locate`, `wait_for_events`, `list_reading_targets`, `find_tools`, `get_tool_schema` |
| `speech_output` | **Enabled** | `speak_text`, `show_message` |
| `speech_observation` | Off | `wait_for_speech` (reads bounded NVDA speech announcements and speech-control events) |
| `input_control` | Off | `press`, `invoke`, `set_value`, `focus`, `activate_window`, `navigate`, `navigate_reading`, `scroll`, `click_at`, `start_browse_mode` |
| `diagnostics` | Off | `health_snapshot`, `inspect_object`, `inspect_app_module`, `walk_object_tree`, `get_focus_lineage`, `list_loaded_addons`, `get_logs`, `search_logs`, `get_log_context`, `summarize_logs`, `get_action_trace` |
| `nvda_lifecycle` | Off | `restart_nvda`, `reload_plugins` |
| `dialog_control` | Off | `dismiss` |
| `developer` | Off, time-limited | `execute_python` |

Manage capabilities in **NVDA Menu → Preferences → Settings → NVDA MCP Bridge**:

- Enable input, diagnostics, lifecycle and dialog control individually.
- Enable **Allow MCP Speech Observation** separately before a client can read NVDA announcements. It is off by default, and observation is blocked on secure desktops or protected focus.
- Choose the Developer Mode elevation window, from 1 minute to 1 day. The selected duration persists across NVDA restarts, and a locally approved grant stays active only until its original expiry or until you disable it.
- Enable developer mode through a confirmation dialog that explains the risk and the time limit. Developer mode expires automatically.
- Set the maximum `execute_python` wait to 1, 2, 5, 10 or 15 minutes. Each call defaults to one minute and is capped by this saved ceiling; the setting survives NVDA restarts. If the wait expires after code starts, it continues running in NVDA.
- **Reset Permissions to Safe Defaults** revokes every elevated capability immediately.

Developer execution is blocked on the Windows secure desktop. No tool can enable developer mode on its own: it requires a local confirmation with a separate approval token.

Anyone who can run code as your Windows user can read the token and control NVDA through the bridge. Treat the token file as a credential, and grant elevated capabilities only while a client actually needs them.

## Tool catalog

The authoritative list is always the bridge's `/tools` endpoint; the MCP companion exposes the same catalog. The registry contains 34 tools. Discovery returns 33 by default because `execute_python` is developer-only.

| Category | Tools | Use |
|---|---|---|
| Observation | `observe`, `read`, `locate`, `wait_for_events`, `list_reading_targets` | Inspect the screen and documents without changing them |
| Action | `activate_window`, `click_at`, `dismiss`, `focus`, `invoke`, `navigate`, `navigate_reading`, `scroll`, `set_value`, `start_browse_mode` | Move through and operate the UI |
| Input | `press` | Send one gesture or an ordered sequence |
| Discovery | `find_tools`, `get_tool_schema` | Search the catalog and fetch one schema on demand |
| Diagnostics | `get_action_trace`, `get_focus_lineage`, `get_log_context`, `get_logs`, `health_snapshot`, `inspect_app_module`, `inspect_object`, `list_loaded_addons`, `search_logs`, `summarize_logs`, `walk_object_tree` | Inspect NVDA objects, add-ons, logs and action traces |
| Speech | `speak_text`, `wait_for_speech` | Speak through NVDA; read announcements only with the separate speech-observation permission |
| UI | `show_message` | Show a message to the user |
| System | `reload_plugins`, `restart_nvda` | Reload add-ons or restart NVDA |
| Developer | `execute_python` | Run Python in NVDA's process when explicitly authorized |

### References and verification

Observation tools return short references such as `f3` or `w1`. Pass a reference to an action tool; it stays valid until its identity, window, TTL or bridge instance changes.

Every action result carries `state_version`, `scope`, `changed`, `delta` and `settled_ms`. An `expect` object adds a verification result, so a client can distinguish "the call returned" from "the UI reached the expected state".

Every action also carries a `trace_id`. Pass it to `get_action_trace` for lifecycle stages, the exception and traceback, the event window, projection deltas and native log matches.

### Example prompts

```text
Observe the current window and describe what has focus.
```

```text
Read the document from the beginning and list its headings.
```

```text
Press control+s, then verify that the document title no longer contains an asterisk.
```

```text
Summarize NVDA's log for errors in the last five minutes, then show the context around the first one.
```

```text
Use the nvda tool `get_action_trace` for the last action and explain why it was not confirmed.
```

## How it works

```text
MCP client (VS Code, Claude, Cursor, Codex, ...)
        │  MCP over stdio (JSON-RPC 2.0)
        ▼
nvda-mcp-server.exe
        │  HTTP on 127.0.0.1:8765
        │  X-NVDA-Bridge-Token
        ▼
NVDA MCP Bridge add-on
        │  main-thread executor
        ▼
NVDA APIs and accessibility objects
```

The companion discovers the tool catalog from `/tools` at startup, retries while NVDA is unavailable, and validates `structuredContent` against the bridge's output schemas. `outputSchema` is omitted from `tools/list` by default to keep the catalog small; `get_tool_schema(name, include_output_schema=true)` returns it on demand.

## Companion configuration

The companion reads these environment variables. They can be set in the MCP client's server entry if needed.

| Variable | Default | Effect |
|---|---|---|
| `NVDA_BRIDGE_URL` | `http://127.0.0.1:8765` | Bridge address |
| `NVDA_BRIDGE_TOKEN` | Auto-discovered token file | Explicit token, for a different user or container |
| `NVDA_BRIDGE_TIMEOUT_SECS` | `15` | HTTP timeout in seconds |
| `NVDA_BRIDGE_CATEGORIES` | all | Comma-separated category allow-list; the only option that removes tools from the catalog |
| `NVDA_BRIDGE_OUTPUT_SCHEMAS` | `0` | Set to `1` to advertise every `outputSchema` in `tools/list` |
| `NVDA_BRIDGE_TOOLS_PAGE_SIZE` | `100` | Maximum tools per `tools/list` page (1–1000) |
| `LOG_LEVEL` | `INFO` | Companion log level: `TRACE`, `DEBUG`, `INFO`, `WARN` or `ERROR` |

## Development

### Prerequisites

- Python 3.12 or later.
- Rust 1.95 (the exact channel is pinned in `rust-toolchain.toml`).
- SCons and Markdown: `python -m pip install scons markdown`.
- Gettext tools for translated documentation (optional).
- NVDA source is not required for the unit tests.

### Build

`SConstruct` owns the release. One command builds the Rust companion, stages it inside the add-on with a checksum and API-level manifest, generates translated documentation, and assembles the `.nvda-addon`:

```powershell
scons
```

Useful variations:

```powershell
scons companion=0            # Python add-on only; no Rust toolchain needed
scons pot                    # regenerate the translation template
scons -c                     # clean generated archives and docs
python scripts/package_companion.py            # companion step on its own
python scripts/package_companion.py --no-build # stage an existing cargo release build
python scripts/validate_addon_package.py       # verify archive contents and checksums
```

The archive waits for the staged companion and manifest, so a release cannot ship a companion that disagrees with the bridge's API level.

### Tests and checks

```powershell
python -m pytest -q
cargo test --manifest-path rust_mcp_bridge/Cargo.toml
python scripts/generate_protocol.py --check
pre-commit run --all-files
```

`python scripts/generate_protocol.py` regenerates the Python and Rust protocol modules and the JSON schemas after a contract change. Do not edit generated protocol files by hand.

### Version sources

The release version must agree in every file that declares it:

- `buildVars.py`
- `pyproject.toml`
- `rust_mcp_bridge/Cargo.toml`
- `docs/protocol/protocol-constants.json`
- `schema.json`

The build workflow verifies these values and rejects a `v*` tag that does not match.

### Releasing

The GitHub workflow builds and validates the add-on on every push and pull request. Pushing a tag in the form `v<version>` publishes a release with:

- the `.nvda-addon`
- the translation template
- the standalone `nvda-mcp-server.exe`
- `SHA256SUMS.txt`
- release notes generated with `git-cliff`

## Troubleshooting

### The client shows no `nvda` tools

1. Confirm NVDA is running and the add-on is enabled in NVDA's Add-on Manager.
2. Open `/health` and check that it returns `api_level: 1`.
3. Reinstall the companion from **NVDA Menu → Tools → NVDA MCP Bridge → Install or Repair MCP Companion...**.
4. Restart the MCP client. The companion reads the catalog at startup.
5. If the client runs as a different user, set `NVDA_BRIDGE_TOKEN` explicitly.

### A client reports a bridge API level mismatch

The add-on and companion must be from the same release line. Reinstall the companion from the matching add-on package.

### A tool returns `permission_denied`

Enable the required capability in the add-on settings. Start with `read_only` and `speech_output`, then grant input or diagnostics only for the task at hand.

### Developer mode is unavailable

`execute_python` requires a local confirmation in NVDA and expires automatically. If NVDA restarts before that expiry, the locally approved grant resumes with the same remaining time. A remote MCP request cannot enable it.

### The bridge port is already in use

Stop the process listening on `127.0.0.1:8765`, then restart NVDA. Only one bridge instance can own the port.

## Project layout

```text
addon/                               NVDA add-on and global plug-in
  globalPlugins/nvdaMcp-bridge/      bridge, tools, settings and bundled companion
docs/                                protocol, tool contract, deployment and diagnostics
rust_mcp_bridge/                     Rust MCP server
scripts/                             protocol generation, packaging and acceptance probes
tests/                               Python unit and integration tests
buildVars.py                         add-on metadata and version
sconstruct                           release build
```

## Contributing

- Use [Conventional Commits](https://www.conventionalcommits.org/).
- Run the Python and Rust test suites, the protocol check, and pre-commit before opening a pull request.
- Regenerate protocol files with `python scripts/generate_protocol.py` after changing the contract.
- Keep every version source in sync; the build workflow enforces it.

## License

GPL v2. See [COPYING.txt](COPYING.txt) and [LICENSE](LICENSE).
