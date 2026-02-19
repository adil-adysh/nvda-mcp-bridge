# NVDA MCP Server Add-on

## Overview

This NVDA add-on embeds a **Model Context Protocol (MCP) server** directly inside the NVDA screen reader process, enabling secure external automation and AI-assisted accessibility control.

## Features

- ✅ **In-Process MCP Server**: Runs inside NVDA, no external subprocess
- ✅ **Localhost-Only Binding**: Secure `127.0.0.1:8765` binding
- ✅ **Token Authentication**: Simple token-based security
- ✅ **Non-Blocking**: Uses background threading, doesn't block NVDA
- ✅ **Clean Lifecycle**: Starts with NVDA, stops cleanly on exit
- ✅ **Fault-Tolerant**: MCP failures don't crash NVDA
- ✅ **No External Dependencies**: Uses Python stdlib only (currently)

## Architecture

```
addon/
├── globalPlugins/
│   └── nvdaMcp/
│       ├── __init__.py          # Global plugin lifecycle
│       ├── mcp_server.py        # HTTP MCP server
│       ├── nvda_bridge.py       # NVDA API wrappers
│       └── lib/                 # Vendored dependencies (optional)
├── manifest.ini                 # Add-on metadata
└── buildVars.py                 # Build configuration
```

## Available MCP Tools

The server exposes these MCP tools:

### 1. `get_current_focus`
Get information about the currently focused UI element.

**Input**: None  
**Output**: 
```json
{
  "name": "Control name",
  "role": "Button",
  "roleText": "button",
  "value": "Control value",
  "description": "Control description",
  "states": "focused, focusable",
  "windowClassName": "Edit",
  "windowControlID": 1234
}
```

### 2. `read_current_line`
Read the current line from the focused text element.

**Input**: None  
**Output**:
```json
{
  "text": "Current line text",
  "lineNumber": 42
}
```

### 3. `speak_text`
Speak text using NVDA's speech synthesizer.

**Input**:
```json
{
  "text": "Text to speak"
}
```

**Output**:
```json
{
  "success": true,
  "spoken": "Text to speak"
}
```

### 4. `get_window_title`
Get the title of the current foreground window.

**Input**: None  
**Output**:
```json
{
  "title": "Window Title",
  "handle": 123456,
  "className": "WindowClass"
}
```

### 5. `move_focus`
Move focus to next or previous UI element.

**Input**:
```json
{
  "direction": "next"  // or "previous"
}
```

**Output**:
```json
{
  "success": true,
  "direction": "next",
  "newFocus": {
    "name": "Next control",
    "role": "button"
  }
}
```

### 6. `get_navigator_object`
Get information about the current navigator object.

**Input**: None  
**Output**: Similar to `get_current_focus`

### 7. `show_message`
Display a message to the user.

**Input**:
```json
{
  "message": "Hello from MCP!"
}
```

**Output**:
```json
{
  "success": true,
  "message": "Hello from MCP!"
}
```

## Installation

### Prerequisites

- **NVDA**: Version 2023.1 or later
- **Python**: 3.7+ (for building)
- **SCons**: For building the add-on package

### Building the Add-on

```powershell
# Clone the repository
git clone https://github.com/nvda-addons/nvda-mcp.git
cd nvda-mcp

# Build the add-on package
scons

# The .nvda-addon file will be created in the current directory
```

### Installing

1. Build the add-on (see above)
2. Double-click the generated `.nvda-addon` file
3. NVDA will prompt to install
4. Restart NVDA
5. The MCP server will start automatically on `127.0.0.1:8765`

### Verification

Check NVDA log (`NVDA+F1`) for:
```
INFO - NVDA MCP Server started successfully on 127.0.0.1:8765
```

Test the health endpoint:
```powershell
curl http://127.0.0.1:8765/health
```

Expected response:
```json
{
  "status": "ok",
  "service": "NVDA MCP Server"
}
```

## Usage

### Authentication

All requests (except `/health`) require the `X-MCP-Token` header:

```powershell
curl -H "X-MCP-Token: nvda-mcp-secret-token-change-me" http://127.0.0.1:8765/tools
```

⚠️ **Security**: Change the default token in `mcp_server.py` before production use!

