"""Your project folders on this Mac and on an external drive, kept in step, and shared between Macs.

A "pair" is a folder on the drive and a folder on the Mac (say My Drive/Projects and ~/Projects).
You work in the Mac's folder, so you can work with or without the drive. While the drive is plugged in,
Claude Duo copies changes both ways (the newer file wins; the one it replaces is kept in an "overwritten"
folder), every minute and when you eject.

It also leaves the session grid, the Claude conversations in it and your settings on the drive
(<drive>/.claude-duo). Plug the drive into another Mac with Claude Duo and that Mac gets the same files,
the same settings and the same grid, each pane carrying on the same conversation in the same folder.

The pairs are written on the drive itself (.claude-duo/sync.json), so every Mac that sees it joins in.
Deleting a file doesn't delete it on the other side (it would come back): nothing is ever removed.
"""
import glob, json, os, re, shutil, subprocess, threading, time

import meter, sessions
from paths import DATA

STATE = ".claude-duo"  # Claude Duo's folder on the drive
EXCLUDE = [".DS_Store", "._*", ".Trashes", ".Spotlight-V100", ".fseventsd", ".TemporaryItems", STATE,
           ".venv", "venv", "__pycache__", "node_modules"]  # these belong to one Mac (and hold symlinks)
MAC_ONLY = {"phone", "phone_token", "statusline_prev", "drive_sync"}  # settings that stay on each Mac
EVERY = 60  # seconds between syncs while the drive is plugged in
KEEP_DAYS = 30  # conversations older than this aren't carried over
ADOPTED = os.path.join(DATA, "drive_grid_adopted.json")
LOG = os.path.join(DATA, "drivesync.log")  # what the last syncs did, for when something looks wrong
lock = threading.Lock()


def log(msg):
    try:
        if os.path.getsize(LOG) > 200_000:
            os.replace(LOG, LOG + ".old")
    except OSError:
        pass
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def this_mac():
    try:
        name = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    return name or os.uname().nodename


def project_key(path):
    """The folder name Claude Code keeps a project's conversations under (~/.claude/projects/<key>)."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(path + ".tmp", path)


def _home(path):
    return "~" + path[len(os.path.expanduser("~")):] if path.startswith(os.path.expanduser("~") + "/") else path


def pairs(drive):
    """[(folder on the drive, folder on this Mac, drive-relative name)] for a plugged-in drive: the ones written
    on the drive, plus this Mac's (Settings key "drive_sync"), which are then written on the drive too."""
    found = {}
    for p in (_read(os.path.join(drive, STATE, "sync.json"), {}) or {}).get("pairs", []):
        if isinstance(p, dict) and p.get("drive") and p.get("local"):
            found[p["drive"].strip("/")] = os.path.expanduser(p["local"])
    mine = {}
    for p in meter.load_config().get("drive_sync") or []:
        d = os.path.abspath(os.path.expanduser(p.get("drive") or ""))
        if isinstance(p, dict) and p.get("local") and sessions.drive_of(d) == drive and d != drive:
            mine[os.path.relpath(d, drive)] = os.path.expanduser(p["local"])
    if any(found.get(k) != v for k, v in mine.items()):  # tell other Macs about this Mac's pairs
        found.update(mine)
        try:
            _write(os.path.join(drive, STATE, "sync.json"),
                   {"pairs": [{"drive": k, "local": _home(v)} for k, v in found.items()]})
        except OSError:
            pass
    return [(os.path.join(drive, k), v, k) for k, v in sorted(found.items())]


def portable(path, prs):
    """A folder written so another Mac can find its own copy: inside a pair, under the home folder, or as is."""
    for drive_dir, local, name in prs:
        for root in (local, drive_dir):
            if path == root or path.startswith(root + "/"):
                return {"pair": name, "rel": os.path.relpath(path, root)}
    return {"path": _home(path)}


def local_path(p, prs):
    """This Mac's folder for a portable() one (the Mac's copy of a pair, where you work)."""
    if isinstance(p, dict) and "pair" in p:
        for _, local, name in prs:
            if name == p["pair"]:
                return os.path.normpath(os.path.join(local, p.get("rel") or "."))
        return None
    return os.path.expanduser(p.get("path", "")) if isinstance(p, dict) else None


# ---- files ----------------------------------------------------------------------------------------------------

def _rsync(src, dst, backup, timeout):
    os.makedirs(dst, exist_ok=True)
    cmd = ["/usr/bin/rsync", "-rtu", "--modify-window=2", "--backup",
           "--backup-dir=" + os.path.join(backup, time.strftime("%Y-%m-%d %H.%M"))]
    cmd += [f"--exclude={x}" for x in EXCLUDE] + [src.rstrip("/") + "/", dst.rstrip("/") + "/"]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).returncode in (0, 23, 24)
    except (OSError, subprocess.SubprocessError):  # 23/24: some files skipped (symlinks, vanished files)
        return False


