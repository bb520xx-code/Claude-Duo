# Claude Duo

A macOS menu bar app for people who live in [Claude Code](https://claude.com/claude-code), now with an iPhone version.

## New: Claude Duo on your iPhone

Check your Claude limits from the couch. Claude Duo on your Mac now serves a phone version over your Wi-Fi:
scan a QR code, tap **Add to Home Screen**, and it opens full screen like any other app. No App Store, no
Xcode, nothing to install on the phone.

<p align="center">
  <img src="docs/iphone-usage.png" width="260" alt="Usage tab: the 5-hour session ring, limits with reset times, and token charts">
  &nbsp;&nbsp;
  <img src="docs/iphone-sessions.png" width="260" alt="Sessions tab: which Claude sessions are working or waiting for you">
</p>

- **Usage**: your 5-hour and weekly limits with reset countdowns and pace, live from your Mac, plus token
  charts, busiest hours, top projects and models.
- **Sessions**: every Claude session on your Mac, and whether it's working or waiting for you.
- **Safe**: look-only, behind a secret link, and only on your own network.

[Set it up in a minute →](#iphone-app) *(Screenshots use demo data.)*

## On your Mac

- **Plan usage in the menu bar**: `✦ 47%` for your 5-hour session (or weekly, or both), coloured
  green → yellow → red at thresholds you choose. A `●` appears while any Claude session is working.
- **Live, exact numbers**: with *Live numbers from Claude Code* on (⚙), the % comes from the usage figures
  Anthropic sends back with every Claude reply, so it updates within a second and can't be rate-limited.
  Otherwise it checks Anthropic's usage endpoint, and always shows how fresh the number is.
- **Liquid Glass popup** with Usage, Sessions and Settings tabs: limits with reset times and pace,
  busiest hours, a 14-day token sparkline.
- **One terminal grid for all your sessions**: Claude Code and plain shell panes in a single tmux window
  you can drag and resize with the mouse. Move an existing Claude Code session into the grid with its
  full conversation (`claude --resume`).
- **Crash-proof grid**: the layout, each pane's folder, its Claude conversation, screen text and shell
  history are saved every few seconds and rebuilt after a crash or restart.
- **iPhone app**: your usage, limits, token charts and which sessions are working, on your iPhone's Home Screen.
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
- **⚙ → Live numbers from Claude Code** adds a small status line to Claude Code (`~/.claude/settings.json`)
  that passes on the usage numbers from each reply; it keeps any status line you already had, and turning
  it off puts yours back.

Organization API usage (Console) needs an Admin API key (`sk-ant-admin…`), pasted in the Usage tab.
It is kept in memory only.

## iPhone app

Claude Duo on your Mac also serves a phone version over your Wi-Fi.

1. On the Mac: Claude Duo **⚙ → iPhone app** (on). A QR code appears.
2. On the iPhone: point the Camera at the QR code and tap the link.
3. In Safari tap **Share → Add to Home Screen**. It opens full screen with the Claude Duo icon, like an app.

It shows the same numbers as the Mac: the session and weekly limits with reset times and pace, token charts,
busiest hours, top projects, models, and which Claude sessions are working or waiting for you.

- **Read-only.** It has no way to change anything on the Mac: no terminal or settings endpoints exist on its
  port (8766), and it only answers under a secret link. **⚙ → New link** locks out every phone that had the old one.
- **Same Wi-Fi.** The iPhone needs to be on the Mac's network (or a VPN such as Tailscale to reach it from
  anywhere). Away from it, the app shows the last numbers it got, and a link to the official usage page on claude.ai.
- **Why not the App Store or a download?** iPhones only install apps from the App Store, TestFlight or Xcode.
  A Home Screen web app needs none of that, and its code ships here in `iphone/` and `phone.py`.

## Develop

```sh
cd ~/ClaudeUsage
PYTHONPATH=.:.venv/lib/python3.14/site-packages /opt/homebrew/bin/python3.14 -m unittest discover -s tests
```
