# Claude Duo

A macOS menu bar app for people who live in [Claude Code](https://claude.com/claude-code).

- **Plan usage in the menu bar**: `✦ 47%` for your 5-hour session (or weekly, or both), coloured
  green → yellow → red at thresholds you choose. A `●` appears while any Claude session is working.
- **Liquid Glass popup** with Usage, Sessions and Settings tabs: limits with reset times and pace,
  busiest hours, a 14-day token sparkline.
- **One terminal grid for all your sessions**: Claude Code and plain shell panes in a single tmux window
  you can drag and resize with the mouse. Move an existing Claude Code session into the grid with its
  full conversation (`claude --resume`).
- **Crash-proof grid**: the layout, each pane's folder, its Claude conversation, screen text and shell
  history are saved every few seconds and rebuilt after a crash or restart.
- **Notifications** near your limit and when Claude finishes in any session.
- Quick links to claude.ai settings, billing, docs and status.

> Not affiliated with Anthropic. Uses the sign-in Claude Code already saved on your Mac to read your
> plan usage, and talks only to `api.anthropic.com`. Everything else stays on your machine.

## Requirements

- macOS on Apple silicon, [Homebrew](https://brew.sh)
- Claude Code, signed in with a Claude plan (Pro / Max)
- Python 3.14 and tmux (the installer adds them via Homebrew)

## Install

```sh
git clone https://github.com/bb520xx-code/Claude-Duo.git
cd Claude-Duo
./install.sh
open ~/Applications/"Claude Duo.app"
```

The installer copies the code to `~/ClaudeUsage`, creates a Python virtual environment there, and builds
`~/Applications/Claude Duo.app` (a small C launcher that runs the Python app in-process, so macOS gives it a
menu bar icon). If the icon doesn't show, allow Claude Duo under System Settings → Menu Bar.

## Use

- Click `✦ %` in the menu bar for the popup. Keys: `1` Usage, `2` Sessions, `,` Settings, `R` refresh.
- **Sessions → Add a pane** opens Claude Code or a terminal in a folder; **⇲** moves another Terminal
  window's Claude session into the grid.
- ✕ hides the window and the app keeps running; ⌘Q or ⏻ quits.

Organization API usage (Console) needs an Admin API key (`sk-ant-admin…`), pasted in the Usage tab.
It is kept in memory only.

## Develop

```sh
cd ~/ClaudeUsage
PYTHONPATH=.:.venv/lib/python3.14/site-packages /opt/homebrew/bin/python3.14 -m unittest discover -s tests
```

## License

MIT