def sync_files(drive, prs, timeout=1800, only_to_drive=False):
    ok = True
    for drive_dir, local, _ in prs:
        if os.path.isdir(local):
            ok &= _rsync(local, drive_dir, os.path.join(drive, STATE, "overwritten"), timeout)
        if not only_to_drive and os.path.isdir(drive_dir):
            ok &= _rsync(drive_dir, local, os.path.join(DATA, "overwritten"), timeout)
    return ok


# ---- conversations --------------------------------------------------------------------------------------------

def _cwd_of(transcript):
    try:
        with open(transcript, encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(40), f):
                if '"cwd"' in line:
                    cwd = json.loads(line).get("cwd")
                    if cwd:
                        return cwd
    except (OSError, ValueError):
        pass
    return None


def export_conversations(drive, prs, grid_ids=()):
    """The conversations in the grid, and recent ones in the pairs' folders, copied onto the drive."""
    out = os.path.join(drive, STATE, "conversations")
    index = _read(os.path.join(out, "index.json"), {}) or {}
    keys = [project_key(r) for d, l, _ in prs for r in (l, d)]
    files = {}
    for f in glob.glob(os.path.join(sessions.PROJECTS, "*", "*.jsonl")):
        sid = os.path.basename(f)[:-6]
        recent = time.time() - os.path.getmtime(f) < KEEP_DAYS * 86400
        if sid in grid_ids or (recent and any(os.path.basename(os.path.dirname(f)).startswith(k) for k in keys)):
            files[sid] = f
    changed = False
    for sid, f in files.items():
        dst = os.path.join(out, sid + ".jsonl")
        if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(f) - 1:
            continue
        cwd = _cwd_of(f)
        if not cwd:
            continue
        os.makedirs(out, exist_ok=True)
        shutil.copy2(f, dst + ".tmp")
        os.replace(dst + ".tmp", dst)
        index[sid] = {"cwd": portable(cwd, prs)}
        changed = True
    if changed:
        _write(os.path.join(out, "index.json"), index)


def import_conversations(drive, prs):
    """Conversations on the drive that are newer than this Mac's copy (or new here), put where Claude Code looks."""
    out = os.path.join(drive, STATE, "conversations")
    running = {s.get("session_id") for s in sessions.running()}
    for sid, info in (_read(os.path.join(out, "index.json"), {}) or {}).items():
        src = os.path.join(out, sid + ".jsonl")
        if sid in running or not os.path.isfile(src):  # never under a Claude that's writing it right now
            continue
        have = glob.glob(os.path.join(sessions.PROJECTS, "*", sid + ".jsonl"))
        if have:
            dst = have[0]
        else:
            cwd = local_path(info.get("cwd"), prs)
            if not cwd:
                continue
            dst = os.path.join(sessions.PROJECTS, project_key(cwd), sid + ".jsonl")
        if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src) - 1:
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst + ".tmp")
        os.replace(dst + ".tmp", dst)


# ---- settings -------------------------------------------------------------------------------------------------

SETTINGS_SEEN = os.path.join(DATA, "drive_settings_synced.json")  # when this Mac last swapped settings with a drive


def _seen(drive, at=None):
    marks = _read(SETTINGS_SEEN, {}) or {}
    if at is not None:
        marks[drive] = at
        _write(SETTINGS_SEEN, marks)
    return marks.get(drive)


def export_settings(drive):
    path = os.path.join(drive, STATE, "settings.json")
    mine = {k: v for k, v in (_read(meter.CONFIG, {}) or {}).items() if k not in MAC_ONLY}
    old = _read(path, {}) or {}
    if old.get("settings") != mine:
        _write(path, {"mac": this_mac(), "saved_at": time.time(), "settings": mine})
    _seen(drive, time.time())


def import_settings(drive):
    """Settings changed on another Mac: take them (not the Mac-only ones), unless you changed this Mac's
    after that. A Mac that never shared settings with this drive takes them."""
    d = _read(os.path.join(drive, STATE, "settings.json"), {}) or {}
    if d.get("mac") == this_mac() or not isinstance(d.get("settings"), dict):
        return False
    seen, at = _seen(drive), d.get("saved_at", 0)
    try:
        local_time = os.path.getmtime(meter.CONFIG)
    except OSError:
        local_time = 0
    if seen is not None and (at <= seen or (local_time > seen + 2 and at <= local_time)):
        return False
    cfg = _read(meter.CONFIG, {}) or {}
    new = {**{k: v for k, v in cfg.items() if k in MAC_ONLY},
           **{k: v for k, v in d["settings"].items() if k not in MAC_ONLY}}
    changed = new != cfg
    if changed:
        meter.save_config(new)
    _seen(drive, time.time())
    return changed


# ---- the grid -------------------------------------------------------------------------------------------------

