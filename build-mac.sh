#!/bin/zsh
# Builds the downloadable Mac app: dist/Claude Duo.app, zipped as dist/Claude-Duo-Mac.zip.
# Needs a Python with requirements.txt, segno and pyinstaller installed:  ./build-mac.sh [python]
set -e
cd ${0:A:h}
PY=${1:-python3}
rm -rf build dist
"$PY" -m PyInstaller --noconfirm --windowed --name "Claude Duo" --icon AppIcon.icns \
  --osx-bundle-identifier local.claude.duo \
  --add-data "index.html:." --add-data "compact.html:." --add-data "settings.html:." --add-data "sessions.html:." \
  --add-data "glass.css:." --add-data "ui.js:." --add-data "iphone:iphone" \
  --hidden-import phone --hidden-import statusline --hidden-import menubar --hidden-import drives --hidden-import drivesync macapp.py
APP="dist/Claude Duo.app"
# a menu bar app: no Dock icon
plutil -insert LSUIElement -bool true "$APP/Contents/Info.plist"
V=${VERSION:-2.1.0}; [[ $V == [0-9]* ]] || V=2.1.0  # a tag like v2.1.0 gives 2.1.0; a branch build keeps the default
plutil -replace CFBundleShortVersionString -string "$V" "$APP/Contents/Info.plist"
codesign --force --deep --sign - "$APP"  # re-seal after editing Info.plist (ad hoc: not from an identified developer)
ditto -c -k --keepParent "$APP" dist/Claude-Duo-Mac.zip
echo "Built $APP and dist/Claude-Duo-Mac.zip"
