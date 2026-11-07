"""Claude Code sessions in one Terminal window, split into panes (tmux), plus the ones running elsewhere.

Each pane of the "claude-grid" tmux session runs one `claude`. Sessions running in other Terminal tabs can be
moved in: they're stopped and resumed (`claude --resume <id>`) in a new pane, so the conversation carries on.

Folders on an external drive: when the drive is ejected or pulled out, its panes stop what they run and wait in
their place ("parked"), so nothing holds the drive. The moment it's plugged back in, each one resumes by itself.
"""
import glob, json, os, shlex, shutil, signal, subprocess, time
from datetime import datetime
from paths import DATA, WIN

CANDIDATES = ["~/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude", "~/.claude/local/claude"] + (
    ["~/.local/bin/claude.exe", "~/AppData/Roaming/npm/claude.cmd"] if WIN else [])
TMUX_CANDIDATES = ["/opt/homebrew/bin/tmux", "/usr/local/bin/tmux"]
PROJECTS = os.path.expanduser("~/.claude/projects")
SESSION_FILES = os.path.expanduser("~/.claude/sessions")  # <pid>.json, written by each running Claude Code
GRID = "claude-grid"
SAVED = os.path.join(DATA, "grid.json")  # last saved grid, for restoring
SCREENS = os.path.join(DATA, "grid_screens")  # each terminal pane's text
NO_GRID = "The pane grid needs tmux, which Windows doesn't have. New sessions open in Windows Terminal instead."
SCREEN_LINES = 5000  # scrollback kept per terminal pane
HOME = os.path.expanduser("~")
PANE_FORMAT = "\t".join(["#{pane_id}", "#{pane_tty}", "#{pane_current_command}", "#{pane_current_path}",
                         "#{pane_active}", "#{@duo_wait}", "#{@duo_sid}", "#{@duo_claude}", "#{pane_pid}"])
_path = None
_known = {}  # pane id -> its folder and conversation at the last look (a pane on a pulled-out drive can't say)


def drive_of(path):
    """The external drive a folder is on ("/Volumes/My Drive"), or None for the Mac's own disk."""
    parts = os.path.abspath(path or "/").split(os.sep)
    if WIN or len(parts) < 3 or parts[1] != "Volumes" or not parts[2]:
        return None
    root = os.path.join("/Volumes", parts[2])
    return None if os.path.realpath(root) == "/" else root  # "/Volumes/Macintosh HD" is the Mac's own disk


def unplugged(path):
    """Is this folder on a drive that isn't plugged in right now?"""
    d = drive_of(path)
    return bool(d) and not os.path.ismount(d)


def _find(name, candidates):
    for p in [shutil.which(name)] + [os.path.expanduser(c) for c in candidates]:
        if p and os.access(p, os.X_OK):
            return p


def claude_bin():
    return _find("claude", CANDIDATES)


def tmux_bin():
    return None if WIN else _find("tmux", TMUX_CANDIDATES)