### Listing Available Tools

```powershell
curl -H "X-MCP-Token: nvda-mcp-secret-token-change-me" http://127.0.0.1:8765/tools
```

### Calling a Tool

```powershell
curl -X POST http://127.0.0.1:8765/call_tool `
  -H "Content-Type: application/json" `
  -H "X-MCP-Token: nvda-mcp-secret-token-change-me" `
  -d '{\"name\": \"speak_text\", \"arguments\": {\"text\": \"Hello from MCP!\"}}'
```

### Python Client Example

```python
import requests
import json

BASE_URL = "http://127.0.0.1:8765"
TOKEN = "nvda-mcp-secret-token-change-me"
HEADERS = {
    "Content-Type": "application/json",
    "X-MCP-Token": TOKEN
}

def call_tool(tool_name, arguments=None):
    """Call an MCP tool"""
    response = requests.post(
        f"{BASE_URL}/call_tool",
        headers=HEADERS,
        json={
            "name": tool_name,
            "arguments": arguments or {}
        }
    )
    response.raise_for_status()
    return response.json()

# Example: Speak text
result = call_tool("speak_text", {"text": "Hello from Python!"})
print(json.dumps(result, indent=2))

# Example: Get current focus
focus = call_tool("get_current_focus")
print(json.dumps(focus, indent=2))

# Example: Read current line
line = call_tool("read_current_line")
print(json.dumps(line, indent=2))

# Example: Get window title
window = call_tool("get_window_title")
print(json.dumps(window, indent=2))

# Example: Move focus
move = call_tool("move_focus", {"direction": "next"})
print(json.dumps(move, indent=2))
```

### JavaScript/TypeScript Client Example

```typescript
const BASE_URL = "http://127.0.0.1:8765";
const TOKEN = "nvda-mcp-secret-token-change-me";

async function callTool(toolName: string, args: any = {}) {
  const response = await fetch(`${BASE_URL}/call_tool`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-MCP-Token": TOKEN,
    },
    body: JSON.stringify({
      name: toolName,
      arguments: args,
    }),
  });
  
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  }
  
  return response.json();
}

// Example usage
async function demo() {
  // Speak text
  await callTool("speak_text", { text: "Hello from JavaScript!" });
  
  // Get current focus
  const focus = await callTool("get_current_focus");
  console.log("Current focus:", focus);
  
  // Read current line
  const line = await callTool("read_current_line");
  console.log("Current line:", line);
}

demo().catch(console.error);
```

## Security Considerations

### ✅ Current Security Measures

- **Localhost-only binding**: Server only accepts connections from `127.0.0.1`
- **Token authentication**: All tool calls require valid token
- **No automatic data exposure**: Screen content only accessible via explicit tool calls
- **Exception handling**: Errors logged, don't crash NVDA
- **CORS headers**: Allow cross-origin requests for browser-based clients

### ⚠️ Security Recommendations

1. **Change the default token**: Edit `DEFAULT_TOKEN` in `mcp_server.py`
2. **Use environment variables**: For production, load token from environment
3. **Firewall rules**: Ensure Windows Firewall blocks external access to port 8765
4. **Monitor logs**: Check NVDA log for unauthorized access attempts
5. **Update regularly**: Keep NVDA and this add-on up to date

### 🔒 Token Configuration

To change the authentication token:

1. Edit [`mcp_server.py`](addon/globalPlugins/nvdaMcp/mcp_server.py#L18)
2. Change `DEFAULT_TOKEN = "nvda-mcp-secret-token-change-me"`
3. Rebuild and reinstall the add-on

Or pass a custom token when creating the server:

```python
server = NVDAMCPServer(auth_token="your-secure-token-here")
```

## Development

### Project Structure

```
nvda-mcp/
├── addon/                          # Add-on source code
│   ├── globalPlugins/
│   │   └── nvdaMcp/
│   │       ├── __init__.py         # Plugin lifecycle management
│   │       ├── mcp_server.py       # MCP HTTP server implementation
│   │       ├── nvda_bridge.py      # NVDA API wrappers
│   │       └── lib/                # Vendored dependencies (if needed)
│   └── manifest.ini                # Add-on metadata
├── buildVars.py                    # Build configuration
├── sconstruct                      # SCons build script
├── readme.md                       # This file
└── docs/                           # Additional documentation
```

### Adding New Tools

To add a new MCP tool:

1. **Add function to `nvda_bridge.py`**:
```python
def your_new_function(param1: str) -> Dict[str, Any]:
    """Your function description"""
    try:
        # Your NVDA API calls here
        result = do_something(param1)
        return {"success": True, "result": result}
    except Exception as e:
        logHandler.log.error(f"Error: {e}", exc_info=True)
        return {"error": str(e)}
