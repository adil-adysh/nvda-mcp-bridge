# NVDA MCP Server - Complete Directory Structure

## Final Directory Layout

```
nvda-mcp/
│
├── addon/                                      # Add-on source code
│   ├── globalPlugins/
│   │   └── nvdaMcp/
│   │       ├── __init__.py                     # Global plugin lifecycle manager
│   │       ├── mcp_server.py                   # HTTP MCP server implementation
│   │       ├── nvda_bridge.py                  # NVDA API wrapper functions
│   │       └── lib/                            # Vendored dependencies (optional)
│   │           └── README.md                   # Dependency vendoring guide
│   │
│   └── manifest.ini                            # Add-on metadata
│
├── docs/                                       # Documentation
│   ├── MCP_SERVER_README.md                    # Comprehensive documentation
│   ├── QUICKSTART.md                           # Quick start guide
│   └── STRUCTURE.md                            # This file
│
├── site_scons/                                 # SCons build tools
│   └── site_tools/
│       ├── gettexttool/
│       │   └── __init__.py
│       └── NVDATool/
│           ├── __init__.py
│           ├── addon.py
│           ├── docs.py
│           ├── manifests.py
│           ├── typings.py
│           └── utils.py
│
├── .github/                                    # GitHub configuration
│   └── workflows/
│       └── build_addon.yml                     # Automated builds
│
├── buildVars.py                                # Build configuration
├── sconstruct                                  # SCons build script
├── manifest.ini.tpl                            # Manifest template
├── manifest-translated.ini.tpl                 # Translated manifest template
│
├── build.ps1                                   # Build helper script
├── vendor_dependencies.ps1                     # Dependency vendoring script
├── test_mcp_server.ps1                         # PowerShell test client
├── test_mcp_client.py                          # Python test client
│
├── readme.md                                   # Project README
├── changelog.md                                # Version history
├── COPYING.txt                                 # GPL v2 license
├── pyproject.toml                              # Python project config
├── .gitignore                                  # Git ignore rules
├── .pre-commit-config.yaml                     # Pre-commit hooks
│
└── .vscode/                                    # VS Code configuration
    └── settings.json
```

## Core Implementation Files

### [`addon/globalPlugins/nvdaMcp/__init__.py`](../addon/globalPlugins/nvdaMcp/__init__.py)
**Purpose**: Global plugin entry point  
**Responsibilities**:
- Initialize the MCP server on NVDA startup
- Manage server lifecycle (start/stop)
- Handle clean shutdown on NVDA exit
- Ensure exceptions don't crash NVDA

**Key Classes**:
- `GlobalPlugin`: Manages MCP server thread and lifecycle

### [`addon/globalPlugins/nvdaMcp/mcp_server.py`](../addon/globalPlugins/nvdaMcp/mcp_server.py)
**Purpose**: MCP HTTP server implementation  
**Responsibilities**:
- HTTP server on `127.0.0.1:8765`
- Token authentication via `X-MCP-Token` header
- MCP tool registration and execution
- Request routing and error handling

**Key Classes**:
- `MCPRequestHandler`: HTTP request handler
- `NVDAMCPServer`: Main server class with tool registry

**Endpoints**:
- `GET /health` - Health check (no auth)
- `GET /tools` - List available tools (auth required)
- `POST /call_tool` - Execute a tool (auth required)

### [`addon/globalPlugins/nvdaMcp/nvda_bridge.py`](../addon/globalPlugins/nvdaMcp/nvda_bridge.py)
**Purpose**: NVDA API wrapper functions  
**Responsibilities**:
- Thin wrappers around NVDA APIs
- Error handling and logging
- Return structured dictionaries

**Functions**:
- `get_current_focus()` - Get focused element info
- `read_current_line()` - Read current text line
- `speak_text(text)` - Speak using NVDA TTS
- `get_window_title()` - Get foreground window info
- `move_focus(direction)` - Navigate focus
- `get_navigator_object()` - Get navigator info
- `show_message(message)` - Show UI message

### [`addon/manifest.ini`](../addon/manifest.ini)
**Purpose**: Add-on metadata  
**Contents**:
- Add-on name, version, description
- Author and URL information
- NVDA version compatibility
- Documentation file reference

## Configuration Files

### [`buildVars.py`](../buildVars.py)
**Purpose**: Build system configuration  
**Key Variables**:
- `addon_info`: Add-on metadata (name, version, author, etc.)
- `pythonSources`: Python files to include
- `i18nSources`: Files for translation
- `excludedFiles`: Files to exclude from package

### [`sconstruct`](../sconstruct)
**Purpose**: SCons build script  
**Actions**:
- Builds `.nvda-addon` package file
- Processes translations
- Generates documentation HTML from markdown
- Validates manifest

### [`pyproject.toml`](../pyproject.toml)
**Purpose**: Python project configuration  
**Tools Configured**:
- Ruff (linting)
- Pyright (type checking)
- Project metadata

## Documentation Files

### [`docs/MCP_SERVER_README.md`](MCP_SERVER_README.md)
Comprehensive documentation covering:
- Architecture overview
- All available MCP tools
- Installation instructions
- Usage examples (PowerShell, Python, JavaScript)
- Security considerations
- Development guide
- Troubleshooting