def login_path():
    """PATH from the login shell: apps opened from Finder get a bare /usr/bin:/bin PATH."""
    global _path
    if WIN:
        return os.environ.get("PATH", "")
    if _path is None:
        try:
            _path = subprocess.run(["/bin/zsh", "-lc", 'printf %s "$PATH"'], capture_output=True, text=True,
                                   timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            _path = ""
        extra = [os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                 "/usr/sbin", "/sbin"]
        _path = ":".join(dict.fromkeys([p for p in _path.split(":") if p] + extra))
    return _path


def clean_env():
    """Our launcher's Python settings must not leak into the shells and tools in the panes."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH", "__CFBundleIdentifier")}
    env.update(PATH=login_path(), TERM="xterm-256color")
    env.setdefault("LANG", "en_US.UTF-8")
    return env


def tmux(*args):
    exe = tmux_bin()
    if not exe:
        raise ValueError(NO_GRID if WIN else "tmux isn't installed. Install it with: brew install tmux")
    try:
        return subprocess.run([exe, *args], capture_output=True, text=True, timeout=10, env=clean_env())
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f"tmux failed: {e}")


def osascript(script, *args):
    try:
        r = subprocess.run(["osascript", "-e", script, *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return "", str(e)
    return r.stdout.strip(), r.stderr.strip()


def grid_exists():
    return bool(tmux_bin()) and tmux("has-session", "-t", GRID).returncode == 0


def grid_panes():
    """{tty: pane id} for every pane in the grid."""
    if not grid_exists():
        return {}
    out = tmux("list-panes", "-s", "-t", GRID, "-F", "#{pane_tty} #{pane_id}").stdout
    return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)


def session_file(pid):
    try:
        with open(os.path.join(SESSION_FILES, f"{pid}.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _etime(seconds):
    """Elapsed time written the way `ps` does: [[dd-]hh:]mm:ss."""
    s = max(0, int(seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return (f"{d}-" if d else "") + (f"{h:02d}:" if d or h else "") + f"{m:02d}:{s:02d}"


def _alive_claude(pid, started_ms):
    """Is this pid still the Claude Code that wrote the session file (not a new process reusing the number)?"""
    try:
        import psutil
        p = psutil.Process(pid)
        if "claude" not in p.name().lower() and "node" not in p.name().lower():
            return False
        return not started_ms or abs(p.create_time() - started_ms / 1000) < 120
    except ImportError:
        pass
    except Exception:  # gone, or not ours to look at
        return False
    if WIN:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running_from_files(now=None):
    """Interactive Claude sessions from the files Claude Code keeps in ~/.claude/sessions (how Windows finds them)."""
    now = now or time.time()
    found = []
    for path in glob.glob(os.path.join(SESSION_FILES, "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                info = json.load(f)
            pid = int(info["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if info.get("kind", "interactive") != "interactive" or not _alive_claude(pid, info.get("startedAt")):
            continue
        cwd = info.get("cwd") or ""
        found.append({"pid": pid, "elapsed": _etime(now - (info.get("startedAt") or now * 1000) / 1000), "tty": "",
                      "cwd": cwd, "name": os.path.basename(cwd.rstrip("/\\")) or "claude",
                      "session_id": info.get("sessionId"), "status": info.get("status", ""), "pane": None})
    return sorted(found, key=lambda s: s["pid"])


def running():
    """Interactive Claude sessions (ones attached to a terminal): folder, status, and whether they're in the grid."""
    if WIN:
        return running_from_files()
    try:
        ps = subprocess.run(["ps", "-axo", "pid=,etime=,tty=,comm="], capture_output=True, text=True,
                            timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in ps.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4 or os.path.basename(parts[3]) != "claude" or parts[2] in ("??", "-"):
            continue
        found.append({"pid": int(parts[0]), "elapsed": parts[1], "tty": "/dev/" + parts[2], "cwd": "", "name": "claude"})
    if not found:
        return []
    try:
        out = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fn", "-p", ",".join(str(s["pid"]) for s in found)],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    cwds, pid = {}, None
    for line in out.splitlines():
        if line[:1] == "p":
            pid = int(line[1:])
        elif line[:1] == "n" and pid:
            cwds[pid] = line[1:]
    try:
        panes = grid_panes()
    except ValueError:
        panes = {}
    for s in found:
        info = session_file(s["pid"])
        s["cwd"] = cwds.get(s["pid"]) or info.get("cwd", "")
        s["name"] = os.path.basename(s["cwd"].rstrip("/")) or "claude"
        s["session_id"] = info.get("sessionId")
        s["status"] = info.get("status", "")  # "busy" while Claude is working, "idle" while it waits for you
        s["pane"] = panes.get(s["tty"])
    return found


def _style():
    """Look of the grid: folder name on each pane's border, mouse to pick and resize panes."""
    for opt in (["mouse", "on"], ["status-style", "bg=#d97757,fg=#1a1a1a"],
                ["status-left", " ✦ Claude grid "], ["status-left-length", "20"],
                ["status-right", " click a pane to type in it · drag borders to resize · Ctrl-b z zooms "],
                ["status-right-length", "80"]):
        tmux("set-option", "-t", GRID, *opt)
    for opt in (["pane-border-status", "top"],
                ["pane-border-format", " #{?@duo_wait,⏏ #{b:@duo_wait} (unplugged),#{b:pane_current_path}} "],
                ["pane-active-border-style", "fg=#d97757"], ["pane-border-style", "fg=#555555"]):
        tmux("set-option", "-w", "-t", GRID, *opt)


def shell_cmd():
    """Your login shell; zsh is told to write each command to its history right away, so a crash can't lose it."""
    shell = os.environ.get("SHELL") or "/bin/zsh"
    return f"exec {shlex.quote(shell)} -l" + (" -o incappendhistory" if os.path.basename(shell) == "zsh" else "")


def add_pane(cwd, args, claude=True, show=True, replay=None, cmd=None):
    """New pane in the grid (created on first use): `claude <args>`, or just your shell; then show it.

    A Claude pane becomes a normal shell when Claude exits, so it stays usable as a terminal.
    `replay` is a saved screen to print first, so a restored terminal pane shows what it showed before.
    `cmd` runs instead of either (a pane waiting for its drive: see wait_cmd).
    """
    if cmd:
        pass
    elif claude:
        exe = claude_bin()
        if not exe:
            raise ValueError("Claude Code isn't installed (no `claude` command found).")
        cmd = f"{shlex.join([exe, *args])}; {shell_cmd()}"
    elif replay and os.path.isfile(replay):
        note = "\\033[2m── restored — the lines above are from before ──\\033[0m"
        cmd = f"cat {shlex.quote(replay)}; printf '%b\\n' '{note}'; {shell_cmd()}"
    else:
        cmd = shell_cmd()
    env = ["-e", f"PATH={login_path()}"]
    if grid_exists():
        r = tmux("split-window", "-t", GRID, *env, "-c", cwd, cmd)
        tmux("select-layout", "-t", GRID, "tiled")
    else:
        r = tmux("new-session", "-d", "-s", GRID, "-x", "220", "-y", "60", *env, "-c", cwd, cmd)
        if r.returncode == 0:
            _style()
    if r.returncode != 0:
        msg = r.stderr.strip()
        raise ValueError("The window is too full for another pane: close one or make the window bigger."
                         if "no space" in msg else f"Couldn't add a pane: {msg}")
    if show:
        show_grid()


def win_terminal_cmd(cwd, cont=False, kind="claude"):
    """The command that opens Claude Code (or a plain terminal) in a new Windows Terminal tab, or a console window."""
    claude = [claude_bin() or "claude"] + (["--continue"] if cont else []) if kind != "shell" else []
    wt = shutil.which("wt")
    if wt:
        return [wt, "-w", "0", "new-tab", "--title", os.path.basename(cwd.rstrip("/\\")) or cwd, "-d", cwd] + claude
    return ["cmd.exe", "/c", "start", "", "/D", cwd, "cmd.exe", "/k"] + (claude or ["echo", "Claude Duo"])


def new_session(cwd, cont=False, kind="claude"):
    """kind "claude" starts Claude Code; "shell" opens a plain terminal pane."""
    cwd = os.path.expanduser((cwd or "").strip() or "~")
    if not os.path.isdir(cwd):
        raise ValueError(f"Folder not found: {cwd}")
    if WIN:
        if kind != "shell" and not claude_bin():
            raise ValueError("Claude Code isn't installed. See https://claude.com/claude-code")
        try:
            subprocess.Popen(win_terminal_cmd(cwd, cont, kind), cwd=cwd, close_fds=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            raise ValueError(f"Couldn't open a terminal: {e}")
        return {"ok": True}
    add_pane(cwd, ["--continue"] if cont else [], claude=kind != "shell")
    return {"ok": True}


ATTACH = '''
on run argv
  tell application "Terminal"
    activate
    do script (item 1 of argv)
  end tell
end run
'''


def show_grid():
    """Bring the grid's Terminal window to the front, opening one if nothing shows the grid yet."""
    if not grid_exists():
        return False
    clients = tmux("list-clients", "-t", GRID, "-F", "#{client_tty}").stdout.split()
    if clients:
        return osascript(FOCUS, clients[0])[0] == "ok"
    return osascript(ATTACH, f"exec {shlex.quote(tmux_bin())} attach -t {GRID}")[1] == ""


def move_to_grid(pid):
    """Stop a session running elsewhere and resume the same conversation in a grid pane."""
    s = next((s for s in running() if s["pid"] == pid), None)
    if not s:
        raise ValueError("That session isn't running any more.")
    if s["pane"]:
        show_grid()
        return {"ok": True}
    if not s["session_id"] or not os.path.isdir(s["cwd"]):
        raise ValueError("Can't tell which conversation that session is on, so it can't be moved.")
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):  # wait for it to exit, so two copies never write the same conversation
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    else:
        raise ValueError("The session didn't stop, so it wasn't moved.")
    add_pane(s["cwd"], ["--resume", s["session_id"]])
    return {"ok": True}


def grid_info():
    """The grid's panes for the window list: what each one runs and in which folder."""
    if not grid_exists():
        return []
    out = tmux("list-panes", "-s", "-t", GRID, "-F", PANE_FORMAT).stdout
    panes = []
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) == 9:
            folder = f[5] or f[3]  # a parked pane is named after the folder it's waiting for
            panes.append({"pane": f[0], "tty": f[1], "command": f[2], "cwd": f[3], "active": f[4] == "1",
                          "name": os.path.basename(folder.rstrip("/")) or folder,
                          "waiting": f[5], "wait_sid": f[6] or None, "wait_claude": f[7] == "1",
                          "pid": int(f[8]) if f[8].isdigit() else None})
    return panes


def wait_cmd(cwd, session_id=None, claude=True):
    """What a pane runs while its drive is unplugged: a note, then (checked every second) as soon as the folder
    is back, the Claude conversation resumes there, or the terminal reopens there. Ctrl-C: a plain terminal now.
    Parked for an eject, the drive is still there for a moment: it first waits for it to go (up to 20 s; an
    eject that failed leaves it, and the pane resumes). Marked (@duo_wait) while waiting, for save and restore."""
    me = f'{shlex.quote(tmux_bin() or "tmux")} set-option -p -t "$TMUX_PANE"'
    q = shlex.quote(cwd)
    drive = os.path.basename(drive_of(cwd) or os.path.dirname(cwd.rstrip("/")) or cwd)
    if claude:
        exe = claude_bin() or "claude"
        run = shlex.join([exe, "--resume", session_id] if session_id else [exe, "--continue"])
        what = "this Claude session picks up where it left off"
    else:
        run, what = ":", "this terminal reopens in its folder"
    note = f"⏏  {drive} is unplugged. Plug it back in and {what}.   (Ctrl-C: use this pane for something else)"
    denied = (f"Claude Duo isn't allowed to open {drive}. Turn it on in System Settings > Privacy & Security > "
              "Files & Folders > Claude Duo > Removable Volumes.")
    return (f"{me} @duo_wait {q}; {me} @duo_sid {shlex.quote(session_id or '')}; {me} @duo_claude {int(claude)}; "
            f"printf '\\033[2m%s\\033[0m\\n' {shlex.quote(note)}; stop=; n=0; trap 'stop=1' INT; "
            f'while [ -z "$stop" ] && [ -d {q} ] && [ $n -lt 100 ]; do sleep 0.2; n=$((n+1)); done; '
            f'while [ -z "$stop" ] && [ ! -d {q} ]; do sleep 1; done; trap - INT; {me} -u @duo_wait; '
            f'if [ -z "$stop" ]; then if cd {q}; then {run}; else printf \'%s\\n\' {shlex.quote(denied)}; fi; fi; '
            f"{shell_cmd()}")


def park(pane, cwd, session_id=None, claude=True):
    """Stop what a pane runs (its drive is going away) and leave it waiting in its place for the drive."""
    for opt, val in (("@duo_wait", cwd), ("@duo_sid", session_id or ""), ("@duo_claude", str(int(claude)))):
        tmux("set-option", "-p", "-t", pane, opt, val)  # set before the swap, so a save in between sees it parked
    return tmux("respawn-pane", "-k", "-t", pane, "-c", HOME, "-e", f"PATH={login_path()}",
                wait_cmd(cwd, session_id, claude)).returncode == 0


def _gone(pid):
    try:
        os.kill(pid, 0)
        return False
    except OSError:
        return True


def _tree(roots):
    """These processes and every process they started (a pane's shell, its Claude, the tools Claude ran)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    kids = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    seen, todo = set(), [r for r in roots if r]
    while todo:
        pid = todo.pop()
        if pid not in seen:
            seen.add(pid)
            todo.extend(kids.get(pid, []))
    return seen


def _wait_gone(pids, secs):
    end = time.time() + secs
    while time.time() < end and not all(_gone(p) for p in pids):
        time.sleep(0.05)
    return [p for p in pids if not _gone(p)]


def release_drive(drive):
    """Park every grid pane working on this drive, so nothing of theirs holds it (it can be ejected) and each one
    resumes by itself when the drive is back. Returns how many panes were parked."""
    mine = []
    for p in grid_info():
        if p["waiting"]:
            continue
        cwd = p["cwd"] if drive_of(p["cwd"]) == drive else _known.get(p["pane"], {}).get("cwd") or ""
        if drive_of(cwd) == drive:
            mine.append((p, cwd))
    if not mine:
        return 0
    by_tty = {s["tty"]: s for s in running() if s["pane"]}
    procs = _tree(p.get("pid") for p, _ in mine)  # before the swap: afterwards they're no longer the pane's
    for p, cwd in mine:
        s = by_tty.get(p["tty"])
        sid = s and (s.get("session_id") or _known.get(p["pane"], {}).get("session_id"))
        park(p["pane"], cwd, sid, claude=bool(s))
    left = _wait_gone(procs, 2)  # each pane's shell and Claude exit when the pane is swapped
    for pid in left:  # what they left running (a tool Claude started, say) would still hold the drive
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    _wait_gone(left, 1)
    save_grid()
    return len(mine)


def park_unplugged():
    """Drives pulled out without ejecting: park their panes, which resume by themselves when the drive is back.
    Returns {drive: panes parked}."""
    gone = set()
    for p in grid_info():
        cwd = p["cwd"] if drive_of(p["cwd"]) else _known.get(p["pane"], {}).get("cwd") or ""
        if not p["waiting"] and unplugged(cwd):
            gone.add(drive_of(cwd))
    return {d: n for d in sorted(gone) if (n := release_drive(d))}


def _peek(folder):
    try:
        os.listdir(folder)
    except OSError:
        pass


def ask_access(folders, wait=60):
    """Look in each folder once from Claude Duo itself, so macOS asks "Claude Duo would like to access files on a
    removable volume / in your Desktop folder" up front; the sessions it starts then get the same access.
    Waits up to `wait` seconds for you to answer (a drive that doesn't respond can't hold things up longer)."""
    import threading
    looks = [threading.Thread(target=_peek, args=(d,), daemon=True) for d in dict.fromkeys(f for f in folders if f)]
    for t in looks:
        t.start()
    end = time.time() + wait
    for t in looks:
        t.join(max(0, end - time.time()))


def pane_action(pane, what):
    """Select ("focus") or close ("close") one grid pane."""
    if pane not in {p["pane"] for p in grid_info()}:
        return False
    if what == "focus":
        tmux("select-pane", "-t", pane)
        return show_grid()
    if what == "close":
        r = tmux("kill-pane", "-t", pane)
        if grid_exists():
            tmux("select-layout", "-t", GRID, "tiled")
        save_grid()  # a pane you closed on purpose must not come back on restore
        return r.returncode == 0
    return False


def snapshot():
    """What's in the grid right now: each pane's folder and Claude conversation, plus the pane sizes."""
    panes = grid_info()
    if not panes:
        return None
    by_tty = {s["tty"]: s for s in running() if s["pane"]}
    layout = tmux("display-message", "-p", "-t", GRID, "#{window_layout}").stdout.strip()
    out, known = [], {}
    for p in panes:
        if p["waiting"]:  # parked: what it will resume when its drive is back
            entry = {"cwd": p["waiting"], "session_id": p["wait_sid"], "claude": p["wait_claude"], "screen": None}
        elif p["pane"] in _known and unplugged(_known[p["pane"]]["cwd"]):  # drive just pulled out, not parked yet
            entry = _known[p["pane"]]
        else:
            claude = p["tty"] in by_tty
            entry = {"cwd": p["cwd"], "session_id": (by_tty.get(p["tty"]) or {}).get("session_id"),
                     "claude": claude, "screen": None if claude else save_screen(p["pane"])}
        known[p["pane"]] = entry
        out.append(entry)
    _known.clear()
    _known.update(known)
    return {"layout": layout, "panes": out}


def save_screen(pane):
    """Save a terminal pane's scrollback, with colours (Claude panes don't need it: their chat is resumed)."""
    text = tmux("capture-pane", "-p", "-e", "-J", "-S", f"-{SCREEN_LINES}", "-t", pane).stdout.rstrip() + "\n"
    os.makedirs(SCREENS, exist_ok=True)
    path = os.path.join(SCREENS, pane.lstrip("%") + ".txt")
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:  # unchanged: don't rewrite it every few seconds
                return path
    except OSError:
        pass
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(path + ".tmp", path)
    return path


def save_grid():
    """Remember the grid (called every few seconds). A grid that's gone isn't saved over the last good copy,
    so after a restart or crash it can be rebuilt; closing it yourself is handled by forget_grid()."""
    try:
        snap = snapshot()
    except ValueError:
        return
    if snap:
        tmp = SAVED + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=1)
        os.replace(tmp, SAVED)
        keep = {p.get("screen") for p in snap["panes"]}
        for f in glob.glob(os.path.join(SCREENS, "*.txt")):  # screens of panes that are gone
            if f not in keep:
                os.remove(f)


def forget_grid():
    try:
        os.remove(SAVED)
    except OSError:
        pass


def restore_grid():
    """Rebuild the saved grid if it isn't running (after a restart or a crash): Claude panes resume their
    conversations, terminal panes reopen in their folders, and the pane sizes come back."""
    if not tmux_bin() or grid_exists():
        return 0
    try:
        with open(SAVED, encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return 0
    busy = {s["session_id"] for s in running() if s["session_id"]}  # already open elsewhere: don't run it twice
    ask_access(p.get("cwd") for p in snap.get("panes", []) if os.path.isdir(p.get("cwd") or ""))
    made = 0
    for p in snap.get("panes", []):
        cwd = p.get("cwd") if os.path.isdir(p.get("cwd") or "") else os.path.expanduser("~")
        sid = p.get("session_id")
        try:
            if unplugged(p.get("cwd")):  # its drive isn't plugged in: wait for it, and resume then
                claude = bool(p.get("claude")) and sid not in busy
                add_pane(HOME, [], show=False, cmd=wait_cmd(p["cwd"], sid if claude else None, claude))
            elif p.get("claude") and sid and sid not in busy:
                add_pane(cwd, ["--resume", sid], show=False)
            elif p.get("claude") and not sid:
                add_pane(cwd, ["--continue"], show=False)
            else:  # a terminal pane (or a conversation that's already open in another window)
                add_pane(cwd, [], claude=False, show=False, replay=p.get("screen"))
            made += 1
        except ValueError:
            continue
    if made:
        if made == len(snap.get("panes", [])) and snap.get("layout"):
            tmux("select-layout", "-t", GRID, snap["layout"])
        show_grid()
    return made


def gather():
    """Move every session that's outside the grid into it."""
    moved, errors = 0, []
    for s in running():
        if not s["pane"]:
            try:
                move_to_grid(s["pid"])
                moved += 1
            except ValueError as e:
                errors.append(f"{s['name']}: {e}")
    if not moved and not errors:
        show_grid()
    return {"ok": not errors, "moved": moved, "error": "; ".join(errors)}


