# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial release of NVDA MCP Server

### Fixed
- NVDA freezing issue with single-worker-thread Tokio runtime
- Missing request_id field in bridge protocol actions
- Bridge output validation for envelope-shaped schemas

### Changed
- Implemented single-worker-thread Tokio runtime to prevent Windows scheduler starvation

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
