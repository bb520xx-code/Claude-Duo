"""Claude Duo for Windows: your Claude plan % in the system tray, with the same popup, dashboard, settings and
iPhone app as on the Mac.

The tray icon shows the % used in green, yellow or red (your thresholds from Settings); hover it for the reset
time, click it for the popup. Windows are Microsoft Edge WebView2 (built into Windows 10 and 11) via pywebview.
The pane grid is Mac-only (it needs tmux); on Windows new sessions open in Windows Terminal.

Run:  pythonw winapp.py        (or Claude Duo.exe)
      winapp.py --statusline   (what Claude Code runs for live numbers; see statusline.py)
      winapp.py --selftest OUT (checks the parts that don't need a screen, for the build)
      winapp.py --guitest OUT  (opens the real windows and tray icon, checks them, and quits)"""
import os, sys

if "--statusline" in sys.argv:  # Claude Code's status line: must stay tiny and fast, so nothing else loads
    import statusline
    statusline.main()
    sys.exit(0)

import json, threading, time, urllib.request, webbrowser
from http.server import ThreadingHTTPServer
import meter, sessions
from paths import DATA

COLORS = {"green": (48, 192, 106), "yellow": (230, 194, 0), "red": (240, 69, 58), "grey": (128, 128, 136)}
FONTS = ["segoeuib.ttf", "arialbd.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "DejaVuSans-Bold.ttf"]


def level(pct, cfg):
    if pct is None:
        return "grey"
    return "red" if pct >= cfg.get("red_at", 90) else "yellow" if pct >= cfg.get("yellow_at", 50) else "green"


def icon_image(text, color, size=64):
    """The tray icon: a rounded square in the usage colour with the number on it (Windows scales it down)."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5, fill=COLORS[color] + (255,))
    fs = int(size * (0.62 if len(text) <= 2 else 0.46))
    font = None
    for name in FONTS:
        try:
            font = ImageFont.truetype(name, fs)
            break
        except OSError:
            continue
    font = font or ImageFont.load_default()
    ink = (20, 20, 22, 255) if color == "yellow" else (255, 255, 255, 255)  # dark on yellow reads better
    d.text((size / 2, size / 2), text, font=font, fill=ink, anchor="mm")
    return img


def summary_line(plan, cfg):
    """What the icon shows and its hover text: (number text, % for the colour, tooltip)."""
    limits = plan.get("limits") or []
    by = {l["key"]: l for l in limits}
    if not limits:
        return "?", None, "Claude Duo: " + (plan.get("error") or "no plan data yet")[:90]
    pick = {"session": ["five_hour"], "weekly": ["seven_day"], "both": ["five_hour", "seven_day"]}[cfg.get("display", "session")]
    shown = [by[k] for k in pick if k in by] or limits[:1]
    top = max(l["percent"] for l in shown)
    parts = []
    for l in shown:
        when = ""
        if l.get("resets_at"):
            mins = max(0, int((meter.parse_ts(l["resets_at"]).timestamp() - time.time()) // 60))
            when = f", resets in {mins // 60}h {mins % 60}m" if mins < 1440 else f", resets in {mins // 1440}d"
        parts.append(f"{l['label'].replace(' (all models)', '')}: {l['percent']:.0f}%{when}")
    tip = "Claude Duo\n" + "\n".join(parts) + ("\n(old numbers)" if plan.get("stale") else "")
    return str(min(999, round(top))), top, tip[:127]  # Windows tray tooltips are short


class App:
    def __init__(self, server):
        import pystray, webview
        self.pystray, self.webview = pystray, webview
        self.server = server
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.cfg = meter.load_config()
        self.notified, self.before = set(), None
        self.wake = threading.Event()
        self.live_mtime = None
        meter.on_action = self.drain
        self.main = webview.create_window("Claude Duo", self.base + "/", width=1000, height=700, hidden=True,
                                          min_size=(640, 480))
        self.pop = None  # made on first use: a second window created hidden never finishes starting on Windows
        self.main.events.closing += lambda: self.hide(self.main)  # ✕ hides; Quit really quits
        Item = pystray.MenuItem
        self.icon = pystray.Icon("Claude Duo", icon_image("…", "grey"), "Claude Duo", menu=pystray.Menu(
            Item("Claude Duo", lambda: self.popup(), default=True),  # a click on the icon
            Item("Dashboard", lambda: self.show(self.main, "/")),
            Item("Sessions", lambda: self.show(self.main, "/sessions")),
            Item("Settings", lambda: self.show(self.main, "/settings")),
            Item("Refresh now", lambda: self.wake.set()),
            pystray.Menu.SEPARATOR,
            Item("Quit Claude Duo", self.quit)))

    # --- windows ---
    def hide(self, win):
        win.hide()
        return False  # cancel the close

    def popup(self):
        if self.pop is None:
            self.pop = self.webview.create_window("Claude Duo", self.base + "/compact", width=300, height=640,
                                                  on_top=True, min_size=(280, 400))
            self.pop.events.closing += lambda: self.hide(self.pop)
        else:
            self.show(self.pop)

    def show(self, win, path=None):
        win.show()  # first: a window that was never shown can't load a page yet
        win.restore()
        if path:
            win.load_url(self.base + path)

    def quit(self):
        self.icon.stop()
        try:
            import phone
            phone.stop()
        except Exception:
            pass
        os._exit(0)

    def drain(self):
        """Buttons pressed in the pages (they arrive through the local server)."""
        while meter.actions:
            act = meter.actions.pop(0)
            if act == "open":
                self.show(self.main)
            elif act in ("settings", "sessions"):
                self.show(self.main, "/" + act)
            elif act == "quit":
                self.quit()
            elif isinstance(act, tuple) and act[0] == "choose_folder":
                threading.Thread(target=self.choose_folder, args=act[1:], daemon=True).start()
            elif act == "menubar_settings":
                os.startfile("ms-settings:taskbar")  # where Windows decides which tray icons show
            elif isinstance(act, str) and act in meter.LINKS:
                webbrowser.open(meter.LINKS[act])
                continue  # a link opens the browser; the popup stays
            if act != "diagnose" and self.pop is not None:
                self.pop.hide()

    def choose_folder(self, cont, kind):
        picked = self.main.create_file_dialog(self.webview.FOLDER_DIALOG)
        if picked:
            try:
                sessions.new_session(picked[0], cont, kind)
            except ValueError as e:
                self.icon.notify(str(e), "Claude Duo")

    # --- the numbers ---
    def worker(self):
        while True:
            try:
                self.refresh()
            except Exception as e:
                self.icon.icon = icon_image("!", "grey")
                self.icon.title = f"Claude Duo: {e}"[:127]
            self.wake.wait(self.cfg.get("refresh_secs", 10))
            self.wake.clear()

    def refresh(self):
        self.cfg = meter.load_config()
        plan = meter.plan_usage()
        text, pct, tip = summary_line(plan, self.cfg)
        self.icon.icon = icon_image(text, level(pct, self.cfg) if not plan.get("stale") else "grey")
        self.icon.title = tip
        if self.cfg.get("notify"):
            for l in plan.get("limits") or []:
                for th in self.cfg.get("notify_at", []):
                    key = (l["key"], (l.get("resets_at") or "")[:13], th)
                    if l["percent"] >= th and key not in self.notified:
                        self.notified.add(key)
                        self.icon.notify(f"{l['label']} is at {l['percent']:.0f}% of your limit.", "Claude usage")
        now = sessions.running()
        if self.before is not None and self.cfg.get("notify_done", True):
            for s in sessions.finished(self.before, now):
                self.icon.notify(f"{s['name']}: waiting for you", "Claude finished")
        self.before = {s["pid"]: s.get("status") for s in now}

    def watch_live(self):
        """Claude Code just wrote live numbers: show them now, not at the next refresh."""
        while True:
            try:
                m = os.stat(meter.LIVE_DIR).st_mtime
            except OSError:
                m = 0
            if self.live_mtime is not None and m != self.live_mtime:
                self.wake.set()
            self.live_mtime = m
            time.sleep(1)

    def guitest(self, out):
        """Runs once the windows exist: load each page in the real WebView2 window, read it back, quit."""
        lines = []
        try:
            for path, want in (("/", "Claude Duo"), ("/settings", "Claude Duo"), ("/compact", "Claude Duo")):
                if path == "/compact":
                    self.popup()
                    win = self.pop
                else:
                    win = self.main
                    self.show(win, path)
                time.sleep(5)
                title = win.evaluate_js("document.title") or ""
                errs = win.evaluate_js("JSON.stringify(window.__errs||[])")
                solid = win.evaluate_js("document.documentElement.classList.contains('solid')")
                lines.append(f"{'ok  ' if want in title else 'FAIL'} window {path}: title={title!r} solid_backdrop={solid} errors={errs}")
            lines.append(f"{'ok  ' if self.icon.visible else 'FAIL'} tray icon visible={self.icon.visible}")
            time.sleep(3)  # let the first refresh draw the number
            lines.append(f"ok   tray tooltip={self.icon.title!r}")
        except Exception as e:
            lines.append(f"FAIL {type(e).__name__}: {e}")
        ok = all(not l.startswith("FAIL") for l in lines)
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + f"\n{'PASSED' if ok else 'FAILED'}\n")
        self.icon.stop()
        os._exit(0 if ok else 1)

    def run(self, show=True, guitest=None):
        try:
            import phone
            phone.sync()
        except Exception:
            pass
        self.icon.run_detached()
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.watch_live, daemon=True).start()
        if show and not guitest:
            threading.Timer(1.5, self.popup).start()
        # windows need the main thread; private_mode off keeps each page's remembered tab and settings
        self.webview.start((lambda: self.guitest(guitest)) if guitest else None,
                           private_mode=False, storage_path=os.path.join(DATA, "webview"))


def selftest(out):
    """Everything that doesn't need a screen: the server, every page, the APIs, the iPhone app, the icon."""
    results = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), meter.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(path, headers=None):
        with urllib.request.urlopen(urllib.request.Request(base + path, headers=headers or {}), timeout=30) as r:
            return r.status, r.read()

    for path in ("/", "/compact", "/settings", "/sessions", "/glass.css", "/ui.js"):
        code, body = get(path)
        results.append((path, code == 200 and len(body) > 100))
    for path in ("/api/settings", "/api/plan", "/api/summary"):
        code, body = get(path)
        results.append((path, code == 200 and isinstance(json.loads(body), dict)))
    code, body = get("/api/terms", {"X-Claude-Usage": "1"})
    results.append(("/api/terms", code == 200 and json.loads(body)["tmux"] is False))
    import phone
    psrv = phone.Server(("::", 0), phone.Handler)
    threading.Thread(target=psrv.serve_forever, daemon=True).start()
    with urllib.request.urlopen(f"http://127.0.0.1:{psrv.server_address[1]}/m/{phone.token()}/api", timeout=30) as r:
        results.append(("iphone api", "plan" in json.loads(r.read())))
    results.append(("qr code", (phone.qr_data_url("http://example.test/m/x/") or "").startswith("data:image/png")))
    img = icon_image("47", "green")
    results.append(("tray icon", img.size == (64, 64)))
    img.save(os.path.join(os.path.dirname(os.path.abspath(out)), "tray-icon-preview.png"))
    import pystray, webview  # noqa: F401  (bundled and importable)
    results.append(("pystray + pywebview import", True))
    ok = all(r for _, r in results)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(f"{'ok  ' if r else 'FAIL'} {name}" for name, r in results) + f"\n{'PASSED' if ok else 'FAILED'}\n")
    srv.shutdown()
    psrv.shutdown()
    return ok


def main():
    if "--selftest" in sys.argv:
        out = sys.argv[sys.argv.index("--selftest") + 1] if len(sys.argv) > sys.argv.index("--selftest") + 1 else "selftest.txt"
        try:
            ok = selftest(out)
        except Exception as e:
            import traceback
            with open(out, "w", encoding="utf-8") as f:
                f.write("FAILED\n" + traceback.format_exc())
            ok = False
        os._exit(0 if ok else 1)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", meter.PORT), meter.H)
    except OSError:  # already running: ask that copy to show itself
        if "--background" not in sys.argv:
            try:
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{meter.PORT}/api/action", data=b'{"name": "open"}', method="POST"), timeout=3)
            except OSError:
                pass
        return
    guitest = sys.argv[sys.argv.index("--guitest") + 1] if "--guitest" in sys.argv else None
    App(server).run(show="--background" not in sys.argv, guitest=guitest)


if __name__ == "__main__":
    main()
