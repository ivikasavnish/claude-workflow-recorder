#!/usr/bin/env bash
# claude-workflow-recorder installer. Safe to re-run (idempotent).
#
#   ./install.sh                 # everything: skill + hooks + wf CLI + MCP router + macOS app
#   ./install.sh --no-router     # skip the MCP router
#   ./install.sh --no-app        # skip Workflows.app (macOS)
#   ./install.sh --no-hooks      # install files only, don't touch ~/.claude/settings.json
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SKILL_DIR="$CLAUDE_DIR/skills/workflow-recorder"
ROUTER_DIR="${MCP_ROUTER_DIR:-$HOME/mcp-router}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
SETTINGS="$CLAUDE_DIR/settings.json"
DO_HOOKS=1 DO_ROUTER=1 DO_APP=1
for arg in "$@"; do
  case "$arg" in
    --no-hooks) DO_HOOKS=0 ;;
    --no-router) DO_ROUTER=0 ;;
    --no-app) DO_APP=0 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

# Prefer the system python on macOS: hooks run with a minimal environment, venv aliases don't apply.
if [ -x /usr/bin/python3 ] && /usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
  PY=/usr/bin/python3
else
  PY="$(command -v python3 || true)"
fi
[ -n "$PY" ] && "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "python3 >= 3.9 required" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq is required (brew install jq / apt install jq)" >&2; exit 1; }

say "Installing skill -> $SKILL_DIR"
mkdir -p "$SKILL_DIR/app"
cp "$HERE/skill/SKILL.md" "$HERE/skill/wf.py" "$HERE/skill/wf_mcp.py" "$SKILL_DIR/"
cp "$HERE/skill/app/server.py" "$HERE/skill/app/index.html" "$SKILL_DIR/app/"
chmod +x "$SKILL_DIR/wf.py" "$SKILL_DIR/wf_mcp.py" "$SKILL_DIR/app/server.py"
# Pin the interpreter so `wf` works regardless of venvs on PATH.
sed -i.bak "1s|.*|#!$PY|" "$SKILL_DIR/wf.py" && rm -f "$SKILL_DIR/wf.py.bak"

say "Linking wf -> $BIN_DIR/wf"
mkdir -p "$BIN_DIR"
ln -sf "$SKILL_DIR/wf.py" "$BIN_DIR/wf"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "    note: add $BIN_DIR to your PATH" ;; esac

if [ "$DO_HOOKS" = 1 ]; then
  say "Registering hooks in $SETTINGS"
  mkdir -p "$CLAUDE_DIR"
  [ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"
  cp "$SETTINGS" "$SETTINGS.bak-$(date +%Y%m%d-%H%M%S)"
  HOOK_CMD="$PY $SKILL_DIR/wf.py hook"
  # Drop any previous workflow-recorder entries, then append ours - other hooks are left untouched.
  jq --arg cmd "$HOOK_CMD" '
    def strip: map(.hooks |= map(select((.command // "") | test("workflow-recorder/wf\\.py hook") | not)))
               | map(select((.hooks | length) > 0));
    def ours: {hooks: [{type: "command", command: $cmd, timeout: 10}]};
    .hooks = (.hooks // {})
    | .hooks.SessionStart       = (((.hooks.SessionStart       // []) | strip) + [ours])
    | .hooks.PostToolUse        = (((.hooks.PostToolUse        // []) | strip) + [ours])
    | .hooks.PostToolUseFailure = (((.hooks.PostToolUseFailure // []) | strip) + [ours])
  ' "$SETTINGS" > "$SETTINGS.tmp" && mv "$SETTINGS.tmp" "$SETTINGS"
fi

if [ "$DO_ROUTER" = 1 ]; then
  say "Installing MCP router -> $ROUTER_DIR"
  mkdir -p "$ROUTER_DIR"
  cp "$HERE/router/router.py" "$HERE/router/README.md" "$ROUTER_DIR/"
  if [ ! -f "$ROUTER_DIR/servers.json" ]; then
    sed "s|\"command\": \"python3\"|\"command\": \"$PY\"|" "$HERE/router/servers.example.json" > "$ROUTER_DIR/servers.json"
    echo "    created servers.json from the example - edit it to put your stdio MCP servers behind the router"
  else
    echo "    kept existing servers.json"
  fi
  if command -v claude >/dev/null; then
    if claude mcp get router >/dev/null 2>&1; then
      echo "    'router' already registered with Claude Code"
    else
      claude mcp add -s user router -- "$PY" "$ROUTER_DIR/router.py" >/dev/null && echo "    registered 'router' with Claude Code (user scope)"
    fi
  else
    echo "    claude CLI not found - register manually: $PY $ROUTER_DIR/router.py (stdio)"
  fi
fi

if [ "$DO_APP" = 1 ] && [ "$(uname)" = Darwin ]; then
  say "Building Workflows.app -> $HOME/Applications"
  "$HERE/macos/build-app.sh" "$HOME/Applications/Workflows.app"
fi

say "Done. Start a new Claude Code session - it will announce 'Session #0001'. Then try: wf list | wf ui"
