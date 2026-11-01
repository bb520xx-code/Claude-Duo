"""Claude Duo for iPhone: a read-only web app your phone opens over your home Wi-Fi.

It runs on its own port, answers only under a secret link (/m/<token>/), and only to GET requests. It shows
your plan usage, token stats and which Claude sessions are working, and cannot change anything on the Mac:
no terminal or settings endpoints exist here. Add it to the iPhone's Home Screen from Safari and it opens
full screen like an app."""
import base64, hmac, json, os, secrets, socket, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import meter, sessions
from paths import HERE, MAC, WIN

PORT = meter.PORT + 1
DIR = os.path.join(HERE, "iphone")
FILES = {"": ("index.html", "text/html; charset=utf-8"), "index.html": ("index.html", "text/html; charset=utf-8"),
         "manifest.json": ("manifest.json", "application/manifest+json"),
         "icon-180.png": ("icon-180.png", "image/png"), "icon-512.png": ("icon-512.png", "image/png"),
         "glass.css": ("../glass.css", "text/css"), "ui.js": ("../ui.js", "text/javascript")}
_server = None
_lock = threading.Lock()
_cache = {"t": 0.0, "data": None}


def token(new=False):
    """The secret part of the link. A new one locks out every phone that had the old link."""
    with _lock:
        cfg = meter.load_config()
        if new or not cfg.get("phone_token"):
            cfg["phone_token"] = secrets.token_urlsafe(12)
            meter.save_config(cfg)
        return cfg["phone_token"]


def host():
    """This computer's name; a Mac's Bonjour name is reachable from iPhones on the same Wi-Fi as <name>.local."""
    name = ""
    if MAC:
        try:
            name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True,
                                  timeout=5).stdout.strip()
        except Exception:
            pass
    return name or socket.gethostname().split(".")[0]


def lan_ip():
    """This Mac's address on the local network (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        s.close()


def links():
    t, ip = token(), lan_ip()
    named = f"http://{host()}.local:{PORT}/m/{t}/"
    by_ip = f"http://{ip}:{PORT}/m/{t}/" if ip else None
    if WIN and by_ip:  # Windows PCs don't reliably answer to <name>.local, so the QR code uses the address
        return {"link": by_ip, "ip_link": named}
    return {"link": named, "ip_link": by_ip}


def qr_data_url(text):
    """A QR code that the iPhone's camera can scan, as a data: URL (segno where installed, else macOS Core Image)."""
    try:
        import segno
        return segno.make(text, error="m").png_data_uri(scale=8, border=2)
    except ImportError:
        pass
    except Exception:
        return None
    try:
        import AppKit, Foundation, Quartz
        f = Quartz.CIFilter.filterWithName_("CIQRCodeGenerator")
        f.setValue_forKey_(Foundation.NSData.dataWithBytes_length_(text.encode(), len(text.encode())), "inputMessage")
        f.setValue_forKey_("M", "inputCorrectionLevel")
        img = f.outputImage().imageByApplyingTransform_(Quartz.CGAffineTransformMakeScale(8, 8))
        rep = AppKit.NSBitmapImageRep.alloc().initWithCIImage_(img)
        png = bytes(rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {}))
        return "data:image/png;base64," + base64.b64encode(png).decode()
    except Exception:
        return None


def data():
    """Everything the phone shows, without paths, process ids or anything it could act on."""
    now = time.time()
    if _cache["data"] and now - _cache["t"] < 4:  # several phones, or a quick refresh, share one look
        return _cache["data"]
    cfg = meter.load_config()
    s = meter.summarize()
    try:
        run = sessions.running()
    except Exception:
        run = []
    try:
        grid = sessions.grid_info()
    except Exception:
        grid = []
    claude_panes = {r.get("pane") for r in run if r.get("pane")}
    d = {"host": host(), "now": now, "refresh_secs": cfg.get("refresh_secs", 10),
         "colors": {k: cfg.get(k, meter.DEFAULTS[k]) for k in ("yellow_at", "red_at")},
         "plan": meter.plan_usage(),
         "usage": {"block": {k: s["block"].get(k) for k in ("counted", "messages", "resets_at")},
                   "week": {k: s["week"].get(k) for k in ("counted", "messages")},
                   "all": {k: s["all"].get(k) for k in ("counted", "messages")},
                   "daily": s["daily"][-14:], "hourly": s["hourly"],
                   "projects": [{"name": p["project"].rsplit("/", 1)[-1] or p["project"], "tokens": p["tokens"]}
                                for p in s["projects"][:5]],
                   "models": s["models"][:4]},
         "sessions": [{"name": r.get("name") or "Claude", "status": r.get("status") or "idle",
                       "where": "grid" if r.get("pane") else "window", "elapsed": r.get("elapsed")} for r in run],
         "shells": sum(1 for p in grid if p.get("pane") not in claude_panes)}
    _cache.update(t=now, data=d)
    return d


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeDuo"

    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json", code=200, cache="no-store"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")  # the secret link never leaks to sites it links to
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        parts = self.path.split("?", 1)[0].split("/")  # "", "m", token, rest
        if len(parts) < 3 or parts[1] != "m" or not hmac.compare_digest(parts[2].encode(), token().encode()):
            return self.send({"error": "not found"}, code=404)
        rest = "/".join(parts[3:])
        if len(parts) == 3:  # /m/<token> without the slash: relative links need it
            self.send_response(301)
            self.send_header("Location", self.path.split("?", 1)[0] + "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if rest == "api":
            try:
                return self.send(data())
            except Exception as e:
                return self.send({"error": str(e)}, code=500)
        if rest in FILES:
            name, ctype = FILES[rest]
            try:
                with open(os.path.normpath(os.path.join(DIR, name)), "rb") as f:
                    return self.send(f.read(), ctype, cache="no-cache")
            except OSError:
                pass
        self.send({"error": "not found"}, code=404)

    def do_HEAD(self):
        self.send_response(405)
        self.end_headers()


class Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6  # IPv6 and IPv4 together: <name>.local can resolve to either
    daemon_threads = True

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()


def running():
    return _server is not None


def start():
    """Start answering phones; returns an error message if the port can't be opened."""
    global _server
    with _lock:
        if _server:
            return None
        try:
            _server = Server(("::", PORT), Handler)
        except OSError as e:
            return f"Couldn't open port {PORT}: {e}"
        threading.Thread(target=_server.serve_forever, daemon=True).start()
    return None


def stop():
    global _server
    with _lock:
        srv, _server = _server, None
    if srv:
        srv.shutdown()
        srv.server_close()


def sync():
    """Match the setting: on starts the phone server, off stops it."""
    return start() if meter.load_config().get("phone") else stop()


def status():
    on = bool(meter.load_config().get("phone"))
    out = {"on": on, "running": running(), "port": PORT}
    if on:
        out.update(links())
        out["qr"] = qr_data_url(out["link"])
    return out
