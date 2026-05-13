---
applyTo: "nvda_ui_host/src/**/*.rs"
description: "Use when editing the Rust UI host, including protocol parsing, IPC transport, window lifecycle, and WebView command handling."
---

# Rust Host Instructions

The Rust binary is a host and renderer boundary, not a business-logic layer.

## Ownership

- `protocol.rs` defines typed message structures and serialization behavior.
- `ipc.rs` owns named-pipe transport and connection mechanics only.
- `app.rs` validates commands and emits `ack` or `error` results.
- `window.rs` and `webview.rs` own UI-thread dispatch, native window lifecycle, and browser event forwarding.

## Implementation Rules

- Keep provider decisions, prompt logic, and NVDA-specific behavior out of Rust.
- Prefer explicit protocol types over stringly-typed ad-hoc payload handling.
- Treat command responses and asynchronous UI events as distinct flows.
- Preserve UI-thread affinity and host lifecycle guarantees.
- When adding a new command or event, update the protocol deliberately and keep semantics generic enough for reuse.
- Keep activation behavior policy-driven in `app.rs` and native close/hide behavior in `window.rs`.

## Validation

- Run `cargo check --manifest-path nvda_ui_host/Cargo.toml` after Rust changes.
- If the change affects messages consumed by Python or the Web UI, validate those slices too.
