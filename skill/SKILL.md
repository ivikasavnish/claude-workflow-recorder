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
- `sessions/0007.jsonl` — same data, machine-readable (source of truth), plus each tool call's full `params` (redacted)
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
wf run <name> [--dry-run] [-e K=V] [--inherit-env] [-P NAME=value] [--allow-dangerous]
wf inspect <name>                 # parameters, dangerous and non-repeatable steps
wf sync [--init --private OWNER/REPO]  # commit ~/workflows, push to a private repo on this machine's branch
wf machine                        # this machine's branch: machine/<host>-<id>
wf machines                       # other machines pulled by sync (read-only mirrors)
wf --machine <name|all> search|list|show ...   # read another machine's sessions
wf sync --schedule MIN | --unschedule          # sync on a timer with no session open
```

Once `wf sync --init` has run, session start and end (and tool calls, at most every `WF_SYNC_INTERVAL` s) sync in the background. Never point it at a
public repo (it refuses), and never use `--force` on a machine branch.

Risk flags: `danger` steps are refused by `wf run` unless `--allow-dangerous` is passed; `once` steps only warn.
Never pass `--allow-dangerous` on your own: show the user the flagged steps and get their approval first.

Parameters: `wf extract ... --param VERSION=1.2.3 [--auto-params] [--safe-only]` replaces the literal with
`{{VERSION}}` (default = recorded value). Pass secrets with `-e`, not `--param`.

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
5. Run `wf inspect <name>`; parameterize values that will change (versions, hosts, paths) as `wf extract` suggests.
6. `wf run <name> --dry-run`, then offer to `wf run <name>`.
7. If the user wants it in the repo, copy it (e.g. `scripts/workflows/<name>.sh`) — don't move it.

## Fix memory and local agents (`wf fix`)

The recorder learns **failure → fix** pairs: when a step fails and the same command passes a few steps later,
the steps in between (edits, installs, config changes — not reads or test reruns) are saved as a fix in
`~/workflows/<project>/fixes.jsonl`. If only the command changed, the new command is saved. Each fix
tracks wins/losses, and fixes that keep failing are retired.

- **In a session**: when a command fails with an error that matches a known fix, the hook adds it to your
  context ("This error matches fixes that worked before…"). Check it applies, then use it. Once a fix of
  your own works, the hook learns it automatically.
- **`wf fix`** makes a failing command pass. It tries remembered fixes first (applied, then verified by
  rerunning), then a local coding agent over **ACP** (Agent Client Protocol), which gets the best related
  past fixes from every project in its prompt. Only a change that makes the command pass is learned (as a patch).
- **Agents** (`wf agents`): `claude` (claude-agent-acp), `codex` (codex-acp), `antigravity` (`agy acp`),
  `gemini` (`--experimental-acp`) over ACP; `muse` (MSP, not ACP) through a one-shot `muse exec`. Add or override
  agents in `~/workflows/agents.json`.

```bash
wf fix --last                      # rerun the session's last failed step and fix it
wf fix --cmd "npm test" [--agent codex|antigravity|none] [--yes] [--attempts 2]
wf run release --fix [--agent ...] # on a failing step: fix, then continue
wf fixes [--all] | wf fixes show <id> | wf fixes forget <id>
wf learn [SEL...]                  # (re)learn from recorded sessions (default all; idempotent)
wf agents
```

Remembered fixes and agent tool calls are confirmed on the terminal unless `--yes` is passed. Even with `--yes`,
commands flagged dangerous are refused. Agent writes go through `wf`, which limits them to the project and
blocks sensitive files. Never pass `--yes` on the user's behalf for an agent run without asking.
Turn off live learning and hints with `WF_FIX_MEMORY=0`.

## MCP access (for Claude and other tools)

- `wf_mcp.py` — stdio MCP server over `~/workflows`: `list_projects`, `list_sessions`, `search_steps`,
  `get_steps`, `list_workflows`, `get_workflow`, `extract_workflow`, and fix memory: `find_fixes`, `record_fix`,
  `fix_outcome` (record only fixes you verified). Returns only matching steps.
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
- **Fix failing steps** (workflow page): pick an agent (or "fix memory only") and run. A failing step tries
  remembered fixes, then the agent; its permission requests appear as **Allow / Always / Deny** cards. The run
  console has **Output**, **Agent feed** (messages, tool calls, plan, fixes tried, verification, the learned
  change) and **ACP debug** (every JSON-RPC message, filterable, click to expand).
- **Agents** (header badge): each agent's transport and command, a "Test connection" ACP handshake (protocol
  version, capabilities, login methods), and a live "connected" dot while an agent session is running.
- **Fixes**: the fix memory with wins/losses, the error, the steps/patch, and Forget.
- **Runs**: every run log (app and CLI), with exit status, plus `fix-*.log` agent transcripts.
CLI equivalent: `wf run <name> [--steps 3 | 2-5,8] [--dry-run]`.
Security: binds 127.0.0.1 only, checks Host header, POSTs need a per-launch token embedded in the page.
