#!/usr/bin/env python3
"""Claude usage meter in the macOS menu bar. Run with .venv/bin/python menubar.py"""
import os, subprocess, sys, threading, time, webbrowser
from http.server import ThreadingHTTPServer
import objc, AppKit, Foundation, rumps
import WebKit
import meter, sessions


MIN_WINDOW = (380, 360)  # the main window's smallest size (points); every page is laid out to fit it


def fmt(n):
    return f"{n/1e6:.2f}M" if n >= 1e6 else f"{n/1e3:.1f}k" if n >= 1e3 else str(n)


def mins(iso):
    from datetime import datetime, timezone
    m = max(0, int((datetime.fromisoformat(iso.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds() // 60))
    return f"{m // 1440}d {m % 1440 // 60}h" if m >= 1440 else f"{m // 60}h {m % 60}m"


class ClickThroughWebView(WebKit.WKWebView):
    """Reacts to the first click even when its window isn't focused (the popup otherwise eats that click)."""
    def acceptsFirstMouse_(self, event):
        return True


class SizeHandler(AppKit.NSObject, protocols=[objc.protocolNamed("WKScriptMessageHandler")]):
    """The popup page reports its height, so the popup grows or shrinks to fit the tab (no scrolling)."""
    def initWithApp_(self, app):
        self = objc.super(SizeHandler, self).init()
        self.app = app
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        try:
            self.app.fit_popover(float(message.body()))
        except (TypeError, ValueError):
            pass


class Clicker(AppKit.NSObject):
    def initWithApp_(self, app):
        self = objc.super(Clicker, self).init()
        self.app = app
        return self

    def clicked_(self, sender):
        self.app.on_click()


class Reopener(AppKit.NSObject):
    """Handles the 'reopen' Apple event: the app icon clicked (Spotlight, Finder, Dock) while it is already running."""
    def initWithApp_(self, app):
        self = objc.super(Reopener, self).init()
        self.app = app
        return self

    def handleReopen_withReplyEvent_(self, event, reply):
        self.app.open(None)


class App(rumps.App):
    def __init__(self, server=None):
        super().__init__("✦ …", quit_button=None)
        self.title_color = None
        self.menu = [rumps.MenuItem("Loading…"), None,
                     rumps.MenuItem("Open dashboard", callback=self.open),
                     rumps.MenuItem("Refresh now", callback=lambda _: self.refresh()),
                     rumps.MenuItem("Quit", callback=rumps.quit_application)]
        server = server or ThreadingHTTPServer(("127.0.0.1", meter.PORT), meter.H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.cfg = meter.load_config()
        self.ticks = 0
        self.notified = set()
        self.win = None
        self.pop = None
        self.monitor = None
        self.hooked = False
        self.pending = None
        self.live_mtime = None  # when Claude Code last wrote live numbers (see statusline.py)
        self.cfg_mtime = None  # when the settings last changed, to switch light / dark right away
        self.wake = threading.Event()
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.grid_keeper, daemon=True).start()
        import drivesync  # folders, settings and the grid shared with a drive (and through it, your other Macs)
        threading.Thread(target=drivesync.watch, args=(banner,), daemon=True).start()
        # a button press runs right away on the main thread, instead of waiting for the next timer tick
        meter.on_action = lambda: Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(self.drain)
        self.apply_timer = rumps.Timer(self.apply, 1)  # applies fetched data on the main thread
        self.apply_timer.start()

    def web(self, path, w, h, sizer=None):
        cfg = WebKit.WKWebViewConfiguration.alloc().init()
        if sizer is not None:
            cfg.userContentController().addScriptMessageHandler_name_(sizer, "size")
        view = ClickThroughWebView.alloc().initWithFrame_configuration_(Foundation.NSMakeRect(0, 0, w, h), cfg)
        view.setValue_forKey_(False, "drawsBackground")  # page is transparent so the glass shows through
        view.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        view.loadRequest_(Foundation.NSURLRequest.requestWithURL_(
            Foundation.NSURL.URLWithString_(f"http://localhost:{meter.PORT}{path}")))
        return view

    def glass(self, view, w, h, radius):
        """Wrap a view in Liquid Glass (NSGlassEffectView, macOS 26+); older macOS gets a blur material."""
        frame = Foundation.NSMakeRect(0, 0, w, h)
        if hasattr(AppKit, "NSGlassEffectView"):
            g = AppKit.NSGlassEffectView.alloc().initWithFrame_(frame)
            g.setCornerRadius_(radius)
        else:
            g = AppKit.NSVisualEffectView.alloc().initWithFrame_(frame)
            g.setMaterial_(AppKit.NSVisualEffectMaterialHUDWindow)
            g.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
            g.setState_(AppKit.NSVisualEffectStateActive)
        g.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        if hasattr(g, "setContentView_"):
            g.setContentView_(view)
        else:
            view.setFrame_(frame)
            g.addSubview_(view)
        return g

    def make_window(self, path, w, h, title):
        """Native Liquid Glass window hosting the dashboard (main thread only)."""
        win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            Foundation.NSMakeRect(0, 0, w, h),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable | AppKit.NSWindowStyleMaskResizable
            | AppKit.NSWindowStyleMaskMiniaturizable | AppKit.NSWindowStyleMaskFullSizeContentView,
            AppKit.NSBackingStoreBuffered, False)
        win.setReleasedWhenClosed_(False)
        win.setContentMinSize_(Foundation.NSMakeSize(*MIN_WINDOW))  # narrower than this, the pages can't fit
        win.setTitle_(title)
        win.setTitlebarAppearsTransparent_(True)
        win.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        win.setOpaque_(False)
        win.setBackgroundColor_(AppKit.NSColor.clearColor())
        self.last_view = self.web(path, w, h)
        win.setContentView_(self.glass(self.last_view, w, h, 0))
        win.center()
        # like any Mac app: the window lives on the desktop (Space) it was opened on and doesn't float over others
        win.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorManaged)
        self.bring_front(win)
        return win

    def bring_front(self, win):
        # menu-bar-only (accessory) apps can't take focus on recent macOS, so act as a regular app while the window is open
        AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
        AppKit.NSApp.activate() if hasattr(AppKit.NSApp, "activate") else AppKit.NSApp.activateIgnoringOtherApps_(True)
        win.makeKeyAndOrderFront_(None)
        win.orderFrontRegardless()
        if os.environ.get("CLAUDE_USAGE_DEBUG"):
            print("window", win.isVisible(), "activeSpace", win.isOnActiveSpace(), "occl", win.occlusionState(),
                  "appActive", AppKit.NSApp.isActive(), "hidden", AppKit.NSApp.isHidden(), "alpha", win.alphaValue(),
                  "frame", win.frame(), "screen", win.screen(), flush=True)

    def open(self, _=None, path=None):
        """The main window, with Usage and Sessions tabs. `path` picks the tab; None keeps the one showing."""
        if self.win is not None and self.win.isVisible():
            if path:
                self.win_view.loadRequest_(Foundation.NSURLRequest.requestWithURL_(
                    Foundation.NSURL.URLWithString_(f"http://localhost:{meter.PORT}{path}")))
            self.bring_front(self.win)
        else:
            self.win = self.make_window(path or "/", 980, 660, "Claude Duo")
            self.win_view = self.last_view

    def open_settings(self, _=None):
        self.open(path="/settings")

    def open_sessions(self, _=None):
        self.open(path="/sessions")

    def choose_folder(self, cont, kind):
        panel = AppKit.NSOpenPanel.openPanel()
        panel.setCanChooseDirectories_(True)
        panel.setCanChooseFiles_(False)
        panel.setPrompt_("Open terminal here" if kind == "shell" else "Start Claude here")
        panel.setMessage_("Choose the folder for the new pane")
        if panel.runModal() == AppKit.NSModalResponseOK:
            try:
                sessions.new_session(panel.URL().path(), cont, kind)
            except ValueError as e:
                rumps.alert("Couldn't start Claude", str(e))

    def edit_menu(self):
        """Main menu for when a window is open: without an Edit menu, ⌘C / ⌘V don't work in the web views."""
        main = AppKit.NSMenu.alloc().init()
        for title, items in (("Claude Duo", [("Close Window", "performClose:", "w"), None,
                                               ("Quit Claude Duo", "terminate:", "q")]),
                             ("Edit", [("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), None, ("Cut", "cut:", "x"),
                                       ("Copy", "copy:", "c"), ("Paste", "paste:", "v"),
                                       ("Select All", "selectAll:", "a")])):
            sub = AppKit.NSMenu.alloc().initWithTitle_(title)
            for it in items:
                sub.addItem_(AppKit.NSMenuItem.separatorItem() if it is None
                             else AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(*it))
            top = AppKit.NSMenuItem.alloc().init()
            top.setSubmenu_(sub)
            main.addItem_(top)
        AppKit.NSApp.setMainMenu_(main)

    def check_menubar(self):
        """macOS can refuse to show the icon (System Settings > Menu Bar); the windows then show a banner."""
        w = self._nsapp.nsstatusitem.button().window()
        meter.state["menubar_hidden"] = w is None or w.frame().size.height < 1

    def show_popover(self):
        """Native popover hanging off the menu bar item (closes when you click elsewhere)."""
        if self.pop is None:
            size = Foundation.NSMakeSize(260, 545)
            self.sizer = SizeHandler.alloc().initWithApp_(self)
            self.pop_view = self.web("/compact", size.width, size.height, self.sizer)
            vc = AppKit.NSViewController.alloc().init()
            vc.setView_(self.glass(self.pop_view, size.width, size.height, 22))
            self.pop = AppKit.NSPopover.alloc().init()
            self.pop.setContentViewController_(vc)
            self.pop.setContentSize_(size)
            self.pop.setBehavior_(AppKit.NSPopoverBehaviorSemitransient)  # stays open until an outside click (monitor below)
        else:  # already loaded: just fetch fresh numbers instead of reloading the page (opens instantly)
            self.pop_view.evaluateJavaScript_completionHandler_("refresh()", None)
        btn = self._nsapp.nsstatusitem.button()
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        if self.monitor is None:  # clicks outside the app close the popover
            self.monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                AppKit.NSEventMaskLeftMouseDown | AppKit.NSEventMaskRightMouseDown,
                lambda ev: self.pop.performClose_(None) if self.pop.isShown() else None)
        self.pop.showRelativeToRect_ofView_preferredEdge_(btn.bounds(), btn, AppKit.NSMinYEdge)

    def fit_popover(self, height):
        """Size the popup to its content, but never taller than the screen below the menu bar."""
        if self.pop is None:
            return
        screen = AppKit.NSScreen.mainScreen().visibleFrame().size.height
        h = max(300, min(screen - 40, round(height)))
        if abs(self.pop.contentSize().height - h) >= 2:
            self.pop.setContentSize_(Foundation.NSMakeSize(260, h))

    def hook(self):
        # left click -> grid panel; right click -> menu (refresh / full dashboard / quit)
        AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # no Dock icon
        item = self._nsapp.nsstatusitem
        # a unique name, so macOS doesn't reuse a hidden state saved for a generic "Item-0"
        item.setAutosaveName_("ClaudeUsageMeter")
        item.setVisible_(True)
        self.clicker = Clicker.alloc().initWithApp_(self)
        item.setMenu_(None)
        btn = item.button()
        btn.setTarget_(self.clicker)
        btn.setAction_("clicked:")
        btn.sendActionOn_(AppKit.NSEventMaskLeftMouseUp | AppKit.NSEventMaskRightMouseUp)
        self.reopener = Reopener.alloc().initWithApp_(self)
        Foundation.NSAppleEventManager.sharedAppleEventManager().setEventHandler_andSelector_forEventClass_andEventID_(
            self.reopener, "handleReopen:withReplyEvent:", 0x61657674, 0x72617070)  # 'aevt' / 'rapp'
        self.edit_menu()
        try:  # ejecting a drive: park its grid sessions first, so they don't block the eject
            import drives
            drives.on_eject(self.drive_ejecting)
        except Exception:
            pass
        self.hooked = True
        # started by hand (not at login): show the dashboard, or the sessions window for the Claude Sessions app
        self.show_on_start = "sessions" if "--sessions" in sys.argv else "--background" not in sys.argv

    def on_click(self):
        try:
            ev = AppKit.NSApp.currentEvent()  # can be None, e.g. for an accessibility "press"
            right = ev is not None and (ev.type() == AppKit.NSEventTypeRightMouseUp
                                        or ev.modifierFlags() & AppKit.NSEventModifierFlagControl)
            if right:
                self._nsapp.nsstatusitem.popUpStatusItemMenu_(self.menu._menu)
            elif self.pop is not None and self.pop.isShown():
                self.pop.performClose_(None)
            else:
                self.show_popover()
        except Exception:
            import traceback
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "error.log"), "a") as f:
                traceback.print_exc(file=f)

    def refresh(self):
        self.wake.set()

    def drive_ejecting(self, volume):
        """macOS is about to eject a drive (main thread; the eject waits for this)."""
        try:  # the last changes onto the drive first, so another Mac carries on from here
            import drivesync
            if drivesync.pairs(volume):
                drivesync.save_to(volume, quick=True)
        except Exception:
            pass
        try:
            n = sessions.release_drive(volume)
        except Exception:
            return
        if n:
            banner(f"{os.path.basename(volume)} ejected", paused(n))

    def grid_keeper(self):
        """Saves the session grid every few seconds and rebuilds it when the app starts after a restart or crash.
        If the grid disappears while the app is running, you closed it yourself, so it isn't brought back.
        A drive pulled out without ejecting: its sessions wait for it, and resume by themselves when it's back."""
        import time
        had, before = False, None
        try:
            sessions.restore_grid()
        except Exception:
            pass
        while True:
            try:
                for drive, n in sessions.park_unplugged().items():
                    banner(f"{os.path.basename(drive)} unplugged", paused(n))
                now = sessions.grid_exists()
                if now:
                    sessions.save_grid()
                elif had:
                    sessions.forget_grid()
                had = now
                running = sessions.running()
                if before is not None and self.cfg.get("notify_done", True):
                    for s in sessions.finished(before, running):
                        banner("Claude finished", f"{s['name']}: waiting for you")
                before = {s["pid"]: s["status"] for s in running}
            except Exception:
                pass
            time.sleep(5)

    def worker(self):
        # keychain/network calls can block, so never do them on the UI thread
        while True:
            try:
                self.pending = self.fetch()
                self.notify(self.pending["limits"])
            except Exception as e:
                self.pending = {"title": "✦ ?", "head": f"Error: {e}", "lines": [], "limits": []}
            self.wake.wait(self.cfg.get("refresh_secs", 10))
            self.wake.clear()

    def fetch(self):
        self.cfg = meter.load_config()  # settings window may have changed it
        plan = meter.plan_usage()
        lines, limits = [], plan.get("limits", [])
        if limits:
            for l in limits:
                line = f"{l['label']}: {l['percent']:.0f}%" + (f"  (resets {mins(l['resets_at'])})" if l["resets_at"] else "")
                if l.get("pace"):
                    line += f" · pace {l['pace']:.0f}%"
                lines.append(line)
            by = {l["key"]: l["percent"] for l in limits}
            s5, wk = by.get("five_hour"), by.get("seven_day")
            shown = {"session": [s5], "weekly": [wk], "both": [s5, wk]}[self.cfg.get("display", "session")]
            shown = [x for x in shown if x is not None] or [s5 if s5 is not None else wk]
            title = ("✦ " if self.cfg.get("show_icon", True) else "") + (" · ".join(f"{x:.0f}%" for x in shown) if shown[0] is not None else "Claude")
            when = time.strftime("%H:%M:%S", time.localtime(plan["updated"])) if plan.get("updated") else "?"
            live = any(l.get("source") == "claude_code" for l in limits)
            head = f"Plan: {(plan.get('plan') or 'unknown').upper()} · updated {when}" + (" (old)" if plan.get("stale") else "")
            tip = f"Updated {when}, " + ("live from Claude Code" if live else "from Anthropic") \
                + (f"\n{plan['note']}" if plan.get("stale") and plan.get("note") else "")
        else:  # not logged in / endpoint down: fall back to local token counts vs your own limits
            s = meter.summarize()
            local = s['block']['counted'] / s['limits']['limit_5h'] * 100
            title = f"✦ {local:.0f}%"
            head = tip = plan["error"]
            lines.append(f"5h session: {fmt(s['block']['counted'])} tokens (local)")
            lines.append(f"7 days: {fmt(s['week']['counted'])} tokens (local)")
        top = max([x for x in shown if x is not None], default=None) if limits else local
        if self.cfg.get("show_busy", True) and any(s["status"] == "busy" for s in sessions.running()):
            title += " ●"  # Claude is working somewhere
        return {"title": title, "head": head, "lines": lines, "limits": limits, "top": top, "tip": tip}

    def color_title(self, pct):
        """Green, then yellow, then red, from the percentages chosen in Settings (default 50 / 90)."""
        if pct is None:
            return
        c = self.cfg
        self.title_color = ("red" if pct >= c.get("red_at", 90) else
                            "yellow" if pct >= c.get("yellow_at", 50) else "green")
        color = {"red": AppKit.NSColor.systemRedColor,
                 "yellow": AppKit.NSColor.systemYellowColor,
                 "green": AppKit.NSColor.systemGreenColor}[self.title_color]()
        btn = self._nsapp.nsstatusitem.button()
        btn.setAttributedTitle_(Foundation.NSAttributedString.alloc().initWithString_attributes_(
            btn.title(), {AppKit.NSForegroundColorAttributeName: color,  # whole title: ✦, %, and the ●
                          AppKit.NSFontAttributeName: AppKit.NSFont.menuBarFontOfSize_(0)}))

    def notify(self, limits):
        """macOS notification when a limit crosses a threshold (once per threshold per reset window)."""
        if not self.cfg.get("notify"):
            return
        for l in limits:
            for th in self.cfg.get("notify_at", []):
                key = (l["key"], (l.get("resets_at") or "")[:13], th)
                if l["percent"] >= th and key not in self.notified:
                    self.notified.add(key)
                    msg = f"{l['label']} is at {l['percent']:.0f}% of your limit."
                    banner("Claude usage", msg)

    # --- settings (right-click menu) ---
    def set_cfg(self, key, value):
        self.cfg = meter.load_config()
        self.cfg[key] = value
        meter.save_config(self.cfg)
        self.refresh()

    def toggle_login(self, _):
        meter.set_login(not meter.login_enabled())

    def link(self, name):
        return lambda _: webbrowser.open(meter.LINKS[name])

    def build_menu(self, head, lines):
        disp = rumps.MenuItem("Menu bar shows")
        for key, label in (("session", "Session %"), ("weekly", "Weekly %"), ("both", "Both")):
            it = rumps.MenuItem(label, callback=lambda _, k=key: self.set_cfg("display", k))
            it.state = self.cfg.get("display", "session") == key
            disp.add(it)
        notif = rumps.MenuItem("Notify at 80% and 95%", callback=lambda _: self.set_cfg("notify", not self.cfg.get("notify")))
        notif.state = bool(self.cfg.get("notify"))
        login = rumps.MenuItem("Launch at login", callback=self.toggle_login)
        login.state = meter.login_enabled()
        return ([rumps.MenuItem(head)] + [rumps.MenuItem("• " + t) for t in lines] + [
            None,
            rumps.MenuItem("＋ Add usage credits…", callback=self.link("credits")),
            rumps.MenuItem("Upgrade plan…", callback=self.link("upgrade")),
            rumps.MenuItem("Add API credits…", callback=self.link("api_credits")),
            None, disp, notif, login,
            rumps.MenuItem("Settings…", callback=self.open_settings), None,
            rumps.MenuItem("Claude sessions…", callback=self.open_sessions),
            rumps.MenuItem("Open dashboard", callback=self.open),
            rumps.MenuItem("Refresh now", callback=lambda _: self.refresh()),
            rumps.MenuItem("Quit", callback=rumps.quit_application)])

    def drain(self):
        """Carry out button presses from the pages (on the main thread)."""
        while meter.actions:
            act = meter.actions.pop(0)
            if self.pop is not None and self.pop.isShown() and act not in ("diagnose",) \
                    and not (isinstance(act, str) and act in meter.LINKS):
                self.pop.performClose_(None)  # a window is about to open: get the popup out of the way
            if act == "open":
                self.open(path="/")
            elif act == "settings":
                self.open_settings()
            elif act == "sessions":
                self.open_sessions()
            elif isinstance(act, tuple) and act[0] == "choose_folder":
                self.choose_folder(act[1], act[2])
            elif act == "menubar_settings":
                subprocess.run(["open", "x-apple.systempreferences:com.apple.ControlCenter-Settings.extension"])
            elif act == "quit":
                rumps.quit_application()
            elif act == "diagnose":  # where the menu bar item really is, for troubleshooting
                it = self._nsapp.nsstatusitem
                w = it.button().window()
                with open(os.path.join(meter.DATA, "status.log"), "w") as f:
                    f.write(f"visible={it.isVisible()} title={it.button().title()!r} length={it.length()}\n"
                            f"frame={w.frame() if w else None} occlusion={w.occlusionState() if w else None} "
                            f"screen={w.screen().frame() if w and w.screen() else None}\n"
                            f"menubar_visible={AppKit.NSMenu.menuBarVisible()} policy={AppKit.NSApp.activationPolicy()}\n"
                            f"color={self.title_color}\n"
                            f"appearance={AppKit.NSApp.effectiveAppearance().name()}\n")
            elif isinstance(act, str) and act in meter.LINKS:
                webbrowser.open(meter.LINKS[act])

    def apply_theme(self):
        """Settings → Appearance: every window (glass and pages) light, dark, or following the Mac."""
        try:
            m = os.stat(meter.CONFIG).st_mtime
        except OSError:
            m = 0
        if m == self.cfg_mtime:
            return
        self.cfg_mtime = m
        name = {"light": AppKit.NSAppearanceNameAqua, "dark": AppKit.NSAppearanceNameDarkAqua}.get(
            meter.load_config().get("theme"))
        AppKit.NSApp.setAppearance_(AppKit.NSAppearance.appearanceNamed_(name) if name else None)

    def apply(self, _):
        self.drain()
        if not self.hooked:
            self.hook()
            return
        if getattr(self, "show_on_start", False):  # a tick after hooking, so the mode switch has settled
            which, self.show_on_start = self.show_on_start, False
            self.open_sessions() if which == "sessions" else self.open(None)
        self.ticks += 1
        self.apply_theme()
        try:  # Claude Code just wrote new live numbers: show them now, not at the next refresh
            m = os.stat(meter.LIVE_DIR).st_mtime
        except OSError:
            m = 0
        if m != self.live_mtime:
            if self.live_mtime is not None:
                self.wake.set()
            self.live_mtime = m
        if self.ticks in (3, 10) or self.ticks % 60 == 0:
            self.check_menubar()
        if not any(w is not None and w.isVisible() for w in (self.win,)) \
                and AppKit.NSApp.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular:
            AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # dashboard closed: back to menu bar only
        if not self.pending:
            return
        p, self.pending = self.pending, None
        self.title = p["title"]
        self.color_title(p.get("top"))
        self._nsapp.nsstatusitem.button().setToolTip_(p.get("tip") or "Claude Duo")
        self.menu.clear()
        self.menu.update(self.build_menu(p["head"], p["lines"]))


