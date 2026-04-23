# Conventional Commits Guide

This project uses [Conventional Commits](https://www.conventionalcommits.org/) for commit messages. This allows automated changelog generation via git-cliff.

## Commit Message Format

```
<type>(<scope>): <subject>

<body>

<footer>
```

## Types

- **feat**: A new feature
- **fix**: A bug fix
- **docs**: Documentation only changes
- **style**: Changes that don't affect code meaning (formatting, semicolons, etc)
- **refactor**: Code changes that neither fix bugs nor add features
- **perf**: Code changes that improve performance
- **test**: Adding or updating tests
- **chore**: Changes to build, dependencies, or tooling
- **ci**: Changes to CI/CD configuration

## Scope (Optional)

The scope specifies which part of the codebase is affected:
- `bridge` - Bridge protocol client
- `tools` - Tool discovery or execution
- `runtime` - Tokio runtime configuration
- `protocol` - MCP protocol handling
- `validation` - Input/output validation

## Examples

### Simple feature
```
feat(tools): add get_clipboard_text tool
```

### Bug fix with scope
```
fix(bridge): handle missing request_id field in action payload
```

### Breaking change
```
feat(runtime): switch to single-worker Tokio
BREAKING CHANGE: Multi-threaded runtime no longer supported on Windows
```

### Detailed commit
```
feat(validation): add envelope-aware output validation

Modify validate_output() to try envelope schema validation first
with fallback to legacy data-only validation for backward compatibility.

Closes #42
```

## Changelog Generation

Commits are automatically processed:
- **feat** → Minor version bump (e.g., 1.0.0 → 1.1.0)
- **fix** → Patch version bump (e.g., 1.0.0 → 1.0.1)
- **BREAKING CHANGE** → Major version bump (e.g., 1.0.0 → 2.0.0)

## Commit Message Best Practices

1. **Use imperative mood**: "add feature" not "added feature" or "adds feature"
2. **Don't capitalize the subject**: "fix bug" not "Fix bug"
3. **Don't use period at end of subject**: "add feature" not "add feature."
4. **Limit subject to 50 characters**
5. **Wrap body at 72 characters**
6. **Use body to explain WHAT and WHY, not HOW**
7. **Reference issues/PRs**: "Closes #123" or "Fixes #456"

## Examples of Good Commits

```
feat(bridge): inject request_id into action payloads

Generate unique request IDs using atomic counter and timestamp
to prevent protocol errors when bridge requires identification.
Implements format: mcp-{timestamp}-{sequence}

Closes #1
```

```
fix(protocol): validate envelope-shaped output schemas

Handle bridge responses with success/data/error envelope wrapper.
Falls back to legacy data-only validation for compatibility.

Fixes #2
```

```
refactor(runtime): use single-worker Tokio to prevent NVDA freeze

Changes Tokio runtime to single-worker-thread flavor to prevent
Windows scheduler starvation when running alongside NVDA.

Improves: #3
```

## Tools

- `git-cliff`: Auto-generates CHANGELOG from commits
- `cargo-release`: Automates release process

To generate changelog locally:
```bash
git-cliff --tag v1.0.0
```

To update CHANGELOG.md with latest unreleased changes:
```bash
git-cliff --unreleased >> CHANGELOG.md
```
