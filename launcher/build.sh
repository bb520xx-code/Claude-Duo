#!/bin/zsh
# Builds the Claude Duo app executable and installs it into the app: build.sh "/path/to/Claude Duo.app"
set -e
APP="$1"
OPT=/opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14  # stable across Homebrew patch updates
if [ -d "$OPT" ]; then HOMEDIR=$OPT; else PY=$(readlink -f "$HOME/ClaudeUsage/.venv/bin/python"); HOMEDIR=${PY:h:h}; fi
BIN=$HOMEDIR/bin
CFG="$BIN/python3.14-config"
clang -O2 -DPYHOME="\"$HOMEDIR\"" $("$CFG" --includes) "${0:h}/main.c" -o "${0:h}/Claude Duo" $("$CFG" --ldflags --embed)
cp "${0:h}/Claude Duo" "$APP/Contents/MacOS/Claude Duo"
plutil -replace CFBundleExecutable -string "Claude Duo" "$APP/Contents/Info.plist"
rm -f "$APP/Contents/MacOS/launch"
codesign --force --deep -s - "$APP"
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP"
echo "installed into $APP"
