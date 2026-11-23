# Deploying the bridge and companion

This page records the version contract and the checks that prove a deployment
is consistent. It exists because an installed API-4 companion next to an API-1
bridge fails closed with no tools, which looks like a broken client.

## Version line

| Artifact | Current | Notes |
|---|---|---|
| Add-on | `0.1.0` | first public preview; `api_level` 1 |
| Companion | `0.1.0` | speaks API level 1 only |
| Protocol | `1.0.0` | `nvda-bridge-protocol` |

`0.1.0` is the first public release. The `0.10.0`–`0.12.0` entries were
internal development builds. Package version is independent of protocol
version `1.0.0` and API level `1`.

## Deployment checklist

1. Build the package with `scons` (or `scons companion=0` when only Python
   changed).
2. Install `nvdaMcpBridge-<version>.nvda-addon` with NVDA's Add-on Manager.
3. Install or repair the companion from **NVDA Menu → Tools → NVDA MCP Bridge
   → Install or Repair MCP Companion**. On success, this refreshes the bridge
   token and stops companion processes started from that install folder. Restart
   MCP clients so their companions read the new token. A companion installed in
   another folder is left running and must also be restarted before it can
   authenticate again. If `NVDA_BRIDGE_TOKEN` is explicitly set, rotation is
   unavailable and install/repair stops before changing the companion files.
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
report the same tool count the bridge's `/tools` endpoint returns (33 by
default, 34 when developer mode is enabled).

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

The companion omits `outputSchema` by default. Measured on the 33-tool API-1
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

## Built artifacts

Sizes and hashes change with every build, so the release publishes a
`SHA256SUMS.txt` beside the `.nvda-addon`. Verify a download against that file
instead of a hash copied into documentation:

```powershell
Get-ChildItem .\nvdaMcpBridge-*.nvda-addon | Get-FileHash -Algorithm SHA256
```

## Rollback

A replaced add-on tree and companion are copied to
`D:\nvda-addons\backups\` before deployment. Restore the tree into
`%APPDATA%\nvda\addons\nvdaMcpBridge` and the companion into
`%LOCALAPPDATA%\Programs\NVDA MCP Bridge\bin`, then restart NVDA. A backup of
the stale API-4 line is named with a `STALE-1.0.0-API4` prefix so it cannot be
mistaken for the current build.