### [`docs/QUICKSTART.md`](QUICKSTART.md)
Quick start guide with:
- Build instructions
- Installation steps
- Basic testing commands
- Simple examples

### [`docs/STRUCTURE.md`](STRUCTURE.md)
This file - explains the directory structure and file purposes.

## Test Files

### [`test_mcp_server.ps1`](../test_mcp_server.ps1)
PowerShell test script that:
- Tests all endpoints
- Validates authentication
- Exercises all MCP tools
- Provides colorized output

### [`test_mcp_client.py`](../test_mcp_client.py)
Python test client that:
- Tests all MCP tools
- Demonstrates Python API usage
- Can be used as a reference implementation

## Build Helper Scripts

### [`build.ps1`](../build.ps1)
Automated build script:
- Cleans build artifacts
- Runs SCons build
- Optionally installs add-on
- User-friendly output

Usage:
```powershell
.\build.ps1                # Build only
.\build.ps1 -Install       # Build and install
.\build.ps1 -Clean         # Clean then build
```

### [`vendor_dependencies.ps1`](../vendor_dependencies.ps1)
Dependency vendoring helper:
- Installs packages to `lib/` directory
- Supports requirements.txt
- Can install individual packages
- Clean option to remove all vendored deps

Usage:
```powershell
.\vendor_dependencies.ps1 -Packages "requests", "pydantic"
.\vendor_dependencies.ps1 -RequirementsFile requirements.txt
.\vendor_dependencies.ps1 -Clean
```

## Important Notes

### Threading Model
- MCP server runs in a daemon thread
- Uses `threading.Event()` for clean shutdown
- Server is non-blocking to NVDA's main thread
- HTTP server uses `handle_request()` in a loop

### Security Model
- Binds only to `127.0.0.1` (localhost)
- Requires `X-MCP-Token` header for all tool calls
- Default token: `nvda-mcp-secret-token-change-me`
- CORS headers allow browser-based clients

### Error Handling
- All functions return dictionaries with `error` key on failure
- Exceptions logged via `logHandler.log.error()`
- MCP server failures don't crash NVDA
- Graceful degradation

### Startup Sequence
1. NVDA loads all global plugins
2. `GlobalPlugin.__init__()` is called
3. MCP server instance created
4. Background thread started
5. HTTP server begins listening
6. "MCP Server started" logged

### Shutdown Sequence
1. NVDA begins shutdown
2. `GlobalPlugin.terminate()` is called
3. Shutdown event is set
4. Server stops accepting requests
5. Server closes socket
6. Thread joins with 5-second timeout
7. "MCP Server stopped" logged

## Dependencies

### Runtime Dependencies
**None** - Uses only Python standard library:
- `http.server` - HTTP server
- `json` - JSON serialization
- `threading` - Background thread management
- `sys`, `os` - System operations

### NVDA Modules Used
- `globalPluginHandler` - Plugin base class
- `addonHandler` - Add-on management
- `logHandler` - Logging
- `api` - NVDA object access
- `speech` - Speech output
- `winUser` - Windows API wrappers
- `ui` - User interface messages
- `textInfos` - Text navigation

### Build Dependencies
- Python 3.7+
- SCons
- markdown (for HTML docs)
- gettext (for translations)

## File Sizes

Approximate sizes:
- `__init__.py`: ~2.5 KB
- `mcp_server.py`: ~10 KB
- `nvda_bridge.py`: ~7 KB
- Total add-on: ~20 KB (without vendored dependencies)

## Extending the Add-on

### Adding New Tools
1. Add function to `nvda_bridge.py`
2. Register tool in `mcp_server.py` `__init__()` method
3. Define input schema (JSON Schema)
4. Test with test clients
5. Update documentation

### Adding Dependencies
1. Use `vendor_dependencies.ps1` to install to `lib/`
2. Verify license compatibility (GPL v2)
3. Update `lib/README.md` with package list
4. Test that NVDA can load the dependencies
5. Document in main README

### Changing Port or Token
Edit [`addon/globalPlugins/nvdaMcp/__init__.py`](../addon/globalPlugins/nvdaMcp/__init__.py):
- Change `port=8765` to your desired port
- Pass `auth_token="your-token"` to server constructor

## Git Workflow

```bash
# Clone repository
git clone https://github.com/nvda-addons/nvda-mcp.git
cd nvda-mcp

# Create feature branch
git checkout -b feature/my-feature

# Make changes
# ... edit files ...

# Build and test
.\build.ps1
.\test_mcp_server.ps1

# Commit
git add .
git commit -m "Add feature description"

# Push and create PR
git push origin feature/my-feature
```

## Continuous Integration

The `.github/workflows/build_addon.yml` file enables:
- Automatic builds on push
- Release creation on tags
- Artifact uploads
- Cross-platform testing (if configured)

## License

All files are GPL v2 licensed. See [COPYING.txt](../COPYING.txt).

## Questions?

- Issues: https://github.com/nvda-addons/nvda-mcp/issues
- Discussions: https://github.com/nvda-addons/nvda-mcp/discussions
- NVDA Add-ons List: https://nvda-addons.groups.io/g/nvda-addons
