#!/usr/bin/env python3
"""Claude usage / token meter.

Run:  python3 meter.py   ->  http://localhost:8765

Sources
  * Local   : Claude Code transcripts in ~/.claude/projects/**/*.jsonl (no login needed)
  * Account : Anthropic Admin API (org-wide API usage). Needs an Admin key (sk-ant-admin...),
              set ANTHROPIC_ADMIN_KEY or paste it in the page. Kept in memory only.

Note: claude.ai subscription "% of plan used" has no public API, so the meter compares
your real token counts against limits you set yourself (config.json).
"""
import json, os, glob, time, urllib.request, urllib.error, urllib.parse
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sessions

PORT = int(os.environ.get("PORT", 8765))
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
PROJECTS = os.path.expanduser("~/.claude/projects")
DEFAULTS = {"limit_5h": 1_000_000, "limit_week": 10_000_000,
            "display": "session",  # what the menu bar shows: session | weekly | both
            "show_icon": True, "refresh_secs": 10, "plan_secs": 60, "notify_done": True, "show_busy": True,
            "notify": True, "notify_at": [80, 95],
            "yellow_at": 50, "red_at": 90}  # menu bar / bar colours (percent used)
LINKS = {"credits": "https://claude.ai/settings/usage",       # extra usage / credits live here
         "upgrade": "https://claude.ai/upgrade",
         "api_credits": "https://platform.claude.com/settings/billing",
         # Claude on the web, for the Settings tab (opened in your browser)
         "web_new": "https://claude.ai/new", "web_recents": "https://claude.ai/recents",
         "web_projects": "https://claude.ai/projects", "web_artifacts": "https://claude.ai/artifacts",
         "web_code": "https://claude.ai/code", "web_profile": "https://claude.ai/settings/profile",
         "web_account": "https://claude.ai/settings/account", "web_appearance": "https://claude.ai/settings/appearance",
         "web_capabilities": "https://claude.ai/settings/capabilities",
         "web_connectors": "https://claude.ai/settings/connectors", "web_privacy": "https://claude.ai/settings/privacy",
         "web_billing": "https://claude.ai/settings/billing", "web_usage": "https://claude.ai/settings/usage",
         "web_download": "https://claude.ai/download", "web_help": "https://support.claude.com",
         "web_status": "https://status.anthropic.com", "web_docs": "https://docs.claude.com",
         "web_console": "https://platform.claude.com"}
WINDOW_HOURS = {"five_hour": 5, "seven_day": 24 * 7}
actions = []  # UI requests from the popover ("open", "quit"), consumed by the menu bar app
on_action = lambda: None  # the menu bar app sets this to handle them at once
state = {"admin_key": os.environ.get("ANTHROPIC_ADMIN_KEY", ""), "menubar_hidden": False}
PAGES = {"/": "index.html", "/index.html": "index.html", "/compact": "compact.html", "/settings": "settings.html",
         "/sessions": "sessions.html"}


def load_config():
    try:
        with open(CONFIG) as f:
            return {**DEFAULTS, **json.load(f)}
    except Exception:
        return dict(DEFAULTS)


def sanitize(d):
    """Validate a settings update from the UI; unknown keys and bad values are dropped."""
    out = {}
    if d.get("display") in ("session", "weekly", "both"):
        out["display"] = d["display"]
    for k in ("notify", "show_icon", "notify_done", "show_busy"):
        if isinstance(d.get(k), bool):
            out[k] = d[k]
    for k in ("limit_5h", "limit_week"):
        if isinstance(d.get(k), (int, float)) and d[k] > 0:
            out[k] = int(d[k])
    for k in ("yellow_at", "red_at"):  # colour thresholds, 1-100 %
        if isinstance(d.get(k), (int, float)) and not isinstance(d[k], bool):
            out[k] = int(min(100, max(1, d[k])))
    if isinstance(d.get("refresh_secs"), (int, float)):
        out["refresh_secs"] = int(min(600, max(10, d["refresh_secs"])))
    if isinstance(d.get("plan_secs"), (int, float)):  # how often to ask Anthropic for the plan %; it rate-limits fast
        out["plan_secs"] = int(min(3600, max(30, d["plan_secs"])))
    if isinstance(d.get("notify_at"), list):
        vals = sorted({int(x) for x in d["notify_at"] if isinstance(x, (int, float)) and 1 <= x <= 100})[:4]
        out["notify_at"] = vals
    return out


APP_PATHS = [os.path.expanduser("~/Applications/Claude Duo.app"), "/Applications/Claude Duo.app"]  # fallbacks
LAUNCH_PLIST = os.path.expanduser("~/Library/LaunchAgents/local.claude.usage.plist")


