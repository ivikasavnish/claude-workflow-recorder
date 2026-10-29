#!/usr/bin/env bash
# Removes hooks, the wf link, the router registration and the app. Recorded data in ~/workflows is kept.
set -euo pipefail
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SETTINGS="$CLAUDE_DIR/settings.json"
ROUTER_DIR="${MCP_ROUTER_DIR:-$HOME/mcp-router}"

if [ -f "$SETTINGS" ] && command -v jq >/dev/null; then
  cp "$SETTINGS" "$SETTINGS.bak-$(date +%Y%m%d-%H%M%S)"
  jq '
    def strip: map(.hooks |= map(select((.command // "") | test("workflow-recorder/wf\\.py hook") | not)))
               | map(select((.hooks | length) > 0));
    if .hooks then .hooks |= with_entries(.value |= strip) | .hooks |= with_entries(select((.value | length) > 0)) else . end
  ' "$SETTINGS" > "$SETTINGS.tmp" && mv "$SETTINGS.tmp" "$SETTINGS"
  echo "removed hooks from $SETTINGS"
fi
command -v claude >/dev/null && claude mcp remove -s user router >/dev/null 2>&1 && echo "unregistered MCP 'router'" || true
pkill -f 'workflow-recorder/app/server.py' 2>/dev/null || true
[ -L "$HOME/.local/bin/wf" ] && unlink "$HOME/.local/bin/wf" && echo "removed ~/.local/bin/wf"

echo
echo "Left in place (delete yourself if you want):"
echo "  $CLAUDE_DIR/skills/workflow-recorder   (skill + app)"
echo "  $ROUTER_DIR                            (router + your servers.json)"
echo "  $HOME/Applications/Workflows.app"
echo "  $HOME/workflows                        (your recorded sessions - may contain sensitive commands)"
