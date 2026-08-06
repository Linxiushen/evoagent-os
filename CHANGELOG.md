# Changelog

All notable changes to HarnessLab are documented here.

## 0.2.0 - 2026-08-06

### Added

- Portable `harnesslab.trace/v1` artifacts.
- Stable protocol and content fingerprints.
- Structural comparisons for lifecycle, tool, policy, terminal, and invariant drift.
- `snapshot`, `verify`, and `compare` CLI commands with CI exit codes.
- Run artifact export and retained-run comparison API endpoints.
- Regression diff workbench with desktop and mobile layouts.
- Recursive credential redaction before event publication.
- Explicit `tool.denied` events for fail-closed side-effecting tools.

### Changed

- Raw model context is no longer serialized through run APIs.
- Project positioning now focuses on executable harness regression contracts.

## 0.1.0 - 2026-08-05

- Initial protocol-first runtime, trace console, conformance matrix, adapters, and MCP bridge.
