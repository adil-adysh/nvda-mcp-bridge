import json
import sys
from pathlib import Path

root = Path(r"d:\nvda-addons\nvda-mcp")
schema_path = root / "schema.json"

def fail(msg):
    print("ERROR:", msg)
    sys.exit(2)

if not schema_path.exists():
    fail(f"schema.json not found at {schema_path}")

with open(schema_path, 'r', encoding='utf-8') as f:
    payload = json.load(f)

# Basic checks matching BridgeClient::validate_tools_payload
if payload.get('protocol') != 'nvda-bridge-protocol':
    fail(f"protocol mismatch: {payload.get('protocol')}")

if payload.get('api_level') != 1:
    fail(f"api_level mismatch: {payload.get('api_level')}")

if not payload.get('protocol_version'):
    fail('protocol_version empty')

if not payload.get('bridge_version'):
    fail('bridge_version empty')

tools = payload.get('tools')
if not isinstance(tools, list):
    fail('tools is not a list')

count = payload.get('count')
if count != len(tools):
    fail(f"count mismatch: declared {count}, actual {len(tools)}")

seen = set()
for t in tools:
    name = t.get('name','').strip()
    if not name:
        fail('Tool with empty name')
    if name in seen:
        fail(f'Duplicate tool name: {name}')
    seen.add(name)
    if not isinstance(t.get('inputSchema'), dict):
        fail(f"Tool {name} inputSchema is not an object")
    out = t.get('outputType')
    if out and out not in payload.get('types', {}):
        print(f"WARNING: tool {name} outputType {out} not found in types map")

print('OK: schema.json passes validation for Rust Bridge expectations')
sys.exit(0)
