---
name: workflow-recorder
description: Records every Claude Code session under an incremental session id (#0001, #0002, …) and logs each Bash command used to find, debug, build, test or run things — with its description as a comment — so commands can later be grouped into isolated, re-runnable workflows. Use when the user asks to see past session commands, "save/turn this into a workflow", group or replay commands, mark a sub-task, or run a saved workflow; also follow its logging conventions in every session.
---

# Workflow recorder

Recording is automatic: hooks in `~/.claude/settings.json` call `wf.py hook` on
`SessionStart`, `PostToolUse` and `PostToolUseFailure` (all tools). Each new Claude
session gets the next number for its project; resumed sessions keep their number.
The SessionStart hook tells you the current number (e.g. `Session #0007`).

Store: `~/workflows/<project>/` (outside the repo, secrets redacted)
- `sessions/0007.sh` — commented log: step, time, category, status, description, `cd`, command
- `sessions/0007.jsonl` — same data, machine-readable (source of truth)
- `workflows/<name>.sh` — extracted runnable workflows
- `runs/<name>-<ts>.log` — output of `wf run`

CLI: `wf` (symlink to `~/.claude/skills/workflow-recorder/wf.py`). `wf` calls are not logged.

### Claude's own tools are logged as their bash equivalent

| Tool | Logged as |
|---|---|
| Read | `cat` / `sed -n 'a,bp'` / `head -n` |
| Grep | `grep -rE` with `-l`/`-c`/`-n`, `-i`, `-A/-B/-C`, `--include` |
| Glob | `find <path> -type f -name/-path …` |
| Write | `cat > file <<'WF_EOF'` (≤80 lines, ends in newline), else exact snapshot in `sessions/NNNN.files/` + `cp` |
| Edit / MultiEdit | `python3` exact-string replace (fails like Edit if the old text is missing) |
| WebFetch | `curl -sL <url>` |
| Agent, WebSearch, Skill, MCP and other tools | `# [Tool] …` comment lines (category `note`) — no shell equivalent |

Skipped as noise: ToolSearch, TodoWrite, AskUserQuestion, plan-mode and other UI tools.
Writes/edits to sensitive paths (`.env*`, `*secret*`, `*credential*`, `*.pem`, `*.key`) are logged
as comments only. Other Write/Edit content is stored verbatim (not redacted) so replays are exact.

## Conventions while working (every session)

1. **The Bash `description` becomes the comment** in the log. Make it say *why*, specific
   enough to reuse later: "Run SpokenText unit tests after splitter change", not "Run tests".
2. **Mark sub-tasks.** When you begin a distinct goal (reproduce bug X, build & install APK,
   check server logs), run `wf mark -s <N> "<goal>"`. Steps after it belong to that task,
   which makes `--from N@<task>` extraction clean.
3. **Record findings** worth keeping with `wf note -s <N> "root cause: ..."`.
4. Prefer one logical action per Bash call over long `&&` chains so steps can be picked individually.
5. Use env vars for secrets (`"$API_KEY"`) rather than literals — literals are redacted to `<REDACTED>`.

## Commands

```bash
wf status                         # store path, session count, workflows
wf list                           # sessions with per-category counts and task titles
wf show [N|last] [--cat debug] [--task 2]
wf search 'gradlew|adb'           # regex over all sessions' commands + descriptions
wf mark -s N "goal"               # start task section
wf note -s N "finding"

# Group commands into a runnable workflow (dedupes; skips failed steps unless --include-failed)
wf extract <name> --from 7:3-9,12 [8@2 ...] [--cat build,run] [--exclude-cat find] [--grep re] -d "what it does"
wf extract test-all  --from all --cat test -d "Every test command ever used"
wf extract debug-sip --from 7@2 -d "Reproduce SIP call drop + collect logcat"

wf workflows                      # list saved workflows
wf run <name> [--dry-run] [-e K=V] [--inherit-env]
```

Categories (auto, by command shape): `find`, `debug`, `test`, `build`, `run`, `git`, `edit`, `note`, `other`.
Use `--exclude-cat find,note` for a lean runnable workflow; keep `note` to retain the narrative.

`wf run` executes in an isolated `bash --noprofile --norc` with `set -euo pipefail` and a minimal
environment (HOME, PATH, USER, TERM, LANG, JAVA_HOME, ANDROID_HOME/SDK_ROOT). Pass extra vars with
`-e`, or `--inherit-env` for the full environment. It refuses to run while `<REDACTED>` remains.

## When the user asks to save / group / replay

1. `wf show <N>` (or `wf search`) to find the relevant steps.
2. Pick the minimal set that reproduces the result: drop exploratory `find` steps and failed attempts.
3. `wf extract` with a kebab-case name and a one-line `-d`.
4. Open the generated file, tighten comments if needed, replace any `<REDACTED>` with `"$VAR"`.
5. `wf run <name> --dry-run`, then offer to `wf run <name>`.
6. If the user wants it in the repo, copy it (e.g. `scripts/workflows/<name>.sh`) — don't move it.

## MCP access (for Claude and other tools)

- `wf_mcp.py` — stdio MCP server over `~/workflows`: `list_projects`, `list_sessions`, `search_steps`,
  `get_steps`, `list_workflows`, `get_workflow`, `extract_workflow`. Returns only matching steps.
  Any client can register it directly: `/usr/bin/python3 ~/.claude/skills/workflow-recorder/wf_mcp.py`.
- In Claude Code it sits behind the **router** (`~/mcp-router/`), which exposes only `search_tools`,
  `call_tool`, `list_servers`, `refresh_index`. To use past commands: `search_tools("past gradle test commands")`
  → `call_tool("workflows__search_steps", {"query": "gradle test"})`.

## Workflows app (viewer + runner)

`~/Applications/Workflows.app` (or `wf ui`) starts `app/server.py` on http://127.0.0.1:47821 and opens it
as a Chrome app window. The server stays running in the background; stop it with `pkill -f workflow-recorder/app/server.py`.
- **Sessions**: browse every session's steps, filter by category / hide failed, copy commands.
- **Search**: across all sessions; tick steps from several sessions → **Create workflow**.
- **Workflows**: **Run all**, **Run selected**, or **▶ Run** a single step. Each step runs in its recorded
  directory, so any subset works on its own. Live output, per-step OK / FAILED / SKIPPED, Stop button,
  extra env vars, in-place script editor.
- **Runs**: every run log (app and CLI), with exit status.
CLI equivalent: `wf run <name> [--steps 3 | 2-5,8] [--dry-run]`.
Security: binds 127.0.0.1 only, checks Host header, POSTs need a per-launch token embedded in the page.
