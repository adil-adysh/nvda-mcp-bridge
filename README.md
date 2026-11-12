
# NVDA MCP Bridge

A thread-safe NVDA add-on that exposes NVDA internals as an MCP-compatible interface, enabling direct execution and inspection of NVDA APIs from VS Code.

---

## What this enables

* Execute Python inside a **live NVDA instance**
* Inspect NVDA internals (focus, objects, config, addons)
* Replace manual console probing with **MCP-driven inspection**
* Validate APIs before implementing them in your add-on

---

## Quick Start (Single-Package Experience)

**Goal:** Install the bridge, deploy the bundled MCP companion, and connect an MCP client (VS Code, Claude Desktop, Cursor) without manual binary builds or copying authentication tokens.

---

### 1. Install the Add-on in NVDA

1. Download the release package:
   ```
   nvdaMcpBridge-1.0.0.nvda-addon
   ```
2. Open the `.nvda-addon` file with NVDA to install it.
3. Restart NVDA when prompted.

---

### 2. Install the Bundled MCP Companion

1. In NVDA, open **NVDA Menu (`NVDA+N`) → Tools → NVDA MCP Bridge → Install or Repair MCP Companion...** (or via NVDA Settings).
2. When prompted, select **Yes**.
3. The add-on safely extracts and verifies the bundled companion binary to:
   ```
   %LOCALAPPDATA%\Programs\NVDA MCP Bridge\bin\nvda-mcp-server.exe
   ```
   *(Per-user installation — no administrator privileges required).*

---

### 3. Configure Your MCP Client Automatically

1. Open **NVDA Menu (`NVDA+N`) → Tools → NVDA MCP Bridge → Configure MCP Clients...**.
2. The configurator automatically detects supported clients (Visual Studio Code, Claude Desktop, Claude Code, Codex, Cursor, Antigravity) and safely merges the `nvda` server entry.
3. Existing settings and other MCP servers are completely preserved, and a timestamped backup (`.bak.<timestamp>`) is created before modifying any configuration.
4. `python scripts/client_config_acceptance.py` checks the result against the clients themselves: it has each vendor's own command write an entry into a throwaway home directory and compares that with ours, then asks Claude Code to connect through the real file.

---

### 4. Manual MCP Client Configuration (Alternative)

If configuring an MCP client manually, add the following to your client's MCP configuration:

#### Claude Desktop (`%APPDATA%\Claude\claude_desktop_config.json`)
```json
{
  "mcpServers": {
    "nvda": {
      "command": "%LOCALAPPDATA%\\Programs\\NVDA MCP Bridge\\bin\\nvda-mcp-server.exe",
      "args": []
    }
  }
}
```

#### Visual Studio Code (`%APPDATA%\Code\User\mcp.json` or `.vscode/mcp.json`)
```json
{
  "servers": {
    "nvda": {
      "type": "stdio",
      "command": "%LOCALAPPDATA%\\Programs\\NVDA MCP Bridge\\bin\\nvda-mcp-server.exe",
      "args": []
    }
  }
}
```

#### Cursor (`%USERPROFILE%\.cursor\mcp.json`)
```json
{
  "mcpServers": {
    "nvda": {
      "command": "%LOCALAPPDATA%\\Programs\\NVDA MCP Bridge\\bin\\nvda-mcp-server.exe",
      "args": []
    }
  }
}
```

#### Claude Code (CLI)

Claude Code writes the entry itself when you run:

```powershell
claude mcp add -s user nvda -- "%LOCALAPPDATA%\Programs\NVDA MCP Bridge\bin\nvda-mcp-server.exe"
```

It records the server in the user-scope `mcpServers` object of `%USERPROFILE%\.claude.json`, alongside the account and history state that file already holds:

```json
{
  "mcpServers": {
    "nvda": {
      "type": "stdio",
      "command": "%LOCALAPPDATA%\\Programs\\NVDA MCP Bridge\\bin\\nvda-mcp-server.exe",
      "args": [],
      "env": {}
    }
  }
}
```

#### Codex (desktop app and CLI)

The app and the CLI read the same `%USERPROFILE%\.codex\config.toml`, so one entry serves both. The equivalent one-liner is:

```powershell
codex mcp add nvda -- "%LOCALAPPDATA%\Programs\NVDA MCP Bridge\bin\nvda-mcp-server.exe"
```

which adds this table to `%USERPROFILE%\.codex\config.toml`. Codex keeps its model, trust and plugin settings in that same file, so add the table rather than replacing the file:

```toml
[mcp_servers.nvda]
command = 'the full path to nvda-mcp-server.exe'
```

Restart the Codex app if it is running; it reads its configuration when it starts.

> **Note on Authentication:** You do **not** need to copy or paste authentication tokens into client configurations. The companion binary automatically discovers the bridge credential generated in `%APPDATA%\nvda\nvda-mcp-bridge\token.key` using Windows user security controls.

---

### 5. Permission Controls and Developer Mode

By default, the bridge enforces a conservative, safe permission policy:
* **Safe defaults enabled:** `read_only` (focus/object inspection, window titles) and `speech_output` (`speak_text`, `cancel_speech`).
* **Input control:** `press` requires granting the `input_control` capability.
* **NVDA Lifecycle:** `restart_nvda` and `reload_plugins` require `nvda_lifecycle`.
* **Developer Mode:** Arbitrary Python execution (`execute_python`) is disabled by default.

#### Managing Permissions in NVDA
Open **NVDA Menu → Preferences → Settings (`NVDA+Control+G`) → NVDA MCP Bridge**:
* Check/uncheck **Allow MCP Input Control**
* Check/uncheck **Allow MCP Diagnostic Inspection**
* Check/uncheck **Allow MCP NVDA Lifecycle actions**
* Set **Elevation window** to choose how long an approval lasts: 1, 5, 10, 15 or 30 minutes. The value is saved, capped at 30 minutes so elevation can never become permanent, and applies to the next approval — an approval already in progress keeps the window it was granted with.
* Click **Enable Developer Mode (…)…** to approve Python execution. A screen-reader confirmation dialog will appear explaining the security risk and stating the window you will actually get. Approving grants the developer capability atomically with an auto-expiration timer.
* Click **Disable Developer Mode** to immediately revoke execution rights.
* Click **Reset Permissions to Safe Defaults** to revoke all elevated permissions.
* Developer operations are strictly blocked on the Windows secure desktop under all circumstances.

---

### 6. Development Guide (Building from Source)

For contributors and developers building the add-on from source:

#### Prerequisites
* Python 3.11+
* Rust 1.80+ with `x86_64-pc-windows-msvc` toolchain
* SCons and Markdown (`pip install scons markdown`)
* Gettext tools (optional, for localization)

#### Build Steps

`SConstruct` owns the whole release. One command compiles the companion, stages
it inside the add-on with a manifest whose checksum and API level come from the
crate itself, and assembles the `.nvda-addon`:

```powershell
scons
```

That is the only step. It is also the step that cannot be half-done: the archive
waits for the staged binary and manifest, so a release cannot ship a companion
that disagrees with what the add-on says about it. Rebuilds are incremental —
`cargo` runs only when a Rust source, `Cargo.toml` or `Cargo.lock` changed.

Useful variations:

```powershell
scons companion=0            # build the add-on without touching Rust (no toolchain, CI, translators)
scons pot                    # translation template only; does not build the companion
scons -c                     # clean the archive and generated docs
python scripts/package_companion.py            # same companion step, standalone
python scripts/package_companion.py --no-build # restage a binary cargo already built
python scripts/validate_addon_package.py       # check the archive's contents
```

Prerequisites for the companion step are Rust and `cargo` on `PATH`. Without
them `scons` stops with a message that names both options — install Rust, or
build with `companion=0` — rather than producing an add-on whose bundled
companion is stale. That failure mode is real: the add-on once shipped a
companion speaking api_level 2 next to a bridge speaking 3, and the companion
refused the catalog and offered no tools at all. The companion now accepts a
bridge that lists its level in `supported_api_levels`, and refuses one that does
not — with a message naming both, instead of silence.

---

## Verify Speech and Focus Inspection (First Win)