def login_enabled():
    return os.path.exists(LAUNCH_PLIST)


def set_login(on):
    if on and not login_enabled():
        import plistlib
        os.makedirs(os.path.dirname(LAUNCH_PLIST), exist_ok=True)
        with open(LAUNCH_PLIST, "wb") as f:
            plistlib.dump({"Label": "local.claude.usage", "RunAtLoad": True,
                           "ProgramArguments": ["/usr/bin/open", state.get("app_path") or next((p for p in APP_PATHS if os.path.exists(p)), "Claude Duo"), "--args", "--background"]}, f)
    elif not on and login_enabled():
        os.remove(LAUNCH_PLIST)


def save_config(cfg):
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def project_name(path):
    """Claude Code names project folders after the path with '/' -> '-'; turn that back into something readable."""
    d = os.path.basename(os.path.dirname(path))
    home = os.path.expanduser("~").replace("/", "-")
    return (d[len(home):] if d.startswith(home) else d).strip("-").replace("-", "/") or "~"


def read_local():
    """Return de-duplicated list of (time, model, in, out, cache_write, cache_read)."""
    seen, rows = set(), []
    for path in glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True):
        try:
            with open(path, errors="ignore") as f:
                for line in f:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                        m = d.get("message") or {}
                        u = m.get("usage")
                        if not u or d.get("type") != "assistant":
                            continue
                        key = (m.get("id"), d.get("requestId"))
                        if key != (None, None):
                            if key in seen:
                                continue
                            seen.add(key)
                        rows.append((
                            parse_ts(d["timestamp"]), m.get("model", "unknown"),
                            u.get("input_tokens", 0) or 0, u.get("output_tokens", 0) or 0,
                            u.get("cache_creation_input_tokens", 0) or 0,
                            u.get("cache_read_input_tokens", 0) or 0,
                            project_name(path),
                        ))
                    except Exception:
                        continue
        except OSError:
            continue
    rows.sort(key=lambda r: r[0])
    return rows


def total(r):
    # cache reads are billed/limited far lighter; count them separately
    return r[2] + r[3] + r[4]


def summarize():
    cfg = load_config()
    rows = read_local()
    now = datetime.now(timezone.utc)

    # 5-hour session block: starts at first message after a >5h gap, floored to the hour
    block_start = None
    for r in rows:
        if block_start is None or r[0] >= block_start + timedelta(hours=5):
            block_start = r[0].replace(minute=0, second=0, microsecond=0)
    in_block = block_start is not None and now < block_start + timedelta(hours=5)
    block = [r for r in rows if in_block and r[0] >= block_start]
    week = [r for r in rows if r[0] >= now - timedelta(days=7)]

    def agg(rs):
        return {"input": sum(r[2] for r in rs), "output": sum(r[3] for r in rs),
                "cache_write": sum(r[4] for r in rs), "cache_read": sum(r[5] for r in rs),
                "counted": sum(total(r) for r in rs), "messages": len(rs)}

    days, models = defaultdict(int), defaultdict(int)
    hourly = [0] * 24  # tokens by local hour of day, last 30 days
    for r in rows:
        if r[0] >= now - timedelta(days=30):
            hourly[r[0].astimezone().hour] += total(r)
    projects = defaultdict(int)
    for r in rows:
        projects[r[6]] += total(r)
    for r in rows:
        if r[0] >= now - timedelta(days=14):
            days[r[0].astimezone().strftime("%Y-%m-%d")] += total(r)
        models[r[1]] += total(r)
    daily = []
    for i in range(13, -1, -1):
        k = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        daily.append({"day": k, "tokens": days.get(k, 0)})

    return {
        "now": now.isoformat(),
        "limits": {"limit_5h": cfg["limit_5h"], "limit_week": cfg["limit_week"]},
        "block": {**agg(block), "resets_at": (block_start + timedelta(hours=5)).isoformat() if in_block else None},
        "week": agg(week), "all": agg(rows), "daily": daily, "hourly": hourly,
        "projects": sorted(({"project": k, "tokens": v} for k, v in projects.items()), key=lambda x: -x["tokens"])[:8],
        "models": sorted(({"model": k, "tokens": v} for k, v in models.items()), key=lambda x: -x["tokens"]),
        "has_admin_key": bool(state["admin_key"]),
    }


