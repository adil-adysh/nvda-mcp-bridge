---
applyTo: "docs/**/*.md"
description: "Use when editing repository documentation, including architecture, protocol, runtime, release notes, and user-facing setup guidance."
---

# Documentation Instructions

Documentation should reflect the real architecture and supported workflows in this repository.

## Writing Rules

- Prefer concrete ownership statements over vague architecture summaries.
- Keep the Python add-on, Rust host, and Web UI responsibilities distinct.
- Document commands exactly as they are used on Windows when the repo is Windows-specific.
- When describing provider behavior or feature flow, align with existing abstractions such as `UseCaseEngine`, `ContextPipeline`, `LLMService`, and `ProviderProxy`.
- Update related protocol or runtime docs when a code change alters IPC messages, host lifecycle, or build steps.

## Good Outcomes

- A contributor can tell which layer should change.
- A maintainer can tell how to validate the change.
- A user can follow setup steps without guessing hidden prerequisites.
