---
name: webview-localization
description: "Use when adding or changing translator-facing WebView UI labels, making UI strings translatable, debugging missing POT entries, or moving label ownership from JS or Rust to Python. Keywords: translation, gettext, POT, WebView labels, localized_strings, missing msgid, translator comment."
---

# WebView Localization

Use this skill when a host-backed UI string should be translator-owned and included in the add-on gettext workflow.

## Goals

- keep translator-facing WebView labels owned by Python
- ensure strings are extractable into the generated POT file
- avoid duplicate or conflicting label ownership in the browser bundle
- preserve the Python -> Rust host -> Web UI localization flow

## Ownership Rules

- Python is the source of truth for translator-facing WebView labels and status strings.
- Rust transports localized payload data but should not become the owner of UI copy.
- The Web UI should render `localized_strings` from Python and avoid maintaining a competing default label catalog for the same surface.
- Browser-side fallback text should be limited to defensive bootstrapping, not normal ownership of user-facing labels.

## Main Files

- `addon/globalPlugins/AI-assistant/ui/session_state.py`
- `buildVars.py`
- `site_scons/site_tools/gettexttool/__init__.py`
- `nvda_ui_host/webui/src/lib/state.svelte.js`
- `nvda_ui_host/webui/src/lib/defaults.js`
- `docs/ui-host-protocol.md`

## Workflow

1. Identify the user-facing label or status string and confirm whether it appears in Python, Rust, or Web UI code.
2. If the string is shown by the WebView, move ownership to Python metadata when practical, usually through `localized_strings` in session or result metadata.
3. Add or update the Python-side localized string entry in `ui/session_state.py` or the closest existing metadata producer.
4. Add a `# TRANSLATORS:` comment immediately above any extracted string whose purpose would not be obvious from the msgid alone.
5. Ensure the string is in a Python file included by `buildVars.i18nSources`.
6. Ensure the string uses a gettext extraction keyword recognized by the repo's `xgettext` configuration, currently including `translate(...)`.
7. Remove redundant browser-owned label defaults when Python already supplies the same label through `localized_strings`.
8. If the localization flow changes across the protocol boundary, update `docs/ui-host-protocol.md`.

## Decision Hints

- If the string is part of session controls, headings, toolbar labels, attachment labels, or status notices in the WebView, it should usually come from Python.
- If the string exists only as a visual-only, temporary browser fallback before any host payload arrives, keep it minimal and generic.
- If a string is missing from the POT file, first check extraction keyword support in `gettexttool/__init__.py`, then check `buildVars.i18nSources`, then verify the exact Python source call shape.
- Prefer extending the existing `localized_strings` map over inventing a second label source.

## Validation

- Run a focused `xgettext` command against the relevant Python files when validating extraction behavior.
- Run `npm --prefix nvda_ui_host run build:webui` after Web UI changes.
- Run `python -m ruff check .` for Python-side changes when the edit touches add-on code.
- If the protocol contract changed, validate the docs and the producing Python path together.

## Expected Output

- owning layer for the string
- files to edit
- extraction or ownership change needed
- validation steps to confirm the msgid is translator-visible
