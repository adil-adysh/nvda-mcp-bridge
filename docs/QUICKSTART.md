# NVDA MCP Server - Quick Start Guide

## 1. Build the Add-on

```powershell
# Navigate to the project directory
cd d:\repos\nvda-addons\nvda-mcp

# Build using SCons
scons

# You should see: nvdaMcp-1.0.0.nvda-addon created
```

## 2. Install in NVDA

```powershell
# Double-click the generated file or run:
start nvdaMcp-1.0.0.nvda-addon
```

NVDA will prompt you to install. Click "Yes" and restart NVDA.

## 3. Verify Installation

After NVDA restarts:

1. Press `NVDA+F1` to open the log
2. Search for: `NVDA MCP Server started successfully`
3. You should see the message confirming the server is running

## 4. Test the Server

### Test Health Endpoint

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

### Test Tool Listing

```powershell
curl -H "X-MCP-Token: nvda-mcp-secret-token-change-me" http://127.0.0.1:8765/tools
```

### Test Speak Tool

```powershell
# PowerShell
$headers = @{
    "Content-Type" = "application/json"
    "X-MCP-Token" = "nvda-mcp-secret-token-change-me"
}

$body = @{
    name = "speak_text"
    arguments = @{
        text = "Hello from MCP!"
    }
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8765/call_tool" `
    -Method Post `
    -Headers $headers `
    -Body $body
```

You should hear NVDA speak "Hello from MCP!"

## 5. Python Client Example

Save this as `test_mcp.py`:

```python
import requests
import json

BASE_URL = "http://127.0.0.1:8765"
TOKEN = "nvda-mcp-secret-token-change-me"

def call_tool(name, arguments=None):
    response = requests.post(
        f"{BASE_URL}/call_tool",
        headers={
            "Content-Type": "application/json",
            "X-MCP-Token": TOKEN
        },
        json={
            "name": name,
            "arguments": arguments or {}
        }
    )
    return response.json()

# Test speak_text
print("Testing speak_text...")
result = call_tool("speak_text", {"text": "Testing MCP integration"})
print(json.dumps(result, indent=2))

# Test get_current_focus
print("\nTesting get_current_focus...")
result = call_tool("get_current_focus")
print(json.dumps(result, indent=2))

# Test get_window_title
print("\nTesting get_window_title...")
result = call_tool("get_window_title")
print(json.dumps(result, indent=2))
```

Run it:
```powershell
pip install requests
python test_mcp.py
```

## Troubleshooting

### "Connection refused"
- Make sure NVDA is running
- Check NVDA log for errors
- Verify the server started successfully

### "Unauthorized" response
- Check you're including the `X-MCP-Token` header
- Verify the token matches the one in `mcp_server.py`

### No speech output
- Verify NVDA speech is working normally
- Check NVDA log for errors
- Try the `show_message` tool instead

## Next Steps

- Read the full documentation: [`docs/MCP_SERVER_README.md`](MCP_SERVER_README.md)
- Explore all available tools
- Integrate with your AI assistant or automation tool
- Consider changing the default authentication token

## Security Note

⚠️ The default token is `nvda-mcp-secret-token-change-me`. This is intentionally obvious to remind you to change it for production use. Edit [`mcp_server.py`](../addon/globalPlugins/nvdaMcp/mcp_server.py) and rebuild the add-on.