In the **VS Code chat interface with MCP enabled**, invoke:

Use the NVDA MCP tool `speak_text` to say: "Hello from NVDA MCP Bridge".

Or test tone generation interactively:
Use the tool `press` with `keys="windows+d"` to toggle the desktop.

---

### ✅ Expected result

* You hear three tones:

  * medium pitch
  * higher pitch
  * highest pitch (longer)

This confirms:

> VS Code chat → MCP → Bridge → NVDA execution is working

---

## Inspect NVDA (example)

Once execution works, inspect runtime state.

Use the NVDA MCP tool `execute_python` to run:

import addonHandler

addons = addonHandler.getRunningAddons()
[result.name for result in addons]

---

### ✅ Expected result

* A list of currently loaded add-ons returned in VS Code

---

## Exploring NVDA APIs interactively

Use Python introspection to understand NVDA internals:

Use the NVDA MCP tool `execute_python` to run:

import tones

tones.beep(100, 100)
tones.beep(200, 100)
tones.beep(400, 100)

help(tones.beep)

---

### What this gives you

* Immediate feedback from NVDA
* Function signatures via `help(...)`
* Faster understanding of internal APIs

---

## Core workflow

1. Use `execute_python` to probe NVDA internals
2. Inspect behavior and outputs
3. Validate APIs and patterns
4. Implement the same logic in your add-on

---

## Available endpoints

The bridge runs locally:

```
http://127.0.0.1:8765
```

### Endpoints

* `GET /health` — bridge status
* `GET /tools` — discover available tools
* `POST /action` — execute tool actions

---

## Example (direct HTTP)

```powershell
$token = (Get-Content "$env:APPDATA\nvda\nvda-mcp-bridge\token.key").Trim()
$headers = @{ 'X-NVDA-Bridge-Token' = $token }

$body = '{
  "request_id": "demo-1",
  "action": "speak_text",
  "params": { "text": "Hello from the bridge" }
}'

Invoke-WebRequest `
  -Uri http://127.0.0.1:8765/action `
  -Method POST `
  -Headers $headers `
  -Body $body `
  -ContentType 'application/json'
```

---

## Available tools

Use `GET /tools` for the authoritative list, including per-tool `outputSchema`
and a `schema_hash` that changes whenever the contract does.

### Observation (start here)

* `observe` — focus, ancestors, children and dialog in one call, each with a `ref`
* `read` — text by scope (`line`, `selection`, `document`, `subtree`)
* `wait_for_events` — block until the next coalesced UI transition

### Reading a document (browsers, Word, PDFs)

* `start_browse_mode` — build the document model NVDA+space would build, for
  documents read in focus mode (Word, some PDF readers). An action of its own
  because it changes the session; `created: false` when one already existed
* `list_reading_targets` — headings, links, landmarks, form fields and tables in
  reading order, without moving, with heading levels
* `navigate_reading` — move the reading position like the quick nav key, with
  `steps`, and report where it landed

Both reading calls take `timeout_ms` (default 1000) because they run on NVDA's
main thread, where a big document is otherwise the only limit. They answer with
`stopped` — `end`, `limit`, `steps`, `stalled` or `timeout` — so a partial
answer is never presented as a complete one.

### Action (by `ref`, settled)

* `invoke` — press an object's default or named action
* `set_value` — platform value pattern when available, keyboard otherwise
* `focus`, `navigate`, `scroll`
* `press` — keyboard keys, with an optional `expect` postcondition
* `click_at` — absolute pointer click, last resort
* `dismiss` — leave a dialog (needs the `dialog_control` capability)

Every action returns `result.changed`, `result.delta` and `result.settled_ms`, so
"the call ran" is distinguishable from "the UI did what was meant".

### Inspection and diagnostics

* `inspect_object`, `inspect_app_module`, `get_focus_lineage`
* `walk_object_tree`, `list_loaded_addons`, `health_snapshot`, `get_logs`
* `search_logs` — structured, paginated search over logical native-log records
  (multiline tracebacks stay one record; unknown levels are preserved)
* `get_log_context` — expand a result's `entry_id` into the surrounding
  chronological evidence, across threads