def banner(title, msg):
    """A macOS notification (text passed as arguments, so quotes in folder names can't break the script)."""
    subprocess.run(["osascript", "-e", "on run a", "-e",
                    'display notification (item 2 of a) with title (item 1 of a) sound name "Glass"', "-e", "end run",
                    title, msg], capture_output=True)


def paused(n):
    return (f"{n} session{'s' if n > 1 else ''} paused. "
            f"{'They pick' if n > 1 else 'It picks'} up again as soon as you plug it back in.")


def mac_copy(bundle):
    """When this app was opened from an external drive: the same app installed on the Mac itself, if there is
    one. Running that copy keeps Claude Duo going when the drive is ejected or pulled out."""
    if not sessions.drive_of(bundle):
        return None
    me = AppKit.NSBundle.mainBundle().bundleIdentifier()
    for path in meter.APP_PATHS:
        b = AppKit.NSBundle.bundleWithPath_(path)
        if path != bundle and b is not None and b.bundleIdentifier() == me and not sessions.drive_of(path):
            return path
    return None


def main():
    bundle = AppKit.NSBundle.mainBundle().bundlePath()
    other = mac_copy(bundle)
    if other:  # opened from a drive: run the Mac's copy instead, so the drive can come and go
        subprocess.Popen(["/usr/bin/open", other, "--args", *sys.argv[1:]])
        return
    try:
        server = ThreadingHTTPServer(("127.0.0.1", meter.PORT), meter.H)
    except OSError:  # already running (one icon only): ask that copy to show its dashboard instead
        if "--background" not in sys.argv or "--sessions" in sys.argv:
            import urllib.request
            name = b"sessions" if "--sessions" in sys.argv else b"open"
            try:
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{meter.PORT}/api/action", data=b'{"name": "' + name + b'"}', method="POST"),
                    timeout=3)
            except OSError:
                pass
        return
    try:  # the iPhone app, if it's turned on in Settings
        import phone
        phone.sync()
    except Exception:
        pass
    if bundle.endswith(".app"):  # wherever the app lives, so "Launch at login" opens this copy
        meter.state["app_path"] = bundle
        meter.fix_login(bundle)  # the app was moved: Launch at login follows it
    App(server).run()


if __name__ == "__main__":
    main()
