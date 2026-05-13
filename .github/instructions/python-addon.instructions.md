---
applyTo: "addon/globalPlugins/AI-assistant/**/*.py"
description: "Use when editing the NVDA add-on Python code, including plugin, use cases, context, services, providers, tools, and UI adapter layers."
---

# Python Add-on Instructions

Use the existing layered architecture before adding new abstractions.

## Routing

- `plugin/` handles NVDA gestures, lifecycle, and background scheduling only.
- `use_case/` orchestrates a feature and should stay free of provider-specific or NVDA API logic.
- `context/` collects structured context through collectors and extractors.
- `service/` owns chat coordination, tool execution, and provider-facing workflows.
- `providers/` contains provider-specific behavior behind shared protocols and proxy layers.
- `ui/` and `ui_host/` adapt results into UI intents and protocol messages.

## Implementation Rules

- Prefer extending an existing `UseCase`, presenter, context collector, or service before creating a new top-level concept.
- Register new use cases in `use_case/registry.py` and route them through `UseCaseEngine`.
- Keep prompt context typed and structured. Do not manually concatenate large prompt strings in arbitrary layers.
- Use the provider proxy and service layer rather than calling Gemini, Ollama, or OpenAI clients from feature code.
- Keep long-running work off the NVDA main thread and preserve graceful failure behavior.
- Follow the repository typing posture: strict type hints, explicit data shapes, and minimal dynamic behavior.
- For host-backed UI work, prefer `ui/intent.py` and presenter/view-model metadata over browser-layer heuristics.
- Keep `ui/adapter.py` focused on coordination. Extract stream projection or payload shaping into helpers when it starts owning too many details.
- Translator-facing WebView labels and status strings should originate in Python metadata rather than being invented in the Web UI.
- Strings that must appear in the generated POT file should live in Python source scanned by `i18nSources` and use a gettext extraction keyword recognized by the repo's `xgettext` configuration, such as `translate(...)`.
- Add `# TRANSLATORS:` comments immediately above extracted msgids when the UI meaning would not be obvious from the text alone.

## Validation

- Start with `python -m ruff check .` for Python edits.
- Use targeted runtime checks or Pyright validation when the change affects types, protocols, or import wiring.
- When editing UI host adapters or protocol models in Python, validate the corresponding Rust or Web UI side too.