def admin_usage():
    if not state["admin_key"]:
        return {"error": "No Admin key connected."}
    start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z")
    qs = urllib.parse.urlencode({"starting_at": start, "bucket_width": "1d", "limit": 7})
    req = urllib.request.Request(
        f"https://api.anthropic.com/v1/organizations/usage_report/messages?{qs}",
        headers={"x-api-key": state["admin_key"], "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        return {"error": f"Anthropic API {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"error": str(e)}
    days = []
    for b in data.get("data", []):
        t = {"input": 0, "output": 0, "cache_read": 0}
        for x in b.get("results", []):
            t["input"] += x.get("uncached_input_tokens", 0) + (x.get("cache_creation", {}) or {}).get("ephemeral_1h_input_tokens", 0) \
                + (x.get("cache_creation", {}) or {}).get("ephemeral_5m_input_tokens", 0)
            t["output"] += x.get("output_tokens", 0)
            t["cache_read"] += x.get("cache_read_input_tokens", 0)
        days.append({"day": b["starting_at"][:10], **t})
    return {"days": days}


_plan_cache = {"t": 0, "until": 0, "data": None, "error": None}


def pace(percent, resets_at, window_hours, now=None):
    """Projected % used at reset if you keep the pace so far this window (None until there's enough data)."""
    if not resets_at or not percent:
        return None
    now = now or datetime.now(timezone.utc)
    left = (parse_ts(resets_at) - now).total_seconds() / 3600
    elapsed = window_hours - left
    if left <= 0 or elapsed < window_hours * 0.04:
        return None
    return percent / elapsed * window_hours


def read_oauth():
    """Find the login Claude Code already stored (macOS Keychain, else ~/.claude/.credentials.json)."""
    raw = None
    try:
        import subprocess
        raw = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                             capture_output=True, text=True, timeout=10).stdout.strip() or None
    except Exception:
        pass
    if not raw:
        try:
            with open(os.path.expanduser("~/.claude/.credentials.json")) as f:
                raw = f.read()
        except OSError:
            return None
    try:
        return json.loads(raw).get("claudeAiOauth")
    except Exception:
        return None


PLAN_CACHE_FILE = os.path.join(HERE, "plan_cache.json")


def _load_plan_cache():
    """Last good plan numbers survive restarts, so a rate-limited start still has something to show."""
    try:
        with open(PLAN_CACHE_FILE) as f:
            saved = json.load(f)
        _plan_cache.update(t=saved["t"], data=saved["data"])
    except Exception:
        pass


def plan_usage():
    """Real plan usage (% of 5h / weekly limits) for the logged-in claude.ai account.
    Uses the same undocumented endpoint Claude Code's /usage uses, so it may change. It rate-limits (429)
    easily, so results are cached, and on failure the last good numbers are kept (marked stale) while we back off."""
    now = time.time()
    last = _plan_cache["data"]
    if last and now < _plan_cache["until"]:  # fresh cache, or backing off after an error
        return last if now - _plan_cache["t"] < 120 or not _plan_cache["error"] else {**last, "stale": True, "stale_since": _plan_cache["t"]}

    def fail(msg, wait):
        _plan_cache.update(until=now + wait, error=msg)
        if last:
            return {**last, "stale": True, "stale_since": _plan_cache["t"]}
        return {"error": msg, "plan": (oauth or {}).get("subscriptionType")}

    oauth = read_oauth()
    if not oauth or not oauth.get("accessToken"):
        return fail("No Claude login found. Run `claude` and log in with /login, then reload.", 30)
    exp = oauth.get("expiresAt")
    if exp and exp / 1000 < now:
        return fail("Your Claude login expired. Open Claude Code once to refresh it, then reload.", 30)
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": f"Bearer {oauth['accessToken']}",
        "anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-usage-meter"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            wait = max(300, int(e.headers.get("retry-after") or 0))  # too many requests: pause a few minutes
            return fail(f"Anthropic is rate-limiting usage checks; retrying in {wait // 60} min.", wait)
        return fail(f"Plan usage request failed ({e.code}).", 120)
    except Exception as e:
        return fail(str(e), 60)
    labels = {"five_hour": "5-hour session", "seven_day": "Weekly (all models)",
              "seven_day_opus": "Weekly (Opus)", "seven_day_sonnet": "Weekly (Sonnet)"}
    limits = [{"key": k, "label": labels.get(k, k.replace("_", " ")), "percent": v["utilization"],
               "resets_at": v.get("resets_at"),
               "pace": pace(v["utilization"], v.get("resets_at"), WINDOW_HOURS.get(k, 0)) if k in WINDOW_HOURS else None}
              for k, v in raw.items() if isinstance(v, dict) and v.get("utilization") is not None]
    data = {"plan": oauth.get("subscriptionType"), "tier": oauth.get("rateLimitTier"), "limits": limits}
    _plan_cache.update(t=now, until=now + load_config().get("plan_secs", 60), data=data, error=None)
    try:
        with open(PLAN_CACHE_FILE, "w") as f:
            json.dump({"t": now, "data": data}, f)
    except OSError:
        pass
    return data


_load_plan_cache()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json", code=200):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def local(self):
        """Only pages served by this app may talk to it (blocks DNS-rebinding and cross-site requests)."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            self.send({"error": "forbidden"}, code=403)
            return False
        if self.path.startswith("/api/term") and self.headers.get("X-Claude-Usage") != "1":
            self.send({"error": "forbidden"}, code=403)  # the custom header can't be sent cross-site without CORS
            return False
        return True

    def body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if not self.local():
            return
        path = self.path.partition("?")[0]
        if path in PAGES:
            with open(os.path.join(HERE, PAGES[path]), "rb") as f:
                self.send(f.read(), "text/html; charset=utf-8")
        elif path == "/api/status":
            self.send({"menubar_hidden": state["menubar_hidden"]})
        elif path == "/api/terms":
            try:
                grid = sessions.grid_info()
            except ValueError:
                grid = []
            self.send({"running": sessions.running(), "grid": grid, "recent": sessions.recent_folders(),
                       "claude": bool(sessions.claude_bin()), "tmux": bool(sessions.tmux_bin()),
                       "home": os.path.expanduser("~")})
        elif self.path == "/api/summary":
            self.send(summarize())
        elif self.path in ("/glass.css", "/ui.js"):
            with open(os.path.join(HERE, self.path[1:]), "rb") as f:
                self.send(f.read(), "text/css" if self.path.endswith("css") else "text/javascript")
        elif self.path == "/api/settings":
            self.send({**load_config(), "login": login_enabled()})
        elif self.path == "/api/plan":
            self.send(plan_usage())
        elif self.path == "/api/account":
            self.send(admin_usage())
        else:
            self.send({"error": "not found"}, code=404)

    def do_POST(self):
        if not self.local():
            return
        d = self.body()
        parts = self.path.strip("/").split("/")  # e.g. api/term/<pid>/focus, api/term/pane/<n>/close
        kind = "shell" if d.get("kind") == "shell" else "claude"
        try:
            if self.path == "/api/term/new":  # a new pane in the grid: Claude, or a plain terminal
                self.send(sessions.new_session(d.get("cwd"), bool(d.get("continue")), kind))
            elif self.path == "/api/term/choose":  # native folder picker, shown by the menu bar app
                actions.append(("choose_folder", bool(d.get("continue")), kind))
                on_action()
                self.send({"ok": True})
            elif self.path == "/api/term/grid":  # show the grid window (a terminal pane if there's no grid yet)
                self.send({"ok": sessions.show_grid() if sessions.grid_exists()
                           else sessions.new_session("~", kind="shell")["ok"]})
            elif self.path == "/api/term/gather":
                self.send(sessions.gather())
            elif len(parts) == 5 and parts[:3] == ["api", "term", "pane"] and parts[3].isdigit():
                self.send({"ok": sessions.pane_action("%" + parts[3], parts[4])})
            elif len(parts) == 4 and parts[:2] == ["api", "term"] and parts[2].isdigit() \
                    and parts[3] in ("focus", "stop", "move"):
                pid = int(parts[2])
                self.send(sessions.move_to_grid(pid) if parts[3] == "move" else
                          {"ok": sessions.focus(pid) if parts[3] == "focus" else sessions.stop(pid)})
            elif self.path.startswith("/api/term"):
                self.send({"error": "not found"}, code=404)
        except ValueError as e:
            self.send({"error": str(e)}, code=400)
        if self.path.startswith("/api/term"):
            return
        if self.path == "/api/limits":
            cfg = load_config()
            for k in ("limit_5h", "limit_week"):
                if int(d.get(k, 0)) > 0:
                    cfg[k] = int(d[k])
            save_config(cfg)
            self.send({"ok": True})
        elif self.path == "/api/settings":
            cfg = load_config()
            cfg.update(sanitize(d))
            save_config(cfg)
            if isinstance(d.get("login"), bool):
                set_login(d["login"])
            self.send({**cfg, "login": login_enabled()})
        elif self.path == "/api/action":
            if d.get("name") in ("open", "quit", "settings", "diagnose", "sessions", "menubar_settings") \
                    or d.get("name") in LINKS:
                actions.append(d["name"])
                on_action()
            self.send({"ok": True})
        elif self.path == "/api/connect":
            state["admin_key"] = (d.get("key") or "").strip()
            self.send({"ok": True})
        else:
            self.send({"error": "not found"}, code=404)


if __name__ == "__main__":
    print(f"Claude usage meter -> http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
