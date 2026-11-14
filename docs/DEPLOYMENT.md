# Deploying the bridge and companion

This page records the version contract and the checks that prove a deployment
is consistent. It exists because an installed API-4 companion next to an API-1
bridge fails closed with no tools, which looks like a broken client.

## Version line

| Artifact | Current | Notes |
|---|---|---|
| Add-on | `0.11.0` | `api_level` 1 |
| Companion | `0.11.0` | speaks API level 1 only |
| Protocol | `1.0.0` | `nvda-bridge-protocol` |

`0.10.0` was the first public API-1 contract. A `1.0.0` package advertising API
level 4 predates the reset and is **not** an upgrade: do not install it over
this line, and do not treat its backup as the current build. Keep at most one
`1.0.0` archive, clearly labelled as stale, until the rollback window closes.

## Deployment checklist

1. Build the package with `scons` (or `scons companion=0` when only Python
   changed).
2. Install `nvdaMcpBridge-<version>.nvda-addon` with NVDA's Add-on Manager.
3. Install or repair the companion from **NVDA Menu → Tools → NVDA MCP Bridge
   → Install or Repair MCP Companion**.
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
report the same tool count the bridge's `/tools` endpoint returns (33 for
`0.11.0` without developer mode).

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

| Artifact | Size | SHA-256 |
|---|---:|---|
| `nvdaMcpBridge-0.11.0.nvda-addon` | 4,623,722 B | `A6B924382E39FA926986CC9BC75E91B717C613AF9171122BE53F2580F2571960` |
| `AIAssistant-0.14.1.nvda-addon` | 24,899,770 B | `7116F9FA0DA4A162FBA8A22A677FAF1A7069740495DD20D92CC0479422D21E79` |

## Rollback

A replaced add-on tree and companion are copied to
`D:\nvda-addons\backups\` before deployment. Restore the tree into
`%APPDATA%\nvda\addons\nvdaMcpBridge` and the companion into
`%LOCALAPPDATA%\Programs\NVDA MCP Bridge\bin`, then restart NVDA. A backup of
the stale API-4 line is named with a `STALE-1.0.0-API4` prefix so it cannot be
mistaken for the current build.
