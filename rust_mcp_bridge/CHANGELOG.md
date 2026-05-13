# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial release of NVDA MCP Server
- Protocol metadata (`request_id`, `action`, `duration_ms`, `bridge`, `nvda_status_snapshot`) in MCP tool response content for AI agent observability
- `POST /execute` endpoint alias on Python add-on side for Rust client compatibility
- `constraints` field in tool discovery payload (`dangerous`, `requires_explicit_opt_in`, `blocked_on_secure_screen`, `blocked_on_modal`, `blocked_on_menu`)
- NVDA state constraint fields in Rust `BridgeToolConstraints` for protocol alignment

### Fixed
- NVDA freezing issue with single-worker-thread Tokio runtime
- Missing request_id field in bridge protocol actions
- Bridge output validation for envelope-shaped schemas
- Rust `error.code` parsing now accepts both integer (protocol v1 spec) and string (legacy)
- `error.code` type in output type schemas changed from `string` to `integer` to match protocol spec
- `TextLineInfo` output schema now includes required `status` field matching the Python dataclass
- Rust output validation now validates inner `data` schema instead of full HTTP envelope, avoiding false rejections from transport metadata fields

### Changed
- Implemented single-worker-thread Tokio runtime to prevent Windows scheduler starvation
- `BridgeActionEnvelope` expanded to carry full protocol metadata (`request_id`, `action`, `duration_ms`, `bridge`, `nvda_status_snapshot`) instead of just `success`/`data`/`error`
- `BridgeError` uses flexible `ErrorCode` type accepting both integer and string codes
- `BridgeToolConstraints` extended with `blocked_on_secure_screen`, `blocked_on_modal`, `blocked_on_menu`

## [1.0.0] - 2026-02-21

### Added
- Initial public release
- Bridge-backed tool discovery from nvda-mcp-bridge
- JSON-RPC 2.0 stdio transport
- MCP protocol 2024-11-05 support
- 18 NVDA automation tools
- Atomic request ID generation for bridge actions
- Envelope-aware output validation with legacy fallback
- Single-worker-thread Tokio runtime configuration
- Startup logging banner confirming NVDA-safe configuration

### Fixed
- NVDA freezing issue with single-worker-thread Tokio runtime
- Missing request_id field in bridge protocol actions
- Bridge output validation for envelope-shaped schemas

### Changed
- Implemented single-worker-thread Tokio runtime to prevent Windows scheduler starvation

[Unreleased]: https://github.com/adilhodge/nvda-mcp-server/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/adilhodge/nvda-mcp-server/releases/tag/v1.0.0