def export_grid(drive, prs):
    snap = _read(sessions.SAVED)
    if not snap or not snap.get("panes"):
        return
    panes = [{"cwd": portable(p.get("cwd") or "", prs), "session_id": p.get("session_id"),
              "claude": bool(p.get("claude"))} for p in snap["panes"]]
    path = os.path.join(drive, STATE, "grid.json")
    old = _read(path, {}) or {}
    if old.get("panes") != panes or old.get("layout") != snap.get("layout"):
        _write(path, {"mac": this_mac(), "saved_at": time.time(), "layout": snap.get("layout"), "panes": panes})


def grid_to_adopt(drive, prs):
    """The grid another Mac left on the drive, in this Mac's folders, if this Mac hasn't opened it yet."""
    d = _read(os.path.join(drive, STATE, "grid.json"), {}) or {}
    if not d.get("panes") or d.get("mac") == this_mac():
        return None
    if (_read(ADOPTED, {}) or {}).get(drive) == d.get("saved_at"):
        return None
    panes = []
    for p in d["panes"]:
        cwd = local_path(p.get("cwd"), prs) or os.path.expanduser("~")
        panes.append({"cwd": cwd, "session_id": p.get("session_id"), "claude": p.get("claude"), "screen": None})
    return {"mac": d.get("mac"), "saved_at": d.get("saved_at"), "layout": d.get("layout"), "panes": panes}


def adopt_grid(drive, grid):
    """Swap this Mac's grid for the one from the other Mac (once nothing in it is working)."""
    if any(s["status"] == "busy" for s in sessions.running() if s["pane"]):
        return False  # try again at the next check
    pids = [s["pid"] for s in sessions.running() if s["pane"]]
    if sessions.grid_exists():
        sessions.tmux("kill-session", "-t", sessions.GRID)
        sessions._wait_gone(pids, 5)
    _write(sessions.SAVED, {"layout": grid["layout"], "panes": grid["panes"]})
    sessions.restore_grid()
    marks = _read(ADOPTED, {}) or {}
    marks[drive] = grid["saved_at"]
    _write(ADOPTED, marks)
    return True


# ---- when to do what ------------------------------------------------------------------------------------------

def drives():
    """Plugged-in drives this Mac shares folders with."""
    out = []
    try:
        names = os.listdir("/Volumes")
    except OSError:
        return out
    for n in names:
        d = os.path.join("/Volumes", n)
        if sessions.drive_of(d) == d and os.path.ismount(d) and pairs(d):
            out.append(d)
    return out


def save_to(drive, quick=False, grid=True):
    """Everything from this Mac onto the drive (quick: just before an eject, so it can't take long).
    grid=False while another Mac's grid is waiting to be opened here, so it isn't saved over."""
    prs = pairs(drive)
    if not lock.acquire(timeout=20 if quick else -1):  # a long sync is running: an eject can't wait for it
        return False
    try:
        export_settings(drive)
        if grid:
            export_grid(drive, prs)
        grid_ids = {p.get("session_id") for p in (_read(sessions.SAVED, {}) or {}).get("panes", [])}
        export_conversations(drive, prs, grid_ids)
        return sync_files(drive, prs, timeout=15 if quick else 1800, only_to_drive=quick)
    finally:
        lock.release()


def plugged_in(drive, notify=None):
    """The drive just appeared: bring its files, conversations and settings here. Returns the grid another Mac
    left on it, to open here (None if there's none, or it's this Mac's own)."""
    prs = pairs(drive)
    grid = grid_to_adopt(drive, prs)  # before anything of this Mac's is saved there
    with lock:
        sync_files(drive, prs)
        import_conversations(drive, prs)
        if import_settings(drive) and notify:
            notify("Settings", "Took the settings from " + ((_read(os.path.join(drive, STATE, "settings.json"), {})
                                                             or {}).get("mac") or "the other Mac"))
    save_to(drive, grid=grid is None)
    return grid


def watch(notify=None):
    """Runs for as long as the app does (its own thread): syncs plugged-in drives, and opens another Mac's grid."""
    last, waiting = {}, {}
    while True:
        try:
            now = time.time()
            here = drives()
            for d in list(last):
                if d not in here:
                    last.pop(d)
                    waiting.pop(d, None)
            for d in here:
                if d not in last:
                    last[d] = now
                    log(f"plugged in: {d}")
                    grid = plugged_in(d, notify)
                    log(f"synced {d}" + (f"; grid from {grid.get('mac')} waiting to open" if grid else ""))
                    if grid:
                        waiting[d] = grid
                elif now - last[d] >= EVERY:
                    last[d] = now
                    if not save_to(d, grid=d not in waiting):
                        log(f"some files didn't copy to or from {d}")
                if d in waiting and adopt_grid(d, waiting[d]):
                    if notify:
                        notify("Session grid", f"Opened the grid from {waiting[d].get('mac') or 'your other Mac'}")
                    log(f"opened the grid from {waiting[d].get('mac')}")
                    waiting.pop(d)
        except Exception:
            import traceback
            log("error: " + traceback.format_exc().strip().replace("\n", "\n    "))
        time.sleep(10)