FOCUS = '''
on run argv
  set want to item 1 of argv
  if application "Terminal" is running then
    tell application "Terminal"
      repeat with w in windows
        repeat with t in tabs of w
          if tty of t is want then
            set selected of t to true
            set index of w to 1
            activate
            return "ok"
          end if
        end repeat
      end repeat
    end tell
  end if
  if application "iTerm" is running then
    tell application "iTerm"
      repeat with w in windows
        repeat with t in tabs of w
          repeat with s in sessions of t
            if tty of s is want then
              select w
              select t
              activate
              return "ok"
            end if
          end repeat
        end repeat
      end repeat
    end tell
  end if
  return "missing"
end run
'''


def focus(pid):
    if WIN:
        return False  # Windows can't bring one terminal tab to the front from outside
    s = next((s for s in running() if s["pid"] == pid), None)
    if not s:
        return False
    if s["pane"]:  # in the grid: select its pane, then show the grid window
        tmux("select-pane", "-t", s["pane"])
        return show_grid()
    return osascript(FOCUS, s["tty"])[0] == "ok"


def finished(before, now):
    """Sessions that were working at the last check and are now waiting for you."""
    return [s for s in now if before.get(s["pid"]) == "busy" and s.get("status") == "idle"]


def stop(pid):
    if not any(s["pid"] == pid for s in running()):  # only ever signal processes that really are Claude sessions
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def recent_folders(limit=8):
    """Folders you've used Claude in, newest first (taken from the session transcripts)."""
    seen = []
    files = sorted(glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")), key=os.path.getmtime, reverse=True)
    for f in files:
        cwd = None
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for _, line in zip(range(30), fh):
                    if '"cwd"' in line:
                        cwd = json.loads(line).get("cwd")
                        if cwd:
                            break
        except (OSError, ValueError):
            continue
        if cwd and cwd not in seen and os.path.isdir(cwd):
            seen.append(cwd)
            if len(seen) >= limit:
                break
    return seen