```

2. **Register tool in `mcp_server.py`**:
```python
self.tools["your_new_tool"] = {
    "description": "Description of your tool",
    "inputSchema": {
        "type": "object",
        "properties": {
            "param1": {
                "type": "string",
                "description": "Description of param1"
            }
        },
        "required": ["param1"]
    },
    "handler": nvda_bridge.your_new_function
}
```

3. Rebuild and test!

### Vendoring Dependencies

Currently, this add-on uses **only Python standard library** modules, so no vendoring is required.

If you need to add external dependencies:

```powershell
# Install to lib directory
pip install --target addon/globalPlugins/nvdaMcp/lib package-name

# Or use a requirements file
pip install --target addon/globalPlugins/nvdaMcp/lib -r requirements.txt
```

See [`lib/README.md`](addon/globalPlugins/nvdaMcp/lib/README.md) for details.

### Testing

#### Manual Testing

1. Install the add-on in NVDA
2. Check NVDA log for startup messages
3. Test health endpoint: `curl http://127.0.0.1:8765/health`
4. Test tool listing: `curl -H "X-MCP-Token: <token>" http://127.0.0.1:8765/tools`
5. Test tool calls with Python/PowerShell client

#### Automated Testing

```powershell
# Run linting
ruff check addon/

# Run type checking (requires pyright)
pyright addon/
```

## Troubleshooting

### Server doesn't start

Check NVDA log (`NVDA+F1`):
- Look for "NVDA MCP Server started" message
- Check for error messages
- Verify port 8765 is not in use

### Authentication fails

- Verify you're sending `X-MCP-Token` header
- Check token matches `DEFAULT_TOKEN` in `mcp_server.py`
- Ensure no extra whitespace in token

### Connection refused

- Verify NVDA is running
- Check server is listening: `netstat -an | findstr 8765`
- Try `127.0.0.1` instead of `localhost`

### Port already in use

Change the port in [`__init__.py`](addon/globalPlugins/nvdaMcp/__init__.py#L31):
```python
self._mcp_server = mcp_server.NVDAMCPServer(
    host="127.0.0.1",
    port=9999,  # Change to your preferred port
    shutdown_event=self._shutdown_event
)
```

## API Endpoints

| Endpoint | Method | Auth Required | Description |
|----------|--------|---------------|-------------|
| `/health` | GET | No | Health check |
| `/tools` | GET | Yes | List available tools |
| `/call_tool` | POST | Yes | Execute a tool |

## License

GPL v2 - See [COPYING.txt](COPYING.txt)

## Contributing

Contributions welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## Credits

- **NVDA Development Team**: For the excellent screen reader
- **MCP Community**: For the Model Context Protocol specification
- **Contributors**: See GitHub contributors list

## Support

- **Issues**: https://github.com/nvda-addons/nvda-mcp/issues
- **Discussions**: https://github.com/nvda-addons/nvda-mcp/discussions
- **NVDA Add-ons List**: https://nvda-addons.groups.io/g/nvda-addons

## Changelog

### Version 1.0.0 (2026-02-19)

- Initial release
- MCP server running inside NVDA process
- Secure localhost-only binding with token authentication
- 7 MCP tools for focus, navigation, speech, and window information
- Clean start/stop lifecycle management
- No external dependencies

## Roadmap

- [ ] WebSocket support for real-time events
- [ ] Custom token configuration via NVDA settings
- [ ] Additional tools for text navigation
- [ ] Tools for braille display control
- [ ] Event streaming (focus changes, text changes)
- [ ] Official MCP SDK integration
- [ ] Multiple client support with session management
