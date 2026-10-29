# Changelog

## 0.1.0 — 2026-10-29

First release.

- Recorder hooks: incremental per-project session ids; every tool call logged as a commented bash step
  (Claude's Read/Grep/Glob/Write/Edit/WebFetch translated to shell; other tools as comments); failures
  keep their last error line; secret redaction; sensitive-file guard.
- `wf` CLI: list / show / search / mark / note / extract / run (all, single step, subsets) / ui.
- Workflows app (macOS): session viewer, cross-session search → create workflow, runner with live
  output, per-step status, env vars, script editor, run history.
- Workflows MCP server and a lazy-starting MCP router (`search_tools` + `call_tool`).
- Idempotent installer that appends hooks without touching existing ones; uninstaller.
