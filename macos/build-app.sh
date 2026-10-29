#!/usr/bin/env bash
# Build Workflows.app: a tiny launcher bundle that starts the local viewer/runner and opens it.
#   macos/build-app.sh [~/Applications/Workflows.app]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="${1:-$HOME/Applications/Workflows.app}"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Workflows</string>
  <key>CFBundleDisplayName</key><string>Workflows</string>
  <key>CFBundleIdentifier</key><string>io.github.claude-workflow-recorder</string>
  <key>CFBundleVersion</key><string>0.1.0</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Workflows</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
EOF

cat > "$APP/Contents/MacOS/Workflows" <<'EOF'
#!/bin/bash
# Start the local viewer/runner server if needed, then open it as an app window.
PORT="${WF_APP_PORT:-47821}"
URL="http://127.0.0.1:$PORT/"
SERVER="$HOME/.claude/skills/workflow-recorder/app/server.py"
LOG="$HOME/workflows/.app.log"
mkdir -p "$HOME/workflows"
if ! /usr/bin/curl -sf -o /dev/null "$URL/health"; then
  # Apps launched from Finder get a bare PATH; start through a login+interactive shell so
  # workflow runs see the same PATH / JAVA_HOME / ANDROID_HOME as your terminal.
  SH="${SHELL:-/bin/zsh}"
  POWERLEVEL9K_DISABLE_GITSTATUS=true POWERLEVEL9K_INSTANT_PROMPT=off \
    nohup "$SH" -ilc "exec /usr/bin/python3 '$SERVER' --port $PORT" >> "$LOG" 2>&1 </dev/null &
  for _ in $(seq 1 150); do /usr/bin/curl -sf -o /dev/null "$URL/health" && break; sleep 0.2; done
fi
if [ -d "/Applications/Google Chrome.app" ]; then
  open -na "Google Chrome" --args --app="$URL" --window-size=1280,860
else
  open "$URL"
fi
EOF
chmod +x "$APP/Contents/MacOS/Workflows"

# Icon: render the SVG with Quick Look, then build an .icns.
TMP="$(mktemp -d)"
if qlmanage -t -s 1024 -o "$TMP" "$HERE/icon.svg" >/dev/null 2>&1 && [ -f "$TMP/icon.svg.png" ]; then
  mkdir -p "$TMP/AppIcon.iconset"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$TMP/icon.svg.png" --out "$TMP/AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) "$TMP/icon.svg.png" --out "$TMP/AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$TMP/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
fi
touch "$APP"
echo "    built $APP"
