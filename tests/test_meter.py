import json, os, sys, tempfile, threading, time, unittest, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import meter, sessions


def entry(ts, mid, inp=10, out=20, cw=5, cr=100, model="claude-x"):
    return json.dumps({"type": "assistant", "timestamp": ts.isoformat().replace("+00:00", "Z"), "requestId": "r" + mid,
                       "message": {"id": mid, "model": model, "usage": {
                           "input_tokens": inp, "output_tokens": out,
                           "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}}})


class LocalUsage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.dir.name, "p"))
        self.cfg = os.path.join(self.dir.name, "config.json")
        mock.patch.object(meter, "PROJECTS", self.dir.name).start()
        mock.patch.object(meter, "CONFIG", self.cfg).start()
        self.addCleanup(mock.patch.stopall)

    def write(self, lines, name="a.jsonl"):
        with open(os.path.join(self.dir.name, "p", name), "w") as f:
            f.write("\n".join(lines))

    def test_counts_and_dedupes(self):
        now = datetime.now(timezone.utc)
        e = entry(now - timedelta(minutes=5), "m1")
        self.write([e, e, "not json", json.dumps({"type": "user"})])
        s = meter.summarize()
        self.assertEqual(s["block"]["messages"], 1)
        self.assertEqual(s["block"]["counted"], 10 + 20 + 5)   # cache reads excluded
        self.assertEqual(s["block"]["cache_read"], 100)

    def test_old_usage_outside_session_and_week(self):
        old = datetime.now(timezone.utc) - timedelta(days=10)
        self.write([entry(old, "old")])
        s = meter.summarize()
        self.assertEqual(s["block"]["messages"], 0)
        self.assertIsNone(s["block"]["resets_at"])
        self.assertEqual(s["week"]["messages"], 0)
        self.assertEqual(s["all"]["messages"], 1)

    def test_hourly_peak(self):
        now = datetime.now(timezone.utc)
        busy = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
        self.write([entry(busy, "a", inp=1000), entry(busy + timedelta(minutes=1), "b", inp=1000), entry(now - timedelta(days=1), "c")])
        h = meter.summarize()["hourly"]
        self.assertEqual(len(h), 24)
        self.assertEqual(h.index(max(h)), busy.astimezone().hour)

    def test_project_breakdown(self):
        self.write([entry(datetime.now(timezone.utc), "p1")])
        self.assertEqual(len(meter.summarize()["projects"]), 1)
        home = os.path.expanduser("~").replace("/", "-")
        self.assertEqual(meter.project_name(f"/x/{home}-Desktop-App/s.jsonl"), "Desktop/App")

    def test_empty(self):
        s = meter.summarize()
        self.assertEqual(s["all"]["messages"], 0)
        self.assertEqual(len(s["daily"]), 14)

    def test_limits_roundtrip(self):
        meter.save_config({"limit_5h": 5, "limit_week": 6})
        self.assertEqual(meter.load_config()["limit_5h"], 5)


class PlanUsage(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        mock.patch.object(meter, "PLAN_CACHE_FILE", os.path.join(d.name, "pc.json")).start()
        self.addCleanup(mock.patch.stopall)
        meter._plan_cache.update(t=0, until=0, data=None, error=None)

    def fake_resp(self, payload):
        r = mock.MagicMock()
        r.__enter__.return_value = r
        r.read.return_value = json.dumps(payload).encode()
        return r

    def test_parses_limits(self):
        oauth = {"accessToken": "t", "subscriptionType": "pro", "expiresAt": (datetime.now().timestamp() + 3600) * 1000}
        payload = {"five_hour": {"utilization": 12.5, "resets_at": "2030-01-01T00:00:00Z"},
                   "seven_day": {"utilization": 3, "resets_at": None}, "extra": None}
        with mock.patch.object(meter, "read_oauth", return_value=oauth), \
             mock.patch("urllib.request.urlopen", return_value=self.fake_resp(payload)) as uo:
            p = meter.plan_usage()
        self.assertEqual(p["plan"], "pro")
        self.assertEqual([l["key"] for l in p["limits"]], ["five_hour", "seven_day"])
        self.assertEqual(p["limits"][0]["percent"], 12.5)
        self.assertEqual(uo.call_args[0][0].get_header("Authorization"), "Bearer t")

    def test_rate_limit_keeps_last_good_data(self):
        oauth = {"accessToken": "t", "subscriptionType": "pro"}
        payload = {"five_hour": {"utilization": 10, "resets_at": None}}
        with mock.patch.object(meter, "read_oauth", return_value=oauth), \
             mock.patch("urllib.request.urlopen", return_value=self.fake_resp(payload)):
            good = meter.plan_usage()
        meter._plan_cache["until"] = 0  # cache expired
        err = urllib.error.HTTPError("u", 429, "Too Many", {"retry-after": "0"}, None)
        with mock.patch.object(meter, "read_oauth", return_value=oauth), \
             mock.patch("urllib.request.urlopen", side_effect=err) as uo:
            p = meter.plan_usage()
            p2 = meter.plan_usage()      # now backing off: no second request
        self.assertTrue(p["stale"])
        self.assertEqual(p["limits"], good["limits"])
        self.assertEqual(uo.call_count, 1)
        self.assertGreaterEqual(meter._plan_cache["until"] - time.time(), 290)

    def test_rate_limit_without_data_reports_error(self):
        err = urllib.error.HTTPError("u", 429, "Too Many", {}, None)
        with mock.patch.object(meter, "read_oauth", return_value={"accessToken": "t"}), \
             mock.patch("urllib.request.urlopen", side_effect=err):
            self.assertIn("rate-limiting", meter.plan_usage()["error"])

    def test_no_login(self):
        with mock.patch.object(meter, "read_oauth", return_value=None):
            self.assertIn("error", meter.plan_usage())

    def test_expired_login(self):
        with mock.patch.object(meter, "read_oauth", return_value={"accessToken": "t", "expiresAt": 1}):
            self.assertIn("expired", meter.plan_usage()["error"])


class Pace(unittest.TestCase):
    def test_projection(self):
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        resets = (now + timedelta(hours=2.5)).isoformat()      # halfway through a 5h window
        self.assertAlmostEqual(meter.pace(20, resets, 5, now), 40)
        self.assertIsNone(meter.pace(0, resets, 5, now))
        self.assertIsNone(meter.pace(20, None, 5, now))
        self.assertIsNone(meter.pace(5, (now + timedelta(hours=4.95)).isoformat(), 5, now))  # too early to tell
        self.assertIsNone(meter.pace(5, (now - timedelta(hours=1)).isoformat(), 5, now))     # already reset

    def test_links_are_https(self):
        self.assertTrue(all(u.startswith("https://") for u in meter.LINKS.values()))


class Settings(unittest.TestCase):
    def test_sanitize(self):
        s = meter.sanitize({"display": "both", "notify": "yes", "show_icon": False, "limit_5h": -5, "limit_week": 2e6,
                            "refresh_secs": 5, "plan_secs": 5, "notify_done": False, "show_busy": False, "notify_at": [95, 80, 80, 500, "x"], "evil": 1})
        self.assertEqual(s, {"display": "both", "show_icon": False, "limit_week": 2000000, "refresh_secs": 10, "plan_secs": 30, "notify_done": False, "show_busy": False, "notify_at": [80, 95]})
        self.assertEqual(meter.sanitize({"display": "nope"}), {})
        self.assertEqual(meter.sanitize({"yellow_at": 150, "orange_at": 70, "red_at": 0, "x": 1}),
                         {"yellow_at": 100, "red_at": 1})
        self.assertEqual(meter.sanitize({"red_at": True, "orange_at": "80"}), {})

    def test_login_toggle(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(meter, "LAUNCH_PLIST", os.path.join(d, "a.plist")):
            meter.set_login(True)
            self.assertTrue(meter.login_enabled())
            meter.set_login(False)
            self.assertFalse(meter.login_enabled())


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.patches = [mock.patch.object(meter, "CONFIG", os.path.join(cls.tmp.name, "c.json")),
                       mock.patch.object(meter, "LAUNCH_PLIST", os.path.join(cls.tmp.name, "l.plist"))]
        for p in cls.patches:
            p.start()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), meter.H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def post(self, path, d):
        with urllib.request.urlopen(urllib.request.Request(self.base + path, data=json.dumps(d).encode())) as r:
            return json.load(r)

    def test_settings_api(self):
        r = self.post("/api/settings", {"display": "weekly", "notify_at": [70], "login": True, "bad": 1})
        self.assertEqual((r["display"], r["notify_at"], r["login"]), ("weekly", [70], True))
        self.assertNotIn("bad", r)
        self.assertEqual(json.loads(self.get("/api/settings")[1])["display"], "weekly")
        self.assertEqual(self.post("/api/settings", {"login": False})["login"], False)
        self.assertEqual(self.get("/settings")[0], 200)

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return r.status, r.read()

    def test_pages(self):
        for p in ("/glass.css", "/ui.js"):
            self.assertEqual(self.get(p)[0], 200)
        for path in ("/", "/compact"):
            st, body = self.get(path)
            self.assertEqual(st, 200)
            self.assertIn(b"<html", body)

    def test_summary_api(self):
        st, body = self.get("/api/summary")
        self.assertIn("block", json.loads(body))

    def test_action_queue(self):
        meter.actions.clear()
        for name in ("open", "bogus", "credits", "settings", "quit"):
            urllib.request.urlopen(urllib.request.Request(self.base + "/api/action", data=json.dumps({"name": name}).encode()))
        self.assertEqual(meter.actions, ["open", "credits", "settings", "quit"])
        meter.actions.clear()

    def term(self, path, d=None, headers=None):
        req = urllib.request.Request(self.base + path, data=None if d is None else json.dumps(d).encode(),
                                     headers={"X-Claude-Usage": "1", **(headers or {})})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    def test_term_api_needs_app_header(self):
        for path, d in (("/api/terms", None), ("/api/term/new", {"cwd": "/tmp"})):
            req = urllib.request.Request(self.base + path, data=None if d is None else json.dumps(d).encode())
            with self.assertRaises(urllib.error.HTTPError) as c:
                urllib.request.urlopen(req)
            self.assertEqual(c.exception.code, 403)

    def test_foreign_host_rejected(self):  # DNS rebinding: a website pointing its own name at 127.0.0.1
        with self.assertRaises(urllib.error.HTTPError) as c:
            self.term("/api/terms", headers={"Host": "evil.example:8765"})
        self.assertEqual(c.exception.code, 403)

    def test_terms_api(self):
        with mock.patch.object(sessions, "running", return_value=[]), \
             mock.patch.object(sessions, "grid_info", return_value=[]), \
             mock.patch.object(sessions, "new_session", return_value={"ok": True}) as ns:
            r = self.term("/api/terms")
            self.assertEqual((r["running"], r["grid"]), ([], []))
            self.assertTrue(self.term("/api/term/new", {"cwd": "/tmp", "kind": "shell"})["ok"])
            ns.assert_called_once_with("/tmp", False, "shell")
            self.assertEqual(self.term("/api/term/123/focus", {}), {"ok": False})
        with mock.patch.object(sessions, "new_session", side_effect=ValueError("Folder not found")):
            self.assertIn(b"Folder not found", self.post_err("/api/term/new", {"cwd": "/x"}))
        with mock.patch.object(sessions, "pane_action", return_value=True) as pa:
            self.assertTrue(self.term("/api/term/pane/4/close", {})["ok"])
            pa.assert_called_once_with("%4", "close")

    def post_err(self, path, d):
        try:
            self.term(path, d)
        except urllib.error.HTTPError as e:
            return e.read()

    def test_sessions_page(self):
        st, body = self.get("/sessions")
        self.assertIn(b"Move all into grid", body)

    def test_404(self):
        with self.assertRaises(urllib.error.HTTPError) as c:
            self.get("/nope")
        self.assertEqual(c.exception.code, 404)


class Sessions(unittest.TestCase):
    PS = "  3170 04-18:13:55 ttys000  claude\n  4000    00:05 ??       claude\n  4100    01:00 ttys004  zsh\n" \
         "  4200    12:30 ttys005  /Users/x/.local/bin/claude\n"

    def fake_run(self, cmd, **kw):
        out = self.PS if cmd[0] == "ps" else "p3170\nn/Users/x/Projects/site\np4200\nn/Users/x/notes\n"
        return mock.Mock(stdout=out, stderr="")

    def test_finds_interactive_sessions_only(self):
        with mock.patch("subprocess.run", side_effect=self.fake_run):
            r = sessions.running()
        self.assertEqual([(x["pid"], x["name"], x["tty"]) for x in r],
                         [(3170, "site", "/dev/ttys000"), (4200, "notes", "/dev/ttys005")])

    def test_stop_only_signals_claude_sessions(self):
        with mock.patch("subprocess.run", side_effect=self.fake_run), mock.patch("os.kill") as k:
            self.assertFalse(sessions.stop(4100))  # a plain shell
            k.assert_not_called()
            self.assertTrue(sessions.stop(3170))
            k.assert_called_once_with(3170, sessions.signal.SIGTERM)

    def fake_tmux(self, exists):
        calls = []
        def tmux(*args):
            calls.append(args)
            ok = args[0] != "has-session" or exists
            return mock.Mock(returncode=0 if ok else 1, stdout="", stderr="")
        return calls, tmux

    def test_first_pane_creates_styled_grid(self):
        calls, tmux = self.fake_tmux(exists=False)
        with mock.patch.object(sessions, "tmux", side_effect=tmux), \
             mock.patch.object(sessions, "tmux_bin", return_value="/t/tmux"), \
             mock.patch.object(sessions, "claude_bin", return_value="/u/bin/claude"), \
             mock.patch.object(sessions, "show_grid") as show:
            d = tempfile.mkdtemp(prefix="my proj ")
            sessions.new_session(d, cont=True)
            new = next(c for c in calls if c[0] == "new-session")
            self.assertIn(d, new)
            self.assertTrue(new[-1].startswith("/u/bin/claude --continue; exec "))  # back to a shell when Claude exits
            self.assertIn(("set-option", "-t", sessions.GRID, "mouse", "on"), calls)
            show.assert_called_once()
            os.rmdir(d)

    def test_more_panes_split_and_retile(self):
        calls, tmux = self.fake_tmux(exists=True)
        with mock.patch.object(sessions, "tmux", side_effect=tmux), \
             mock.patch.object(sessions, "tmux_bin", return_value="/t/tmux"), \
             mock.patch.object(sessions, "show_grid"):
            sessions.new_session(tempfile.gettempdir(), kind="shell")
            split = next(c for c in calls if c[0] == "split-window")
            self.assertTrue(split[-1].startswith("exec ") and "claude" not in split[-1])  # a plain terminal pane
            self.assertIn(("select-layout", "-t", sessions.GRID, "tiled"), calls)

    def test_new_session_errors(self):
        with self.assertRaises(ValueError):
            sessions.new_session("/no/such/folder")
        with mock.patch.object(sessions, "claude_bin", return_value=None), \
             mock.patch.object(sessions, "grid_exists", return_value=False):
            with self.assertRaises(ValueError):
                sessions.new_session(tempfile.gettempdir())

    def test_move_to_grid_resumes_same_conversation(self):
        s = {"pid": 3170, "pane": None, "session_id": "abc-123", "cwd": tempfile.gettempdir(), "name": "t"}
        with mock.patch.object(sessions, "running", return_value=[s]), \
             mock.patch("os.kill", side_effect=[None, OSError()]) as k, \
             mock.patch.object(sessions, "add_pane") as add, mock.patch("time.sleep"):
            self.assertEqual(sessions.move_to_grid(3170), {"ok": True})
            k.assert_any_call(3170, sessions.signal.SIGTERM)
            add.assert_called_once_with(tempfile.gettempdir(), ["--resume", "abc-123"])

    def test_move_needs_conversation_id(self):
        s = {"pid": 5, "pane": None, "session_id": None, "cwd": "/tmp", "name": "t"}
        with mock.patch.object(sessions, "running", return_value=[s]), mock.patch("os.kill") as k:
            with self.assertRaises(ValueError):
                sessions.move_to_grid(5)
            k.assert_not_called()

    def test_pane_action_only_for_grid_panes(self):
        with mock.patch.object(sessions, "grid_info", return_value=[{"pane": "%3"}]), \
             mock.patch.object(sessions, "tmux") as t, mock.patch.object(sessions, "grid_exists", return_value=True), \
             mock.patch.object(sessions, "save_grid") as save:
            self.assertFalse(sessions.pane_action("%9", "close"))
            t.assert_not_called()
            t.return_value = mock.Mock(returncode=0)
            self.assertTrue(sessions.pane_action("%3", "close"))
            t.assert_any_call("kill-pane", "-t", "%3")
            save.assert_called_once()  # a pane closed on purpose isn't restored later

    def test_restore_rebuilds_saved_grid(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = os.path.join(tmp, "grid.json")
            with open(saved, "w") as f:
                json.dump({"layout": "abcd,220x60,0,0", "panes": [
                    {"cwd": tmp, "session_id": "s-1", "claude": True},
                    {"cwd": tmp, "session_id": "s-open", "claude": True},  # already open elsewhere
                    {"cwd": "/gone/folder", "session_id": None, "claude": False}]}, f)
            with mock.patch.object(sessions, "SAVED", saved), \
                 mock.patch.object(sessions, "tmux_bin", return_value="/t/tmux"), \
                 mock.patch.object(sessions, "grid_exists", return_value=False), \
                 mock.patch.object(sessions, "running", return_value=[{"session_id": "s-open"}]), \
                 mock.patch.object(sessions, "add_pane") as add, mock.patch.object(sessions, "tmux") as t, \
                 mock.patch.object(sessions, "show_grid") as show:
                self.assertEqual(sessions.restore_grid(), 3)
            self.assertEqual(add.call_args_list, [
                mock.call(tmp, ["--resume", "s-1"], show=False),
                mock.call(tmp, [], claude=False, show=False, replay=None),
                mock.call(os.path.expanduser("~"), [], claude=False, show=False, replay=None)])
            t.assert_called_once_with("select-layout", "-t", sessions.GRID, "abcd,220x60,0,0")
            show.assert_called_once()

    def test_save_keeps_last_grid_when_none_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = os.path.join(tmp, "grid.json")
            snap = {"layout": "x", "panes": [{"cwd": tmp, "session_id": "s", "claude": True}]}
            with mock.patch.object(sessions, "SAVED", saved):
                with mock.patch.object(sessions, "snapshot", return_value=snap):
                    sessions.save_grid()
                with mock.patch.object(sessions, "snapshot", return_value=None):
                    sessions.save_grid()  # grid gone (e.g. the Mac restarted): keep what we had
                with open(saved) as f:
                    self.assertEqual(json.load(f), snap)
                sessions.forget_grid()
                self.assertFalse(os.path.exists(saved))

    def test_finished_detects_busy_to_idle(self):
        now = [{"pid": 1, "status": "idle"}, {"pid": 2, "status": "busy"}, {"pid": 3, "status": "idle"}]
        self.assertEqual([s["pid"] for s in sessions.finished({1: "busy", 2: "busy", 3: "idle"}, now)], [1])

    def test_recent_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, "p")
            os.makedirs(proj)
            with open(os.path.join(proj, "a.jsonl"), "w") as f:
                f.write(json.dumps({"type": "summary"}) + "\n" + json.dumps({"cwd": tmp}) + "\n")
            with mock.patch.object(sessions, "PROJECTS", tmp):
                self.assertEqual(sessions.recent_folders(), [tmp])


class AppLoads(unittest.TestCase):
    def test_menubar_imports(self):  # catches crashes at start-up (e.g. clashing Objective-C class names)
        import subprocess
        here = os.path.join(os.path.dirname(__file__), "..")
        r = subprocess.run([sys.executable, "-c", "import menubar"], cwd=here, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])


if __name__ == "__main__":
    unittest.main()
