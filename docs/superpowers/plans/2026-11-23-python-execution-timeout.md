# Python Execution Timeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow `execute_python` calls up to 15 minutes, with a persistent NVDA-configured ceiling, and allow Developer Mode elevation presets through one day.

**Architecture:** Add an NVDA setting for the maximum Python call wait, defaulting to five minutes with presets through 15 minutes. Expose a per-call `timeout_ms` (default one minute), validate it against the saved ceiling, and propagate it through the Rust HTTP client and NVDA action executor. Expand Developer Mode elevation presets to one day and persist the absolute expiry of locally approved grants so restart never renews the grant. A timed-out Python script that has started continues running.

**Tech Stack:** Python, wxPython settings, Rust/reqwest, unittest/pytest and Cargo tests.

**Spec:** User-approved timeout design in conversation.

## Tasks

- [x] Add failing tests for persisted max duration, per-call validation, and Rust request deadline propagation.
- [x] Implement NVDA config helpers, settings control, Python tool schema and handler validation.
- [x] Propagate the timeout through `/action` and the Rust HTTP request timeout.
- [x] Update tool inventory, tool contract, README and changelog.
- [x] Run Python, Rust and packaging checks; exercise the live MCP path when Developer Mode is already authorized.