* `summarize_logs` — bounded, deterministic severity/source/pattern/exception
  statistics, warning groups, time buckets, COM/UIA noise share and anomaly
  candidates
* `get_action_trace` — read back the diagnostic timeline of one action
* `speak_text`, `show_message`

The three log tools use NVDA's canonical active log, the existing bridge token
and the `diagnostics` permission. They are read-only, stream the file, never
accept a path, and never change NVDA's logging level. `get_logs` is retained
unchanged for compatibility.

Start an investigation with `summarize_logs` when the source is unknown, then
`search_logs`, then `get_log_context`. For a known action, start with
`get_action_trace` and use `search_logs(trace_id=...)` to find the native
records that explicitly carry the id. See
[docs/LOG_DIAGNOSTICS.md](docs/LOG_DIAGNOSTICS.md) for schemas, severity and
source semantics, pagination/cursor rules, date limitations, redaction limits
and worked examples.

### Diagnosing an action

Every action response carries `trace_id` and a compact `trace` summary
(`outcome`, `duration_ms`, exception type/stage, event range, projection change,
expectation status, observation completeness). Pass that id to
`get_action_trace` for the full record: lifecycle milestones, the structured
exception and its traceback, the event window it spanned, the baseline and final
projection with their delta, the expected-postcondition result, best-effort
native log matches, and the list of things the record cannot prove.

`result.changed` is a projection diff, not a statement about the whole screen,
and an `expect` that does not match is reported separately from a handler
failure. See [docs/ACTION_TRACE.md](docs/ACTION_TRACE.md) for the schema, the
identifier semantics and the retention limits.

### Development

* `execute_python`

### Removed at api_level 4

`list_reading_targets` and `navigate_reading` no longer take `create`. A read is
not allowed to start browse mode — a `read_only` client could otherwise change
the user's session — so it became its own action:

* `create: true` on either reading tool → `start_browse_mode`

### Removed at api_level 3

The v1 aliases were retired rather than deprecated again. Each has had a
replacement since api level 2:

* `get_current_focus`, `get_navigator_object` → `observe`
* `read_current_line` → `read`
* `move_focus` → `navigate`
* `send_keyboard` → `press`
* `get_desktop_object` → `inspect_object(target="desktop")`
* `get_window_title` → `observe().window`

A call to a removed name is `4102`, with the action named. `GET /tools` is the
authoritative list for the level this bridge serves.

---

## Architecture overview

The bridge implements a layered design:

1. **HTTP transport**

   * Local server on `127.0.0.1:8765`
   * Handles `/health`, `/tools`, `/action`

2. **Tool registry**

   * Manifest-driven tool definitions
   * JSON schema validation

3. **Main thread execution**

   * All NVDA calls routed through a `MainThreadExecutor`
   * Prevents unsafe threading access

4. **NVDA add-on runtime**

   * Runs as a global plugin inside NVDA

---

## Configuration

* Localhost-only access (`127.0.0.1`)
* Auth via `X-NVDA-Bridge-Token`
* Configure the same token in:

  * MCP server
  * VS Code environment

---

## Prerequisites

* Windows 10/11
* NVDA with add-on support
* Python ≥ 3.12
* `scons` (`pip install scons`)
* `markdown` (`pip install markdown`)
* `cargo` + `rustup` (for MCP server)
* `gettext` (optional, for docs build)

---

## Development notes

* Python ≥ 3.12
* Strict typing (`pyright`)
* Linting via `ruff`
* Add-on code:

  ```
  addon/globalPlugins/nvdaMcp-bridge/
  ```
* Tool definitions are manifest-driven
* Protocol schemas:

  ```
  docs/protocol/
  ```

---

## Companion MCP server

Located in:

```
rust_mcp_bridge/
```

Responsibilities:

* Discover tools from the bridge
* Expose them via MCP (stdio)
* Enable VS Code integration

---

## Contributing

Follow Conventional Commits:

* `feat:` new features
* `fix:` bug fixes
* `docs:` documentation
* `chore:` maintenance

Ensure:

* Tool changes are reflected in the manifest
* Behavior matches runtime discovery

---

## License

See `LICENSE`.
