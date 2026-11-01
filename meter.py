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
import json, os, glob, re, shlex, shutil, subprocess, sys, threading, time, urllib.request, urllib.error, urllib.parse
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sessions
from paths import HERE, DATA, WIN, MAC, FROZEN

PORT = int(os.environ.get("PORT", 8765))
CONFIG = os.path.join(DATA, "config.json")
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
WINDOW_HOURS = {"five_hour": 5, "seven_day": 168, "seven_day_opus": 168, "seven_day_sonnet": 168,
                "seven_day_oauth_apps": 168}
LABELS = {"five_hour": "5-hour session", "seven_day": "Weekly (all models)",
          "seven_day_opus": "Weekly (Opus)", "seven_day_sonnet": "Weekly (Sonnet)"}
ORDER = ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"]
LIVE_DIR = os.path.join(DATA, "live")  # Claude Code's live numbers, one file per session (see statusline.py)
STATUSLINE = os.path.join(HERE, "statusline.py")
CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
MARK = "--claude-duo"  # tags our status line command in Claude Code's settings (an ignored argument, fine in any shell)
actions = []  # UI requests from the popover ("open", "quit"), consumed by the menu bar app
on_action = lambda: None  # the menu bar app sets this to handle them at once
state = {"admin_key": os.environ.get("ANTHROPIC_ADMIN_KEY", ""), "menubar_hidden": False}
PAGES = {"/": "index.html", "/index.html": "index.html", "/compact": "compact.html", "/settings": "settings.html",
         "/sessions": "sessions.html"}


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return {**DEFAULTS, **json.load(f)}
    except Exception:
        return dict(DEFAULTS)


def sanitize(d):
    """Validate a settings update from the UI; unknown keys and bad values are dropped."""
    out = {}
    if d.get("display") in ("session", "weekly", "both"):
        out["display"] = d["display"]
    for k in ("notify", "show_icon", "notify_done", "show_busy", "phone"):
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
STARTUP_CMD = os.path.join(os.environ.get("APPDATA") or "~", "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
                           "Claude Duo.cmd")  # Windows: what's in this folder starts when you sign in


def login_enabled():
    return os.path.exists(STARTUP_CMD if WIN else LAUNCH_PLIST)


def win_launch_args():
    """How Windows starts Claude Duo: the .exe, or Python without a console window running winapp.py."""
    if FROZEN:
        return [sys.executable]
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return [pyw if os.path.exists(pyw) else sys.executable, os.path.join(HERE, "winapp.py")]


def set_login(on):
    if WIN:
        if on and not login_enabled():
            os.makedirs(os.path.dirname(STARTUP_CMD), exist_ok=True)
            with open(STARTUP_CMD, "w", encoding="utf-8") as f:
                f.write('@start "" ' + subprocess.list2cmdline(win_launch_args() + ["--background"]) + "\r\n")
        elif not on and login_enabled():
            os.remove(STARTUP_CMD)
        return
    if on and not login_enabled():
        import plistlib
        os.makedirs(os.path.dirname(LAUNCH_PLIST), exist_ok=True)
        with open(LAUNCH_PLIST, "wb") as f:
            plistlib.dump({"Label": "local.claude.usage", "RunAtLoad": True,
                           "ProgramArguments": ["/usr/bin/open", state.get("app_path") or next((p for p in APP_PATHS if os.path.exists(p)), "Claude Duo"), "--args", "--background"]}, f)
    elif not on and login_enabled():
        os.remove(LAUNCH_PLIST)


def save_config(cfg):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def project_name(path):
    """Claude Code names project folders after the path, each character other than a letter or digit -> '-'
    (/Users/me/app -> -Users-me-app, C:\\Users\\me\\app -> C--Users-me-app); turn that back into something readable."""
    d = os.path.basename(os.path.dirname(path))
    home = re.sub(r"[^A-Za-z0-9]", "-", os.path.expanduser("~"))
    return (d[len(home):] if d.startswith(home) else d).strip("-").replace("-", "/") or "~"


def read_local():
    """Return de-duplicated list of (time, model, in, out, cache_write, cache_read)."""
    seen, rows = set(), []
    for path in glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
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


