# GitHub Copilot Instructions — NVDA AI Assistant

This repository is a layered NVDA add-on plus an external Rust UI host.

Copilot must generate changes that:
- respect the Python add-on, Rust host, and WebView boundary
- route feature work through the existing abstractions instead of bypassing them
- keep NVDA responsive by avoiding blocking work on the main thread

## System Model

Primary application flow:

NVDA → GlobalPlugin → AIAssistantApplication → UseCaseEngine  
→ ContextPipeline → LLMService → ProviderProxy → Provider  
→ Result → Presenter → NVDA UI

Host-backed UI flow:

Python add-on → UI intent / host protocol → named-pipe IPC → Rust host  
→ WebView / Svelte UI → typed UI event → Python add-on

Never bypass those flows with direct provider calls, ad-hoc prompt assembly, or host-specific business logic in the wrong layer.

## Layer Rules

### Python add-on

- `plugin/` owns NVDA entrypoints, gestures, lifecycle, and background task scheduling.
- `use_case/` owns feature orchestration and must go through `UseCaseEngine`.
- `context/` owns structured context collection and must produce typed context instead of raw prompt strings.
- `service/` owns model interaction, streaming, tool execution, and chat coordination.
- `providers/` owns provider-specific implementation details behind `ProviderProxy`.
- `ui/` and `ui_host/` own rendering contracts and adapters, not business logic.
- `ui/intent.py` is the source of truth for host presentation intent such as `interaction_mode`, `attention_policy`, and `focus_target`.
- `plugin/presenter.py` decides result-mode behavior such as `result_action_only` and generic follow-up actions like `Open Chat`.
- `ui/adapter.py` should remain a coordinator; detailed stream projection or payload shaping should move into helpers when it grows.

### Rust host

- `nvda_ui_host/src/protocol.rs` is the protocol source of truth.
- `ipc.rs` owns transport only.
- `app.rs` validates incoming commands and normalizes them for the UI thread.
- `window.rs` and `webview.rs` own native window and WebView lifecycle.
- `app.rs` owns command-to-activation-policy mapping.
- `window.rs` owns foreground, focus, hide, and close behavior.

### Web UI

- `nvda_ui_host/webui/src/` renders generic host intents.
- Keep provider logic, use-case branching, and NVDA behavior out of the browser layer.
- Prefer typed protocol-driven UI state over hidden browser-only behavior.
- `webui/src/lib/bridge.js` should use shared helpers or handler registries rather than repeated payload branches.
- `webui/src/lib/state.svelte.js` owns local presentation state only.

## Required Patterns

- New features should usually be implemented as a `UseCase` and registered in `use_case/registry.py`.
- Use `ContextPipeline` and the existing collector/extractor structure instead of hand-building prompt context in a feature.
- Access providers through `LLMService` and `ProviderProxy`, never directly from `use_case/` or `ui/`.
- Register tool behavior in the tool registry/executor path instead of invoking tools directly from a feature.
- Keep host commands generic and protocol-backed. If Python and Rust both change, update the protocol contract deliberately.
- Express host-backed UI behavior through protocol-backed presentation metadata such as `interaction_mode`, `controls_visible`, `attention_policy`, and `focus_target`.
- Streaming updates must not change focus. Final answers may request `foreground_if_background` when Python wants to surface the completed answer.
- One-shot results such as image description, summary, and structured summary should be modeled as `result_action_only` views with generic result actions.

## Adding Commands And Events

When adding a host command:

1. document it in `docs/ui-host-protocol.md`
2. update the Python producer such as `presenter.py`, `view_models.py`, or `adapter.py`
3. update Rust parsing and dispatch in `nvda_ui_host/src/protocol.rs` and `nvda_ui_host/src/app.rs`
4. update Web UI application in `nvda_ui_host/webui/src/lib/bridge.js`
5. validate both the producer and consumer sides

When adding a UI-originated event:

1. document it in `docs/ui-host-protocol.md`
2. emit it from the Web UI
3. keep `webview.rs` transport-focused; it should forward rather than interpret feature semantics
4. dispatch it in `ui/host_renderer.py` and route it to the owning Python layer
5. add targeted tests where useful

## Hard Constraints

- Do not call providers from `use_case/` or UI code.
- Do not access NVDA APIs outside the plugin/context layers unless an existing pattern already permits it.
- Do not block the NVDA main thread.
- Do not hardcode provider names or provider-specific branching outside provider/config layers.
- Do not mix UI rendering concerns with business logic.
- Do not introduce parallel abstractions when an existing `UseCase`, `ContextPipeline`, presenter, or host protocol type already fits.
- Do not implement new result behavior by branching on use-case id in the Web UI when presentation intent metadata can express it.

## Validation Defaults

Match validation to the slice you changed:

- Python add-on changes: `python -m ruff check .` and targeted type or runtime checks when available.
- Rust host changes: `cargo check --manifest-path nvda_ui_host/Cargo.toml`.
- Web UI changes: `npm --prefix nvda_ui_host run build:webui`.
- Cross-boundary protocol changes: validate both Python and Rust/Web UI sides.

## Supporting Docs

- `docs/architecture.md` describes the Python/Rust/WebView split and IPC ownership.
- `docs/ui-host-runtime.md` covers host supervision and pipe lifecycle.
- `docs/ui-host-protocol.md` covers protocol expectations.
- Additional file-scoped guidance lives in `.github/instructions/`.
- Task-specific workflows live in `.github/skills/` and `.github/agents/`.

---

## 19. Project Constraints

- NVDA add-on (accessibility-critical)
- Must remain responsive
- Supports:
  - page summarization
  - image description
  - contextual chat

Providers:
- Ollama (preferred, local-first)
- Gemini (optional fallback)

---

## 20. If Uncertain

- Do NOT guess architecture
- Ask for clarification
- Or follow existing similar implementation
