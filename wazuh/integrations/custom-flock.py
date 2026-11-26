#!/usr/bin/env python3
"""Wazuh -> Flock integration (wazuh-integratord, <integration><name>custom-flock</name>).

Called by integratord as: custom-flock <alert.json> <api_key> <hook_url>
<hook_url> is the path of an env file with the Flock webhooks (the same file as
alertmanager/secrets/flock_channels.env: FLOCK_UCPIN=https://api.flock.com/hooks/sendMessage/...),
so no webhook URL is in ossec.conf or git.

Channel: FLOCK_WAZUH if set (one security channel), else by agent name:
  ucpin* -> FLOCK_UCPIN, uc11/uc12 -> FLOCK_UCDEMO, ucpmed* -> FLOCK_UCPMED, my0* -> FLOCK_MEYDAN,
  shams* -> FLOCK_SHAMS, spc*/sj0* -> FLOCK_SPC, ds0*/dsouth* -> FLOCK_DUBAI_SOUTH,
  anything else -> FLOCK_MULTICOMM.
Message format matches alertmanager/flock_relay.py (text + flockml).
"""
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape

ROUTES = [
    (r"^ucpmed", "UCPMED"), (r"^ucpin", "UCPIN"), (r"^uc1[0-9]", "UCDEMO"),
    (r"^my0", "MEYDAN"), (r"^shams", "SHAMS"), (r"^(spc|sj0)", "SPC"),
    (r"^(ds0|dsouth)", "DUBAI_SOUTH"),
]
DISPLAY_TZ = timedelta(hours=5, minutes=30)     # IST, like the relay's DISPLAY_TZ=+05:30
DISPLAY_TZ_NAME = "IST"


def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def channel_for(agent):
    for rx, ch in ROUTES:
        if re.match(rx, agent or "", re.I):
            return ch
    return "MULTICOMM"


def render(alert):
    rule = alert.get("rule", {})
    agent = alert.get("agent", {})
    data = alert.get("data", {})
    level = int(rule.get("level", 0))
    sev = "CRITICAL" if level >= 12 else "WARNING"
    icon = "\U0001F534" if level >= 12 else "\U0001F7E0"
    try:
        ts = datetime.strptime(alert["timestamp"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        when = (ts + DISPLAY_TZ).strftime("%d %b %H:%M:%S ") + DISPLAY_TZ_NAME
    except (KeyError, ValueError):
        when = ""
    host = agent.get("name", "?")
    desc = rule.get("description", "")
    extra = []
    if data.get("srcip"):
        extra.append("Source IP: %s" % data["srcip"])
    if alert.get("syscheck", {}).get("path"):
        extra.append("File: %s" % alert["syscheck"]["path"])
    diff = (alert.get("syscheck", {}).get("diff") or "").strip()
    if diff:
        extra.append("Change: %s" % diff[:300].replace("\n", " | "))
    extra.append("Rule %s, level %d · Wazuh" % (rule.get("id", "?"), level))
    text = "%s %s · %s\n%s\n%s\n%s" % (icon, sev, host, desc, when, "\n".join(extra))
    ml = "<flockml>%s <b>%s · %s</b><br/>%s<br/><i>%s</i><br/>%s</flockml>" % (
        icon, sev, escape(host), escape(desc), escape(when), "<br/>".join(escape(x) for x in extra))
    return text, ml


def main():
    alert_file, hook = sys.argv[1], (sys.argv[3] if len(sys.argv) > 3 else "")
    with open(alert_file) as f:
        alert = json.load(f)
    env = load_env(hook[5:] if hook.startswith("file:") else hook)
    url = env.get("FLOCK_WAZUH") or env.get("FLOCK_" + channel_for(alert.get("agent", {}).get("name")))
    if not url:
        sys.exit("custom-flock: no webhook for this alert in %s" % hook)
    text, ml = render(alert)
    req = urllib.request.Request(url, data=json.dumps({"text": text, "flockml": ml}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10).read()


if __name__ == "__main__":
    main()
