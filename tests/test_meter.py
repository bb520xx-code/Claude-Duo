import json, os, re, sys, tempfile, threading, time, unittest, urllib.error, urllib.request
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
        mock.patch.object(meter, "LIVE_DIR", os.path.join(d.name, "live")).start()
        mock.patch.object(meter, "CONFIG", os.path.join(d.name, "c.json")).start()
        mock.patch.object(meter, "CLAUDE_SETTINGS", os.path.join(d.name, "cs.json")).start()
        self.addCleanup(mock.patch.stopall)
        self.dir = d.name
        meter._plan_cache.update(t=0, next=0, data=None, error=None, fails=0, plan=None)

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
        self.assertNotIn("stale", good)
        meter._plan_cache["t"] -= 600  # that answer is now 10 minutes old
        err = urllib.error.HTTPError("u", 429, "Too Many", {"retry-after": "0"}, None)
        with mock.patch.object(meter, "read_oauth", return_value=oauth), \
             mock.patch("urllib.request.urlopen", side_effect=err) as uo:
            p = meter.plan_usage()
            meter.plan_usage()           # now backing off: no second request
        self.assertTrue(p["stale"])      # and it says so, instead of passing old numbers off as current
        self.assertIn("rate-limiting", p["note"])
        self.assertEqual([l["percent"] for l in p["limits"]], [10.0])
        self.assertEqual(uo.call_count, 1)
        self.assertAlmostEqual(meter._plan_cache["next"] - time.time(), 120, delta=5)

    def test_backoff_doubles(self):
        err = urllib.error.HTTPError("u", 429, "Too Many", {"retry-after": "0"}, None)
        waits = []
        with mock.patch.object(meter, "read_oauth", return_value={"accessToken": "t"}), \
             mock.patch("urllib.request.urlopen", side_effect=err):
            for _ in range(5):
                meter._plan_cache["next"] = 0
                meter.plan_usage()
                waits.append(round(meter._plan_cache["next"] - time.time(), -1))
        self.assertEqual(waits, [120, 240, 480, 900, 900])

    def test_retry_after_is_respected(self):
        err = urllib.error.HTTPError("u", 429, "Too Many", {"retry-after": "42"}, None)
        with mock.patch.object(meter, "read_oauth", return_value={"accessToken": "t"}), \
             mock.patch("urllib.request.urlopen", side_effect=err):
            meter.plan_usage()
        self.assertAlmostEqual(meter._plan_cache["next"] - time.time(), 42, delta=3)

    def write_live(self, sid, pct, resets, t=None, week=None):
        os.makedirs(os.path.join(self.dir, "live"), exist_ok=True)
        rl = {"five_hour": {"used_percentage": pct, "resets_at": resets}}
        if week is not None:
            rl["seven_day"] = {"used_percentage": week, "resets_at": resets + 86400}
        with open(os.path.join(self.dir, "live", sid + ".json"), "w") as f:
            json.dump({"t": t or time.time(), "fp": "x", "rate_limits": rl}, f)

    def test_live_numbers_win_and_skip_the_request(self):
        """The bug: Anthropic said 0 % at the start of the session, then rate-limited us while you used 7 %."""
        end = time.time() + 4 * 3600
        meter._plan_cache.update(t=time.time() - 100, data={"plan": "pro", "limits": [
            {"key": "five_hour", "label": "5-hour session", "percent": 0.0,
             "resets_at": datetime.fromtimestamp(end, timezone.utc).isoformat()}]})
        self.write_live("a", 7.0, int(end), week=15.0)
        with mock.patch("urllib.request.urlopen") as uo:
            p = meter.plan_usage()
        uo.assert_not_called()           # live numbers are fresh, so Anthropic's rate limit is left alone
        by = {l["key"]: l for l in p["limits"]}
        self.assertEqual((by["five_hour"]["percent"], by["seven_day"]["percent"]), (7.0, 15.0))
        self.assertEqual(by["five_hour"]["source"], "claude_code")
        self.assertTrue(p["live"])
        self.assertNotIn("stale", p)

    def test_live_reset_time_is_unix_seconds(self):
        end = int(time.time()) + 3600
        self.write_live("a", 3.0, end)
        l = meter.live_readings()[0]
        self.assertEqual(meter.parse_ts(l["resets_at"]).timestamp(), end)

    def test_old_live_files_are_removed(self):
        self.write_live("old", 50.0, int(time.time()) - 86400, t=time.time() - 9 * 86400)
        self.assertEqual(meter.live_readings(), [])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "live", "old.json")))


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


