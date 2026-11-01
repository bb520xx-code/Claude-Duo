#!/usr/bin/env python3
"""Claude Code status line for Claude Duo: the live feed of your plan usage.

After each update Claude Code pipes a JSON snapshot to its status line command. It includes `rate_limits`,
your plan's 5-hour and weekly used %, taken from the headers of Claude's latest reply, so it is exact and
current. We save it (one file per session) for the menu bar, which picks it up within a second with no
extra requests to Anthropic (so it can't be rate-limited). Then we print the status line: the one you had
before Claude Duo, if any, otherwise your usage.

Must be quick and must never fail: Claude Code runs it after every message."""
import json, os, subprocess, sys, time

HERE = os.environ.get("CLAUDE_DUO_DIR") or os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(HERE, "live")
CONFIG = os.path.join(HERE, "config.json")


def save(d):
    rl = d.get("rate_limits")
    if not isinstance(rl, dict) or not rl:
        return  # API key / Bedrock / not signed in with a plan, or no reply yet
    sid = "".join(c for c in str(d.get("session_id") or "default") if c.isalnum() or c in "-_")[:80] or "default"
    cost, ctx = d.get("cost") or {}, d.get("context_window") or {}
    # A fingerprint that changes only when this session hears from Anthropic, so redrawing an idle session
    # (a resize, a mode switch) isn't mistaken for a fresh reading.
    fp = json.dumps([rl, cost.get("total_api_duration_ms"), ctx.get("total_input_tokens"),
                     ctx.get("total_output_tokens")], sort_keys=True)
    path = os.path.join(LIVE, sid + ".json")
    try:
        with open(path) as f:
            if json.load(f).get("fp") == fp:
                return  # nothing new
    except (OSError, ValueError, AttributeError):
        pass
    os.makedirs(LIVE, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump({"t": time.time(), "fp": fp, "rate_limits": rl}, f)
    os.replace(tmp, path)  # atomic: the menu bar never reads half a file


def config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def usage_line(d, cfg):
    rl = d.get("rate_limits") or {}
    yellow, red = cfg.get("yellow_at", 50), cfg.get("red_at", 90)
    parts = []
    for key, name in (("five_hour", "session"), ("seven_day", "week")):
        p = (rl.get(key) or {}).get("used_percentage")
        if isinstance(p, (int, float)):
            color = 31 if p >= red else 33 if p >= yellow else 32  # red / yellow / green, like the menu bar
            parts.append(f"\033[{color}m{p:.0f}%\033[0m {name}")
    return "✦ " + " · ".join(parts) if parts else ""


def main():
    raw = sys.stdin.read()
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            d = {}
    except ValueError:
        d = {}
    try:
        save(d)
    except Exception:
        pass  # never break Claude Code's status line
    cfg = config()
    prev = cfg.get("statusline_prev") or {}
    cmd = prev.get("command") if isinstance(prev, dict) else None
    if cmd and "statusline.py" not in cmd:  # the status line you had before: keep showing it
        try:
            r = subprocess.run(cmd, shell=True, input=raw, capture_output=True, text=True, timeout=10)
            sys.stdout.write(r.stdout)
            return
        except Exception:
            pass
    print(usage_line(d, cfg))


if __name__ == "__main__":
    main()
