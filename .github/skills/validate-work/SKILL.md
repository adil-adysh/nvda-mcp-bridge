---
name: validate-work
description: "Use when validating changes in the NVDA AI Assistant repository, choosing the right checks for Python add-on code, Rust host code, Web UI code, or cross-boundary protocol updates. Keywords: validate, test, lint, typecheck, cargo check, vite build, protocol change."
---

# Validate Work

Use this skill to choose the smallest reliable validation for the slice that changed.

## Validation Matrix

### Python add-on

- `python -m ruff check .`
- add targeted Python runtime checks or Pyright validation when the change affects typing, imports, registries, or protocols

### Rust host

- `cargo check --manifest-path nvda_ui_host/Cargo.toml`

### Web UI

- `npm --prefix nvda_ui_host run build:webui`

### Cross-boundary protocol work

- validate at least the producer and consumer sides
- typical minimum: `cargo check --manifest-path nvda_ui_host/Cargo.toml` plus the matching Python or Web UI validation
- for command or event wiring changes, also run the focused Python protocol tests under `addon/globalPlugins/AI-assistant/ui/`

## Repository Notes

- Python formatting and linting are configured in `pyproject.toml`.
- The repo targets Windows and NVDA, so prefer Windows-friendly commands and paths in documentation or automation.
- The host build path has Windows-specific behavior in `scripts/build_host.py`; do not assume Unix shell tooling.
- The host UI uses protocol-backed presentation intent (`interaction_mode`, `controls_visible`, `attention_policy`, `focus_target`); validate both producer and renderer when those change.

## Output Format

- changed slice
- recommended validation command(s)
- what each command proves
- any gap that still requires manual verification