class Merge(unittest.TestCase):
    def r(self, pct, end, t, key="five_hour", source="claude_code"):
        iso = datetime.fromtimestamp(end, timezone.utc).isoformat() if end else None
        return {"key": key, "percent": pct, "resets_at": iso, "t": t, "source": source}

    def test_highest_reading_in_the_window(self):
        now, end = time.time(), time.time() + 3600
        # an idle session redrawn just now still remembers 3 %; the busy one saw 7 % a bit earlier
        m = meter.merge_limits([self.r(3, end, now), self.r(7, end + 0.4, now - 30), self.r(0, end, now - 600)], now)
        self.assertEqual(m[0]["percent"], 7)
        self.assertEqual(m[0]["t"], now)

    def test_reset_window_is_zero(self):
        now = time.time()
        m = meter.merge_limits([self.r(88, now - 60, now - 300)], now)
        self.assertEqual((m[0]["percent"], m[0]["resets_at"]), (0.0, None))

    def test_new_window_beats_old_one(self):
        now = time.time()
        m = meter.merge_limits([self.r(90, now - 60, now - 400), self.r(4, now + 5 * 3600, now - 10)], now)
        self.assertEqual(m[0]["percent"], 4)

    def test_undated_reading_after_reset(self):
        now = time.time()
        m = meter.merge_limits([self.r(60, now - 600, now - 900), self.r(2, None, now - 5, source="anthropic")], now)
        self.assertEqual(m[0]["percent"], 2)

    def test_order_and_labels(self):
        now = time.time()
        m = meter.merge_limits([self.r(1, now + 99, now, "seven_day_opus"), self.r(2, now + 99, now, "seven_day"),
                                self.r(3, now + 99, now)], now)
        self.assertEqual([x["key"] for x in m], ["five_hour", "seven_day", "seven_day_opus"])
        self.assertEqual(m[2]["label"], "Weekly (Opus)")


class StatusLine(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name
        self.script = os.path.join(os.path.dirname(__file__), "..", "statusline.py")

    def run_line(self, d):
        import subprocess
        raw = d if isinstance(d, str) else json.dumps(d)
        r = subprocess.run([sys.executable, "-I", self.script], input=raw, capture_output=True, text=True, encoding="utf-8",
                           timeout=20, env={**os.environ, "CLAUDE_DUO_DIR": self.dir})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def snapshot(self, api_ms=1000, pct=7.0):
        return {"session_id": "abc-123", "cost": {"total_api_duration_ms": api_ms},
                "context_window": {"total_input_tokens": 10, "total_output_tokens": 5},
                "rate_limits": {"five_hour": {"used_percentage": pct, "resets_at": 1893456000},
                                "seven_day": {"used_percentage": 15.0, "resets_at": 1893800000}}}

    def saved(self):
        with open(os.path.join(self.dir, "live", "abc-123.json")) as f:
            return json.load(f)

    def test_saves_live_numbers_and_prints_usage(self):
        out = re.sub(r"\x1b\[[0-9;]*m", "", self.run_line(self.snapshot()))  # without the colours
        self.assertIn("7% session", out)
        self.assertIn("15% week", out)
        self.assertEqual(self.saved()["rate_limits"]["five_hour"]["used_percentage"], 7.0)

    def test_redraw_is_not_news(self):
        self.run_line(self.snapshot())
        t1 = self.saved()["t"]
        time.sleep(0.05)
        self.run_line(self.snapshot())              # same reply, just redrawn
        self.assertEqual(self.saved()["t"], t1)
        self.run_line(self.snapshot(api_ms=2500))   # a new reply from Anthropic
        self.assertGreater(self.saved()["t"], t1)

    def test_keeps_your_status_line(self):
        with open(os.path.join(self.dir, "config.json"), "w") as f:
            json.dump({"statusline_prev": {"type": "command", "command": "cat > /dev/null; echo mine"}}, f)
        self.assertEqual(self.run_line(self.snapshot()).strip(), "mine")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "live", "abc-123.json")))

    def test_bad_input_never_breaks_claude(self):
        self.run_line("not json")
        self.run_line({"rate_limits": None})
        self.assertFalse(os.path.exists(os.path.join(self.dir, "live")))


