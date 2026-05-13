---
name: route-change
description: "Use when deciding where a requested change belongs in the NVDA AI Assistant, mapping a task to the correct layer, files, abstractions, and validation steps. Keywords: architecture, where should this go, correct layer, route feature, use case or service, protocol boundary."
---

# Route Change

Use this skill when a request is unclear about which layer should own the behavior.

## Goals

- map the request to the smallest correct layer
- identify the main files or directories to inspect first
- avoid bypassing `UseCaseEngine`, `ContextPipeline`, `LLMService`, `ProviderProxy`, or the host protocol
- propose the narrowest useful validation step

## Workflow

1. Classify the request as one of: NVDA plugin, use case, context, service, provider, Python UI adapter, Rust host, Web UI, or documentation.
2. Check `docs/architecture.md` when the change crosses Python, Rust, and Web UI boundaries.
3. Name the owning abstraction before editing. Examples: `UseCase`, presenter, collector, protocol model, host command handler, Svelte component.
4. If the request spans Python and Rust or Web UI, treat it as a protocol-backed change rather than a local patch.
5. For host-backed UI work, decide whether the change belongs in Python presentation intent, Rust activation/window behavior, or Web UI presentation-only state.
6. Recommend the cheapest validation that can falsify the implementation.

## Decision Hints

- Gesture, NVDA lifecycle, and background scheduling belong in `plugin/`.
- Feature intent and orchestration belong in `use_case/`.
- Structured data gathering belongs in `context/`.
- Streaming, chat sessions, providers, and tool execution belong in `service/`.
- Provider-specific adapters belong in `providers/`.
- Native windowing, WebView, and pipes belong in `nvda_ui_host/src/`.
- Browser rendering belongs in `nvda_ui_host/webui/src/`.
- New commands should usually start from Python intent, then flow through `host_protocol.py`, `protocol.rs`, and `bridge.js`.
- New UI-originated events should usually start from Web UI emission, then flow through `webview.rs`, the event pipe, and `host_renderer.py`.

## Expected Output

- owning layer
- likely files to inspect
- constraints to preserve
- validation command or check
