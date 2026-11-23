# NVDA MCP Server

![Version](https://img.shields.io/badge/version-0.1.0-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20x64-brightgreen)

Model Context Protocol (MCP) server for NVDA automation, built in Rust. Allows AI assistants like Claude to control NVDA through a standardized interface.

## Features

- Single-threaded Tokio runtime for compatibility with NVDA
- Dynamically discovers NVDA tools from bridge at startup
- JSON-RPC 2.0 stdio transport with MCP 2025-06-18 protocol
- Envelope-aware output validation with legacy fallback
- Atomic request ID generation for bridge actions
- Comprehensive logging and error handling

## 📋 Requirements

- **Windows x64** (Windows 10/11 or later)
- **NVDA** with nvda-mcp-bridge addon installed
- **Authentication**: Automatically detected from `%APPDATA%\nvda\nvda-mcp-bridge\token.key` or configured via `NVDA_BRIDGE_TOKEN`

## 🚀 Quick Start

### Installation

1. Download the latest release from [GitHub Releases](https://github.com/adil-adysh/nvda-mcp-bridge/releases)
2. Extract `nvda-mcp-server.exe` to a convenient location (e.g., `C:\Users\<user>\bin\`)
3. Ensure NVDA is running with the `nvdaMcpBridge` addon enabled (which automatically generates your secure token)

### Running Standalone

```powershell
# Auto-discovers token from %APPDATA%\nvda\nvda-mcp-bridge\token.key
& "C:\Users\<user>\bin\nvda-mcp-server.exe"

# Or explicitly pass token if running under a different user or container
$env:NVDA_BRIDGE_TOKEN = (Get-Content "$env:APPDATA\nvda\nvda-mcp-bridge\token.key").Trim()
& "C:\Users\<user>\bin\nvda-mcp-server.exe"
```

### Configure for Claude Desktop (Windows)

Add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "nvda": {
      "command": "C:\\Users\\<user>\\bin\\nvda-mcp-server.exe"
    }
  }
}
```

### Environment Variables

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `NVDA_BRIDGE_TOKEN` | Auto-discovered from `%APPDATA%` | ❌ No | Authentication token for NVDA bridge |
| `NVDA_BRIDGE_URL` | `http://127.0.0.1:8765` | ❌ No | Bridge server URL |
| `NVDA_BRIDGE_TIMEOUT_SECS` | `15` | ❌ No | HTTP bridge client timeout in seconds |
| `NVDA_BRIDGE_CATEGORIES` | all | ❌ No | Comma-separated category allow-list; the only lever that removes tools |
| `NVDA_BRIDGE_OUTPUT_SCHEMAS` | `0` | ❌ No | Set to `1` to advertise `outputSchema` in `tools/list`; omitted by default to halve the catalog |
| `NVDA_BRIDGE_TOOLS_PAGE_SIZE` | `100` | ❌ No | Maximum tools per `tools/list` page (1–1000) |
| `LOG_LEVEL` | `INFO` | ❌ No | Log level (TRACE, DEBUG, INFO, WARN, ERROR) |

## 🏗️ Architecture

```
AI Assistant (Claude/Cursor/etc)
        │
        ├─ Stdin (JSON-RPC 2.0 Request)
        │
    ┌───▼───────────────────┐
    │  NVDA MCP Server 0.1.0 │
    │    (Rust Binary)       │
    │                        │
    │ • Request ID Injection │
    │ • Output Validation    │
    │ • Tool Catalog         │
    └───┬───────────────────┘
        │
        ├─ Stdout (JSON-RPC 2.0 Response)
        │
        │ HTTP POST
        │ X-NVDA-Bridge-Token
        ▼
┌────────────────────────────┐
│  NVDA Bridge Add-on 0.1.0  │
│   (Inside NVDA Process)    │
│                            │
│ • Tool Execution           │
│ • NVDA Automation          │
│ • Error Handling           │
└────────────────────────────┘
        │
        ▼
    NVDA API
```

## 🛠️ Usage Examples

### Discover Available Tools

Tools are discovered automatically from the bridge at startup. Use Claude or your MCP client to list available tools.

### Common Tool Categories

The server provides access to NVDA tools across these categories:

- **Window & Focus** - Get window titles, switch windows, click positions
- **Text & Clipboard** - Read/write clipboard, get text from screen
- **Navigation** - Navigate NVDA objects, move cursor
- **Control** - Execute NVDA functions, trigger actions
- **Settings** - Read/adjust NVDA configuration

## 📁 Project Structure

```
.
├── src/
│   └── main.rs              # Main server implementation (815 lines)
├── .github/workflows/
│   └── release.yml          # CI/CD pipeline for automated releases
├── docs/
│   ├── BRIDGE_PROTOCOL.md   # Bridge protocol specification
│   └── CONVENTIONAL_COMMITS.md  # Commit message guidelines
├── Cargo.toml               # Rust package manifest
├── CHANGELOG.md             # Version history
├── cliff.toml               # Git-cliff changelog generation config
└── README.md                # This file
```

## 🔄 Protocol Details

### Bridge Protocol

The server implements **NVDA Bridge Protocol v2.x** with:

- **Envelope-based responses** - `{success, data, error}` wrapper format
- **Request ID injection** - Atomic counter with timestamp format: `mcp-{timestamp}-{sequence}`
- **Output type validation** - Resolves output schemas from bridge type registry
- **Legacy fallback** - Backward compatible with data-only responses

### MCP Protocol

- **Version** - 2024-11-05
- **Transport** - stdio (JSON-RPC 2.0)
- **Capabilities** - Tools (read-only)

For detailed protocol documentation, see [docs/BRIDGE_PROTOCOL.md](docs/BRIDGE_PROTOCOL.md).

## 🐛 Known Issues & Solutions

### NVDA Freezing

**Issue**: NVDA becomes unresponsive during server operation

**Root Cause**: Default multi-threaded Tokio runtime competed with NVDA's single-threaded event loop

**Solution**: The companion uses a single-worker-thread Tokio configuration to reduce contention.

**Verification**: Look for startup log: `single-worker-thread Tokio runtime`

### Missing Request ID

**Issue**: `Bridge returned 400: Missing 'request_id' field`

**Solution**: The companion injects request IDs into bridge action payloads.

### Output Schema Validation

**Issue**: `Bridge output validation failed` for envelope-shaped schemas

**Solution**: The companion validates envelope format before attempting data-only validation.

## 📝 Contributing

### Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(bridge): add new capability
fix(validation): handle edge case
docs: update protocol documentation
```

See [docs/CONVENTIONAL_COMMITS.md](docs/CONVENTIONAL_COMMITS.md) for detailed guidelines.

### Building from Source

```bash
git clone https://github.com/adil-adysh/nvda-mcp-bridge.git
cd nvda-mcp-bridge/rust_mcp_bridge
cargo build --release --target x86_64-pc-windows-msvc
```

### Running Tests

```bash
cargo test
```

## 🔄 Release Process

Releases use automated CI/CD:

1. **Commit** with conventional commit messages
2. **Tag** with the next semantic version (the first public release is `v0.1.0`)
3. **Push** tag to GitHub
4. **CI/CD Pipeline** automatically builds, generates changelog, and creates release

```bash
git tag -a v<version> -m "Release v<version>"
git push origin v<version>
```

## 📊 Version History

The companion is released as part of NVDA MCP Bridge. The first public
combined release is `v0.1.0`; its release notes are maintained in
[`docs/changelog.md`](../docs/changelog.md).

See the canonical [project changelog](../docs/changelog.md) for release history.

## 🔐 Security

- **No hardcoded credentials** - Requires `NVDA_BRIDGE_TOKEN` environment variable
- **HTTPS Ready** - Supports HTTPS URLs via `NVDA_BRIDGE_URL`
- **Request Isolation** - Each request gets unique atomic ID
- **Schema Validation** - Input/output validated against bridge schemas

## 🤝 Support

- **Issues**: [GitHub Issues](https://github.com/adil-adysh/nvda-mcp-bridge/issues)
- **Documentation**: [docs/](docs/) folder
- **Protocol Spec**: [docs/BRIDGE_PROTOCOL.md](docs/BRIDGE_PROTOCOL.md)
