#!/bin/zsh
# Installs Claude Duo: copies the code to ~/ClaudeUsage, sets up Python, and builds ~/Applications/Claude Duo.app
set -e
SRC=${0:A:h}
DEST=$HOME/ClaudeUsage            # the app runs its code from here (the Desktop is privacy-protected)
APP=$HOME/Applications/"Claude Duo.app"

command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
[ -x /opt/homebrew/bin/python3.14 ] || brew install python@3.14
command -v tmux >/dev/null || brew install tmux

mkdir -p "$DEST"
if [ "$SRC" != "$DEST" ]; then
  rsync -a --exclude .git --exclude .venv --exclude '*.app' "$SRC"/ "$DEST"/
fi
/opt/homebrew/bin/python3.14 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$DEST/AppIcon.icns" "$APP/Contents/Resources/"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleDisplayName</key><string>Claude Duo</string>
  <key>CFBundleName</key><string>Claude Duo</string>
  <key>CFBundleExecutable</key><string>Claude Duo</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleIdentifier</key><string>local.claude.usage</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>2.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
"$DEST/launcher/build.sh" "$APP"
echo "Done. Open it with:  open \"$APP\""
