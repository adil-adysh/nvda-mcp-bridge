# Deploying the bridge and companion

This page records the version contract and the checks that prove a deployment
is consistent. The companion fails closed when its advertised API level is not
supported by the bridge, instead of connecting with an incompatible catalog.

## Version line

| Artifact | Current | Notes |
|---|---|---|
| Add-on | `0.1.0` | first public preview |
| Companion | `0.1.0` | speaks API level 1 only |
| Protocol | `1.0.0` | `nvda-bridge-protocol` |

Version `0.1.0` is the first public GitHub release. Earlier `0.10.0`–`0.12.0`
numbers were internal development builds, not public releases. The package
version is independent of protocol version `1.0.0` and API level `1`.

## Deployment checklist

1. Build the package with `scons` (or `scons companion=0` when only Python
   changed).
2. Install `nvdaMcpBridge-<version>.nvda-addon` with NVDA's Add-on Manager.
3. Install or repair the companion from **NVDA Menu → Tools → NVDA MCP Bridge
   → Install or Repair MCP Companion**. This stops every companion process
   started from that install folder; their MCP clients must reconnect. A
   companion installed in another folder is left running.
4. Restart NVDA.
5. Verify the bridge and the companion independently:

```powershell
$token = (Get-Content "$env:APPDATA\nvda\nvda-mcp-bridge\token.key").Trim()
Invoke-RestMethod http://127.0.0.1:8765/health |
  Select-Object bridge_version, api_level, supported_api_levels
python scripts/mcp_tool_catalog_probe.py
```

The health response must report `bridge_version` equal to the package version
and `api_level: 1` with `supported_api_levels: [1]`. The catalog probe must
report the same tool count the bridge's `/tools` endpoint returns (34 for
`0.1.0` without developer mode).

## Companion environment

| Variable | Default | Effect |
|---|---|---|
| `NVDA_BRIDGE_URL` | `http://127.0.0.1:8765` | Bridge address |
| `NVDA_BRIDGE_TOKEN` | `%APPDATA%\nvda\nvda-mcp-bridge\token.key` | Override the discovered token |
| `NVDA_BRIDGE_TIMEOUT_SECS` | `15` | HTTP timeout |
| `NVDA_BRIDGE_CATEGORIES` | all | Comma-separated category allow-list; the only lever that removes tools from the catalog |
| `NVDA_BRIDGE_OUTPUT_SCHEMAS` | `0` | Set to `1` to advertise each tool's `outputSchema`; omitted schemas are still returned by `get_tool_schema` |
| `NVDA_BRIDGE_TOOLS_PAGE_SIZE` | `100` | Maximum tools per `tools/list` page; `1`–`1000` |

## Catalog size

The companion omits `outputSchema` by default. Measured on the 34-tool API-1
catalog: `tools/list` fell from **77,046 B** with output schemas to **36,344 B**
without them (53% smaller); `structuredContent` is still validated against the
same schema. A client that needs the schema can
call `get_tool_schema(name, include_output_schema=true)`, and a deployment that
prefers the old payload can set `NVDA_BRIDGE_OUTPUT_SCHEMAS=1`.

`NVDA_BRIDGE_CATEGORIES` remains the only way to reduce the number of tools
themselves. `find_tools` is an index over the advertised catalog, not a smaller
catalog.

## Restarting NVDA

`restart_nvda` returns as soon as NVDA begins restarting. If it is called from
inside a modal dialog the relaunch can fail and leave no NVDA process; start
`C:\Program Files\NVDA\nvda.exe` once, then re-check `/health`. Close settings
dialogs before restarting when a hands-off restart matters.

## Release artifacts

The GitHub release publishes the NVDA add-on, bundled MCP companion executable,
localization template, and SHA256SUMS.txt. Verify downloaded files against
that checksum manifest before installing.

## Rollback

Before replacing an installation, preserve the installed add-on directory and
companion binary. To roll back, restore both from that backup and restart NVDA.
