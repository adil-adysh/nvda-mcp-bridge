---
applyTo: "nvda_ui_host/webui/src/**/*"
description: "Use when editing the Svelte WebView UI for the external NVDA host, including App.svelte, components, styling, and client-side protocol handling."
---

# Web UI Instructions

The Web UI renders host-backed intents. It should stay generic and protocol-driven.

## Ownership

- Render typed UI state coming from Python through the host protocol.
- Emit typed UI events for user actions that Python must interpret or persist.
- Keep purely visual state local when Python does not need to know about it.

## Implementation Rules

- Do not introduce provider-specific business logic or use-case branching in Svelte components.
- Favor small components and explicit props over hidden global state.
- Preserve keyboard accessibility and screen-reader friendly labeling because this UI is accessibility-critical.
- Keep styling intentional but lightweight; avoid browser-only behavior that bypasses the host protocol.
- If a UI change requires a new command or event, update the Rust and Python protocol slices in the same change.
- Prefer shared helpers or handler registries in `webui/src/lib/bridge.js` over repeated command-specific branches.
- Keep focus, scroll, and rendering behavior separate. Streaming updates must not move focus.
- Do not add user-facing label catalogs or fallback UI copy in the Web UI when Python already supplies `localized_strings` for the same surface.
- Web UI fallback text should be limited to transport-safe bootstrapping or defensive rendering, and should not become the primary owner of translator-facing labels.

## Validation

- Run `npm --prefix nvda_ui_host run build:webui` after Web UI changes.
- For protocol-affecting changes, validate Rust host compilation and the Python side that produces or consumes the same message.
