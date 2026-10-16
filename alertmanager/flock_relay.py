#!/usr/bin/env python3
"""Alertmanager -> Flock relay.

Alertmanager posts its webhook JSON to http://flock-relay:9099/<channel>;
the relay formats it as a Flock message and posts it to the incoming webhook
configured as FLOCK_<CHANNEL> (e.g. /ucpin -> FLOCK_UCPIN, /dubai_south -> FLOCK_DUBAI_SOUTH).
Webhook URLs come from the environment (alertmanager/secrets/flock_channels.env), never from alertmanager.yml.

Optional settings:
  RELAY_PORT        listen port (default 9099)
  DISPLAY_TZ        offset for times in messages, e.g. +05:30 (default +00:00)
  DISPLAY_TZ_NAME   label shown after times (default UTC)
  MAX_ALERTS        alerts listed per message; the rest are counted (default 10)
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("RELAY_PORT", "9099"))
MAX_ALERTS = int(os.environ.get("MAX_ALERTS", "10"))
TZ_NAME = os.environ.get("DISPLAY_TZ_NAME", "UTC")


def _tz(spec):
    sign = -1 if spec.startswith("-") else 1
    hh, mm = spec.lstrip("+-").split(":")
    return timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))


TZ = _tz(os.environ.get("DISPLAY_TZ", "+00:00"))
SEVERITY_ICON = {"critical": "🔴", "page": "🔴", "warning": "🟠", "info": "🔵"}


def log(msg):
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", file=sys.stderr, flush=True)


def parse_time(ts):
    # Alertmanager sends RFC3339 with nanoseconds; "0001-01-01..." means unset.
    if not ts or ts.startswith("0001-"):
        return None
    ts = ts.replace("Z", "+00:00")
    if "." in ts:
        head, rest = ts.split(".", 1)
        n = len(rest) - len(rest.lstrip("0123456789"))
        frac, tail = rest[:n], rest[n:]
        ts = f"{head}.{frac[:6]}{tail}"
    return datetime.fromisoformat(ts)


def fmt_time(dt):
    return f"{dt.astimezone(TZ):%Y-%m-%d %H:%M} {TZ_NAME}" if dt else "?"


def fmt_duration(start, end):
    if not start:
        return ""
    secs = int(((end or datetime.now(timezone.utc)) - start).total_seconds())
    if secs < 60:
        return f"{secs}s"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return " ".join(p for p in (f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m and not d else "") if p)


def render(payload):
    """Return (text, flockml) for one Alertmanager notification."""
    status = payload.get("status", "firing")
    alerts = payload.get("alerts", [])
    firing = [a for a in alerts if a.get("status") == "firing"]
    resolved = [a for a in alerts if a.get("status") == "resolved"]
    common = payload.get("commonLabels", {})
    name = common.get("alertname") or (alerts[0]["labels"].get("alertname", "alert") if alerts else "alert")
    cluster = common.get("cluster") or payload.get("groupLabels", {}).get("cluster", "")
    severity = common.get("severity", "")

    if status == "firing":
        head = f"🔥 FIRING{f' ({len(firing)})' if len(firing) > 1 else ''}"
    else:
        head = "✅ RESOLVED"
    tags = " · ".join(x for x in (severity.upper(), cluster) if x)

    text_lines = [f"{head}: {name}" + (f" [{tags}]" if tags else "")]
    ml = [f"<b>{escape(head)}: {escape(name)}</b>" + (f" — <i>{escape(tags)}</i>" if tags else "")]

    shown = (firing + resolved)[:MAX_ALERTS]
    for a in shown:
        lab, ann = a.get("labels", {}), a.get("annotations", {})
        icon = "✅" if a.get("status") == "resolved" else SEVERITY_ICON.get(lab.get("severity", ""), "⚪")
        server = lab.get("server_name") or lab.get("instance", "")
        extra = " ".join(f"{k}={lab[k]}" for k in ("gateway", "container", "service", "queue", "db") if lab.get(k))
        start, end = parse_time(a.get("startsAt")), parse_time(a.get("endsAt"))
        if a.get("status") == "resolved":
            when = f"Resolved {fmt_time(end)} · lasted {fmt_duration(start, end)}"
        else:
            when = f"Since {fmt_time(start)} · {fmt_duration(start, None)}"
        summary = ann.get("summary", "")
        desc = ann.get("description", "")

        text_lines.append(f"{icon} {server}{f' ({extra})' if extra else ''}: {summary}")
        if desc and desc != summary:
            text_lines.append(f"   {desc}")
        text_lines.append(f"   {when}")

        ml.append(f"<br/>{icon} <b>{escape(server)}</b>{f' <i>({escape(extra)})</i>' if extra else ''}: {escape(summary)}")
        if desc and desc != summary:
            ml.append(f"<br/>{escape(desc)}")
        ml.append(f"<br/><i>{escape(when)}</i>")

    hidden = len(alerts) - len(shown)
    if hidden > 0:
        text_lines.append(f"…and {hidden} more")
        ml.append(f"<br/><i>…and {hidden} more</i>")

    return "\n".join(text_lines), "<flockml>" + "".join(ml) + "</flockml>"


def post_flock(url, text, flockml):
    body = json.dumps({"text": text, "flockml": flockml}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, msg):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(msg.encode())

    def do_GET(self):
        # Health check; lists configured channels, never their URLs.
        chans = sorted(k[6:].lower() for k, v in os.environ.items() if k.startswith("FLOCK_") and v)
        self._reply(200, "ok channels=" + ",".join(chans) + "\n")

    def do_POST(self):
        channel = self.path.strip("/").split("?")[0].lower()
        url = os.environ.get("FLOCK_" + channel.upper().replace("-", "_"), "")
        if not url:
            log(f"unknown channel '{channel}'")
            self._reply(404, f"no webhook configured for channel '{channel}'\n")
            return
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            text, flockml = render(payload)
        except Exception as e:  # bad input: retrying will not help
            log(f"{channel}: bad payload: {e}")
            self._reply(400, "bad payload\n")
            return
        try:
            status = post_flock(url, text, flockml)
            log(f"{channel}: sent {payload.get('status')} {payload.get('commonLabels', {}).get('alertname', '')} ({len(payload.get('alerts', []))} alerts) -> {status}")
            self._reply(200, "sent\n")
        except (urllib.error.URLError, TimeoutError) as e:
            # 5xx makes Alertmanager retry.
            log(f"{channel}: flock post failed: {e}")
            self._reply(502, "flock post failed\n")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    log(f"flock-relay listening on :{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