class LiveFeedSwitch(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.cs = os.path.join(d.name, "settings.json")
        for name, val in (("CLAUDE_SETTINGS", self.cs), ("CONFIG", os.path.join(d.name, "c.json"))):
            mock.patch.object(meter, name, val).start()
        self.addCleanup(mock.patch.stopall)

    def read(self):
        with open(self.cs) as f:
            return json.load(f)

    def test_on_and_off_keeps_your_status_line(self):
        mine = {"type": "command", "command": "~/.claude/mine.sh", "padding": 1}
        with open(self.cs, "w") as f:
            json.dump({"model": "opus", "statusLine": mine}, f)
        meter.set_live_feed(True)
        s = self.read()
        self.assertTrue(meter.live_feed_enabled())
        self.assertIn("statusline.py", s["statusLine"]["command"])
        self.assertEqual((s["model"], s["statusLine"]["padding"]), ("opus", 1))
        self.assertEqual(meter.load_config()["statusline_prev"], mine)
        meter.set_live_feed(True)        # twice is harmless: yours is still remembered
        self.assertEqual(meter.load_config()["statusline_prev"], mine)
        meter.set_live_feed(False)
        self.assertEqual(self.read(), {"model": "opus", "statusLine": mine})
        self.assertFalse(meter.live_feed_enabled())

    def test_without_a_previous_status_line(self):
        meter.set_live_feed(True)
        self.assertTrue(meter.live_feed_enabled())
        meter.set_live_feed(False)
        self.assertEqual(self.read(), {})

    def test_broken_settings_file_is_never_overwritten(self):
        with open(self.cs, "w") as f:
            f.write("{oops")
        with self.assertRaises(ValueError):
            meter.set_live_feed(True)
        with open(self.cs) as f:
            self.assertEqual(f.read(), "{oops")

    def test_command_uses_a_real_python(self):
        cmd = meter.statusline_cmd()
        self.assertIn("python", cmd.split()[0].lower())
        self.assertTrue(cmd.rstrip().endswith(meter.MARK))



class Phone(unittest.TestCase):
    """The iPhone app: a secret link, read-only, and nothing private beyond what it shows."""
    def setUp(self):
        import phone
        self.phone = phone
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        mock.patch.object(meter, "CONFIG", os.path.join(d.name, "c.json")).start()
        summary = {"block": {"counted": 1200, "messages": 3, "resets_at": None, "input": 9},
                   "week": {"counted": 5000, "messages": 9}, "all": {"counted": 90000, "messages": 99},
                   "daily": [{"day": "d", "tokens": i} for i in range(20)], "hourly": [0] * 24,
                   "projects": [{"project": "Desktop/Secret/App", "tokens": 70}], "models": [{"model": "claude-x", "tokens": 70}]}
        mock.patch.object(meter, "summarize", return_value=summary).start()
        mock.patch.object(meter, "plan_usage", return_value={"plan": "pro", "limits": [], "updated": 1}).start()
        mock.patch.object(sessions, "running", return_value=[
            {"name": "app", "status": "busy", "pane": "%1", "pid": 4242, "cwd": "/Users/me/app", "tty": "/dev/ttys1",
             "session_id": "abc", "elapsed": "01:02:03"}]).start()
        mock.patch.object(sessions, "grid_info", return_value=[{"pane": "%1"}, {"pane": "%2", "command": "zsh"}]).start()
        mock.patch.object(phone, "host", return_value="Test-Mac").start()
        self.addCleanup(mock.patch.stopall)
        phone._cache.update(t=0, data=None)
        self.srv = phone.Server(("::", 0), phone.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"  # IPv4 reaches the dual-stack server
        self.tok = phone.token()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_secret_link_required(self):
        self.assertEqual(self.get("/m/wrong/")[0], 404)
        self.assertEqual(self.get("/")[0], 404)
        self.assertEqual(self.get("/api/terms")[0], 404)  # none of the Mac app's endpoints exist here
        code, body, _ = self.get(f"/m/{self.tok}/")
        self.assertEqual(code, 200)
        self.assertIn(b"Claude Duo", body)

    def test_app_files(self):
        _, m, _ = self.get(f"/m/{self.tok}/manifest.json")
        self.assertEqual(json.loads(m)["display"], "standalone")
        for name, magic in (("icon-180.png", b"\x89PNG"), ("icon-512.png", b"\x89PNG"), ("glass.css", b"/*"),
                            ("ui.js", b"// the Mac")):
            code, body, _ = self.get(f"/m/{self.tok}/{name}")
            self.assertEqual((code, body[:len(magic)]), (200, magic), name)
        self.assertEqual(self.get(f"/m/{self.tok}/../meter.py")[0], 404)
        self.assertEqual(self.get(f"/m/{self.tok}/config.json")[0], 404)

    def test_data_has_nothing_to_act_on(self):
        code, body, headers = self.get(f"/m/{self.tok}/api")
        d = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(d["sessions"], [{"name": "app", "status": "busy", "where": "grid", "elapsed": "01:02:03"}])
        self.assertEqual(d["shells"], 1)
        self.assertEqual(d["usage"]["projects"][0]["name"], "App")  # folder name only, not the path
        self.assertEqual(len(d["usage"]["daily"]), 14)
        for secret in (b"4242", b"/Users/me", b"ttys1", b"abc", b"Secret", b"phone_token", self.tok.encode()):
            self.assertNotIn(secret, body)

    def test_read_only(self):
        req = urllib.request.Request(f"{self.base}/m/{self.tok}/api", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(e.exception.code, 501)

    def test_link_without_slash_redirects(self):
        conn = __import__("http.client").client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        conn.request("GET", f"/m/{self.tok}")
        r = conn.getresponse()
        self.assertEqual((r.status, r.getheader("Location")), (301, f"/m/{self.tok}/"))
        conn.close()

    def test_new_link_locks_out_old_phones(self):
        old = self.tok
        new = self.phone.token(new=True)
        self.assertNotEqual(old, new)
        self.assertEqual(self.get(f"/m/{old}/")[0], 404)
        self.assertEqual(self.get(f"/m/{new}/")[0], 200)

    def test_links_and_qr(self):
        meter.save_config({**meter.load_config(), "phone": True})
        st = self.phone.status()
        links = [st["link"], st.get("ip_link") or ""]
        self.assertTrue(any(l.startswith("http://Test-Mac.local:") for l in links))  # Windows puts the address first
        self.assertTrue(st["link"].endswith(f"/m/{self.tok}/"))
        if st["qr"] is not None:  # Core Image is there on any Mac
            self.assertTrue(st["qr"].startswith("data:image/png;base64,"))

    def test_token_not_settable_from_pages(self):
        self.assertEqual(meter.sanitize({"phone": True, "phone_token": "x"}), {"phone": True})


class CrossPlatform(unittest.TestCase):
    """The parts Windows runs differently: sessions from Claude Code's files, Windows Terminal, the tray icon."""
    def test_sessions_from_files(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            for pid, kind, status in ((os.getpid(), "interactive", "busy"), (999999, "interactive", "idle"),
                                      (os.getppid(), "background", "idle")):
                with open(os.path.join(d, f"{pid}.json"), "w", encoding="utf-8") as f:
                    json.dump({"pid": pid, "sessionId": f"s{pid}", "cwd": os.path.join(d, "my app"), "kind": kind,
                               "status": status, "startedAt": (now - 3725) * 1000}, f)
            with mock.patch.object(sessions, "SESSION_FILES", d), \
                 mock.patch.object(sessions, "_alive_claude", side_effect=lambda pid, started: pid == os.getpid()):
                found = sessions.running_from_files(now)
        self.assertEqual(len(found), 1)  # the dead one and the background one are left out
        s = found[0]
        self.assertEqual((s["pid"], s["name"], s["status"], s["session_id"], s["pane"]),
                         (os.getpid(), "my app", "busy", f"s{os.getpid()}", None))
        self.assertEqual(s["elapsed"], "01:02:05")

    def test_elapsed_like_ps(self):
        self.assertEqual([sessions._etime(x) for x in (59, 125, 3725, 90061)], ["00:59", "02:05", "01:02:05", "1-01:01:01"])

    def test_windows_terminal_command(self):
        with mock.patch("shutil.which", side_effect=lambda n: "C:/wt.exe" if n == "wt" else None), \
             mock.patch.object(sessions, "claude_bin", return_value="C:/claude.exe"):
            cmd = sessions.win_terminal_cmd("C:/code/my app", cont=True)
            self.assertEqual(cmd, ["C:/wt.exe", "-w", "0", "new-tab", "--title", "my app", "-d", "C:/code/my app",
                                   "C:/claude.exe", "--continue"])
            self.assertEqual(sessions.win_terminal_cmd("C:/code", kind="shell")[-2:], ["-d", "C:/code"])
        with mock.patch("shutil.which", return_value=None), mock.patch.object(sessions, "claude_bin", return_value=None):
            self.assertEqual(sessions.win_terminal_cmd("C:/code")[:2], ["cmd.exe", "/c"])  # no Windows Terminal

    def test_windows_project_names(self):
        with mock.patch("os.path.expanduser", return_value="C:\\Users\\me"):
            self.assertEqual(meter.project_name("x/C--Users-me-Desktop-app/a.jsonl"), "Desktop/app")

    def test_tray_text_and_colour(self):
        import winapp
        cfg = {"display": "session", "yellow_at": 50, "red_at": 90}
        plan = {"limits": [{"key": "five_hour", "label": "5-hour session", "percent": 71.4, "resets_at": None},
                           {"key": "seven_day", "label": "Weekly (all models)", "percent": 23.0, "resets_at": None}]}
        text, pct, tip = winapp.summary_line(plan, cfg)
        self.assertEqual((text, round(pct), winapp.level(pct, cfg)), ("71", 71, "yellow"))
        self.assertIn("5-hour session: 71%", tip)
        self.assertLessEqual(len(tip), 127)
        self.assertEqual(winapp.summary_line(plan, {**cfg, "display": "both"})[0], "71")
        self.assertEqual(winapp.summary_line({"error": "No login"}, cfg)[:2], ("?", None))
        self.assertEqual((winapp.level(95, cfg), winapp.level(10, cfg), winapp.level(None, cfg)), ("red", "green", "grey"))

    def test_tray_icon_image(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow is only needed on Windows")
        import winapp
        img = winapp.icon_image("47", "green")
        self.assertEqual(img.size, (64, 64))
        self.assertEqual(img.getpixel((32, 4))[:3], winapp.COLORS["green"])

    def test_statusline_entry_point(self):  # what Claude Code runs on Windows: winapp --statusline
        import subprocess
        here = os.path.join(os.path.dirname(__file__), "..")
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, os.path.join(here, "winapp.py"), "--statusline"], capture_output=True,
                               input=json.dumps({"rate_limits": {"five_hour": {"used_percentage": 12.0}}}), text=True,
                               encoding="utf-8", timeout=30, env={**os.environ, "CLAUDE_DUO_DIR": d})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("12%", r.stdout)

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
                       mock.patch.object(meter, "LAUNCH_PLIST", os.path.join(cls.tmp.name, "l.plist")),
                       mock.patch.object(meter, "CLAUDE_SETTINGS", os.path.join(cls.tmp.name, "cs.json")),
                       mock.patch.object(meter, "LIVE_DIR", os.path.join(cls.tmp.name, "live"))]
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
        req = urllib.request.Request(self.base + path, data=json.dumps(d).encode(), headers={"X-Claude-Usage": "1"})
        with urllib.request.urlopen(req) as r:
            return json.load(r)

    def test_settings_need_app_header(self):  # another website can't change settings (or Claude Code's)
        req = urllib.request.Request(self.base + "/api/settings", data=json.dumps({"live_feed": True}).encode())
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        self.assertEqual(e.exception.code, 403)
        self.assertFalse(meter.live_feed_enabled())

    def test_phone_settings_api(self):
        import phone
        with mock.patch.object(phone, "sync", return_value=None) as sy, \
             mock.patch.object(phone, "qr_data_url", return_value="data:image/png;base64,x"):
            self.assertTrue(self.post("/api/settings", {"phone": True})["phone"])
            sy.assert_called()
            req = urllib.request.Request(self.base + "/api/phone", headers={"X-Claude-Usage": "1"})
            with urllib.request.urlopen(req) as r:
                st = json.load(r)
            self.assertTrue(st["on"])
            self.assertIn("/m/", st["link"])
            with self.assertRaises(urllib.error.HTTPError) as e:  # the link is secret: app pages only
                urllib.request.urlopen(self.base + "/api/phone")
            self.assertEqual(e.exception.code, 403)
            old = st["link"]
            self.post("/api/settings", {"phone_reset": True})
            with urllib.request.urlopen(req) as r:
                self.assertNotEqual(json.load(r)["link"], old)
            self.assertFalse(self.post("/api/settings", {"phone": False})["phone"])

    def test_live_feed_via_settings_api(self):
        self.assertTrue(self.post("/api/settings", {"live_feed": True})["live_feed"])
        self.assertFalse(self.post("/api/settings", {"live_feed": False})["live_feed"])

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


@unittest.skipUnless(sys.platform == "darwin", "the tmux pane grid is Mac-only")
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


@unittest.skipUnless(sys.platform == "darwin", "the menu bar app is the Mac one")
class AppLoads(unittest.TestCase):
    def test_menubar_imports(self):  # catches crashes at start-up (e.g. clashing Objective-C class names)
        import subprocess
        here = os.path.join(os.path.dirname(__file__), "..")
        r = subprocess.run([sys.executable, "-c", "import menubar"], cwd=here, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])


if __name__ == "__main__":
    unittest.main()