_plan_cache = {"t": 0, "next": 0, "data": None, "error": None, "fails": 0, "plan": None}
_plan_lock = threading.Lock()  # one request to Anthropic at a time, however many windows are asking


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
    if MAC:
        try:
            raw = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                                 capture_output=True, text=True, timeout=10).stdout.strip() or None
        except Exception:
            pass
    if not raw:  # Windows and Linux keep it in this file
        try:
            with open(os.path.expanduser("~/.claude/.credentials.json"), encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            return None
    try:
        return json.loads(raw).get("claudeAiOauth")
    except Exception:
        return None


PLAN_CACHE_FILE = os.path.join(DATA, "plan_cache.json")


def _load_plan_cache():
    """Last good plan numbers survive restarts, so a rate-limited start still has something to show."""
    try:
        with open(PLAN_CACHE_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        _plan_cache.update(t=saved["t"], data=saved["data"])
    except Exception:
        pass


def _fetch_plan(now):
    """One request to the usage endpoint Claude Code's /usage uses (undocumented, so it may change)."""
    c = _plan_cache

    def fail(msg, wait):
        c.update(error=msg, next=now + wait, fails=c["fails"] + 1)

    oauth = read_oauth()
    if not oauth or not oauth.get("accessToken"):
        return fail("No Claude login found. Run `claude` and log in with /login, then reload.", 30)
    c["plan"] = oauth.get("subscriptionType")
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
        if e.code == 429:  # too many checks: wait as long as asked, else 2, 4, 8 ... up to 15 minutes
            ra = str((e.headers or {}).get("retry-after") or "").strip()
            wait = int(ra) if ra.isdigit() and int(ra) > 0 else min(900, 120 * 2 ** c["fails"])
            return fail(f"Anthropic is rate-limiting usage checks; next try in {max(1, round(wait / 60))} min.", wait)
        return fail(f"Plan usage request failed ({e.code}).", 120)
    except Exception as e:
        return fail(str(e), 60)
    limits = [{"key": k, "label": LABELS.get(k, k.replace("_", " ")), "percent": float(v["utilization"]),
               "resets_at": v.get("resets_at")}
              for k, v in raw.items() if isinstance(v, dict) and isinstance(v.get("utilization"), (int, float))]
    data = {"plan": oauth.get("subscriptionType"), "tier": oauth.get("rateLimitTier"), "limits": limits}
    c.update(t=now, next=now, data=data, error=None, fails=0)
    try:
        with open(PLAN_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"t": now, "data": data}, f)
    except OSError:
        pass


def _poll(now, live_t):
    """The usage endpoint, as the backup to Claude Code's live numbers: at most every plan_secs (every 5 min
    or so while live numbers are coming in), one request at a time, backing off when Anthropic rate-limits."""
    c = _plan_cache
    every = load_config().get("plan_secs", 60)
    if live_t and now - live_t < every:
        every = max(300, 5 * every)
    if now >= c["next"] and (c["data"] is None or now - c["t"] >= every) and _plan_lock.acquire(blocking=False):
        try:
            _fetch_plan(now)
        finally:
            _plan_lock.release()
    return c["data"]


def _iso(v):
    """Reset times come as ISO text (usage endpoint) or Unix seconds (Claude Code)."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc).isoformat() if v > 0 else None
    try:
        parse_ts(str(v))
        return str(v)
    except ValueError:
        return None


def live_readings(now=None):
    """The plan usage each Claude Code session saw in Anthropic's replies (files written by statusline.py)."""
    now = now or time.time()
    out = []
    for path in glob.glob(os.path.join(LIVE_DIR, "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            t, rl = float(d["t"]), d["rate_limits"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if now - t > 8 * 86400:  # older than any window: tidy up
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        for k, v in (rl.items() if isinstance(rl, dict) else ()):
            p = v.get("used_percentage") if isinstance(v, dict) else None
            if isinstance(p, (int, float)) and not isinstance(p, bool):
                out.append({"key": k, "percent": float(p), "resets_at": _iso(v.get("resets_at")), "t": t,
                            "source": "claude_code"})
    return out


def merge_limits(readings, now=None):
    """One value per limit from every reading (the usage endpoint and each Claude Code session). Usage only
    goes up within a window, so the highest reading for the current window is the right one. Once a window's
    reset time has passed it is back to 0 % until you use Claude again."""
    now = now or time.time()
    by = defaultdict(list)
    for r in readings:
        by[r["key"]].append(r)
    out = []
    for key in sorted(by, key=lambda k: (ORDER.index(k) if k in ORDER else len(ORDER), k)):
        rs = by[key]
        ends = {id(r): parse_ts(r["resets_at"]).timestamp() for r in rs if r.get("resets_at")}
        current = [r for r in rs if ends.get(id(r), 0) > now]
        if current:
            last_end = max(ends[id(r)] for r in current)
            # same window: the sources' reset times differ by moments, and windows are hours apart
            tol = WINDOW_HOURS.get(key, 4) * 3600 / 4
            current = [r for r in current if last_end - ends[id(r)] < tol]
            best = max(current, key=lambda r: (r["percent"], r["t"]))
            t = max(r["t"] for r in current)
        else:
            ended = max(ends.values(), default=0)
            undated = [r for r in rs if not r.get("resets_at") and r["t"] > ended]  # read after the last reset
            best = max(undated, key=lambda r: r["t"]) if undated else {"percent": 0.0, "resets_at": None,
                                                                           "source": "reset"}
            t = max(r["t"] for r in rs)
        resets_at = best["resets_at"]
        out.append({"key": key, "label": LABELS.get(key, key.replace("_", " ")), "percent": best["percent"],
                    "resets_at": resets_at, "t": t, "source": best.get("source"),
                    "pace": pace(best["percent"], resets_at, WINDOW_HOURS[key])
                    if key in WINDOW_HOURS and resets_at else None})
    return out


def plan_usage():
    """Your plan usage (% of the 5-hour and weekly limits): Claude Code's live numbers merged with the usage
    endpoint. `updated` is when it was last confirmed; `stale` means nothing new for a few minutes."""
    now = time.time()
    live = live_readings(now)
    data = _poll(now, max((r["t"] for r in live), default=0))
    c = _plan_cache
    readings = live + [dict(l, t=c["t"], source="anthropic") for l in (data or {}).get("limits", [])]
    if not readings:
        return {"error": c["error"] or "Connecting to your account…", "plan": c["plan"]}
    limits = merge_limits(readings, now)
    updated = max(l["t"] for l in limits)
    out = {"plan": (data or {}).get("plan") or c["plan"], "tier": (data or {}).get("tier"), "limits": limits,
           "updated": updated, "live": bool(live), "live_feed": live_feed_enabled()}
    if now - updated > max(180, 2 * load_config().get("plan_secs", 60)):
        out["stale"] = True
    if c["error"]:
        out["note"] = c["error"]
    return out


def statusline_cmd():
    """How Claude Code runs statusline.py. On the Mac not sys.executable: inside the app that is the app itself."""
    if WIN:  # Claude Code runs it through Git Bash or PowerShell: forward slashes work in both
        args = [sys.executable, "--statusline"] if FROZEN else [
            next((p for p in (os.path.join(HERE, ".venv", "Scripts", "python.exe"), sys.executable) if os.path.exists(p)),
                 "python"), "-I", STATUSLINE]
        return " ".join(f'"{a}"' if " " in a else a for a in (x.replace("\\", "/") for x in args)) + " " + MARK
    py = next((p for p in (os.path.join(HERE, ".venv", "bin", "python3"),
                           os.path.join(sys.base_prefix, "bin", "python3")) if os.path.exists(p)), None) \
        or shutil.which("python3") or "python3"
    return f"{shlex.quote(py)} -I {shlex.quote(STATUSLINE)} {MARK}"


def _claude_settings():
    try:
        with open(CLAUDE_SETTINGS, encoding="utf-8") as f:
            s = json.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(s, dict):
        raise ValueError("~/.claude/settings.json isn't a JSON object")
    return s


def live_feed_enabled():
    try:
        sl = _claude_settings().get("statusLine")
    except (OSError, ValueError):
        return False
    cmd = str(sl.get("command") or "") if isinstance(sl, dict) else ""
    return MARK in cmd or STATUSLINE.replace("\\", "/") in cmd.replace("\\", "/")


def set_live_feed(on):
    """Make Claude Code's status line feed Claude Duo live numbers (a status line you already had keeps
    showing), or put yours back. The rest of ~/.claude/settings.json is left as it was."""
    if bool(on) == live_feed_enabled():
        return
    s = _claude_settings()  # a file that isn't valid JSON raises, so we never overwrite it
    cfg = load_config()
    cur = s.get("statusLine") if isinstance(s.get("statusLine"), dict) else None
    if on:
        if not (cur and MARK in str(cur.get("command") or "")):  # remember yours (not an older copy of ours)
            cfg["statusline_prev"] = cur
        s["statusLine"] = {**(cur or {}), "type": "command", "command": statusline_cmd()}
    else:
        prev = cfg.pop("statusline_prev", None)
        if isinstance(prev, dict) and prev.get("command") and MARK not in str(prev["command"]):
            s["statusLine"] = prev
        else:
            s.pop("statusLine", None)
    os.makedirs(os.path.dirname(CLAUDE_SETTINGS), exist_ok=True)
    tmp = CLAUDE_SETTINGS + ".claude-duo.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
        f.write("\n")
    os.replace(tmp, CLAUDE_SETTINGS)
    save_config(cfg)


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
        guarded = self.path.startswith("/api/term") or self.path == "/api/phone" \
            or (self.command == "POST" and self.path == "/api/settings")
        if guarded and self.headers.get("X-Claude-Usage") != "1":
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
            self.send({**load_config(), "login": login_enabled(), "live_feed": live_feed_enabled()})
        elif self.path == "/api/plan":
            self.send(plan_usage())
        elif self.path == "/api/phone":  # the iPhone link and its QR code (secret, so app pages only)
            import phone
            self.send(phone.status())
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
            extra = {}
            if "phone" in d or d.get("phone_reset") is True:
                import phone
                if d.get("phone_reset") is True:
                    phone.token(new=True)  # phones with the old link stop working
                err = phone.sync()
                if err:
                    extra["phone_error"] = err
            if isinstance(d.get("live_feed"), bool):
                try:
                    set_live_feed(d["live_feed"])
                except (OSError, ValueError) as e:
                    self.send({"error": f"Couldn't change Claude Code's settings: {e}"}, code=400)
                    return
            self.send({**load_config(), "login": login_enabled(), "live_feed": live_feed_enabled(), **extra})
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
