"""Claude Duo for Mac as one downloadable app (Claude-Duo-Mac.zip), built with PyInstaller.

Same app as menubar.py; this only adds what the self-contained build needs:
      Claude Duo --statusline   (what Claude Code runs for live numbers; see statusline.py)
      Claude Duo --selftest OUT (checks the server, pages, APIs and iPhone app, for the build)"""
import os, sys

if "--statusline" in sys.argv:  # Claude Code's status line: must stay tiny and fast, so nothing else loads
    import statusline
    statusline.main()
    sys.exit(0)


def selftest(out):
    import json, threading, urllib.request
    from http.server import ThreadingHTTPServer
    import meter, phone
    import menubar  # noqa: F401  (AppKit, WebKit and rumps are bundled and importable)
    results = [("menubar imports", True)]
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
    results.append(("/api/terms", code == 200 and "tmux" in json.loads(body)))
    psrv = phone.Server(("::", 0), phone.Handler)
    threading.Thread(target=psrv.serve_forever, daemon=True).start()
    with urllib.request.urlopen(f"http://127.0.0.1:{psrv.server_address[1]}/m/{phone.token()}/api", timeout=30) as r:
        results.append(("iphone api", "plan" in json.loads(r.read())))
    results.append(("qr code", (phone.qr_data_url("http://example.test/m/x/") or "").startswith("data:image/png")))
    cmd = meter.statusline_cmd()
    results.append(("status line runs this app", sys.executable in cmd and "--statusline" in cmd))
    ok = all(r for _, r in results)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(f"{'ok  ' if r else 'FAIL'} {name}" for name, r in results) + f"\n{'PASSED' if ok else 'FAILED'}\n")
    srv.shutdown()
    psrv.shutdown()
    return ok


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        out = sys.argv[i + 1] if len(sys.argv) > i + 1 else "selftest.txt"
        try:
            ok = selftest(out)
        except Exception:
            import traceback
            with open(out, "w", encoding="utf-8") as f:
                f.write("FAILED\n" + traceback.format_exc())
            ok = False
        os._exit(0 if ok else 1)
    import menubar
    menubar.main()
