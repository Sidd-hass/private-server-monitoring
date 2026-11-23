#!/usr/bin/env python3
"""Call statistics and RabbitMQ queue exporter for one Kazoo/fonouc site.

Read-only. It only sends GET requests:
  * CouchDB: _design/cdrs/_view/crossbar_listing in the account's monthly CDR databases
    (account/xx/yy/rest-YYYYMM), and _design/users/_view/crossbar_listing for user names.
    Both views are the ones Kazoo crossbar already uses, so they stay indexed; the
    exporter asks with update=lazy so a query never waits on (or forces) an index build.
  * RabbitMQ: the rabbitmq_prometheus plugin (port 15692). /metrics is passed through as-is;
    /metrics/detailed (queue_coarse_metrics + queue_consumer_count) is grouped into queue
    families so 5000 per-connection queues do not become 5000 Prometheus series.

The CDR rows of the last 24 h stay in memory (about 20k rows / 20 MB at Meydan's volume).
Every POLL_SECONDS only rows newer than the last one seen are fetched.

A CDR is one call leg. Legs with an owner_id belong to a user (agent) and are counted per
user. Kazoo's call_direction is seen from FreeSWITCH, so for a user's leg:
  call_direction "inbound"  = the user's phone sent the call  -> call_type "outgoing"
  call_direction "outbound" = the switch rang the user's phone -> call_type "incoming"
"""

import base64
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COUCH_URL = os.environ.get("COUCH_URL", "http://127.0.0.1:5984").rstrip("/")
# Comma-separated account ids; empty = every account in the "accounts" database.
ACCOUNTS = [a.strip() for a in os.environ.get("ACCOUNTS", "").split(",") if a.strip()]
RABBIT_URL = os.environ.get("RABBIT_URL", "http://127.0.0.1:15692").rstrip("/")
LISTEN = os.environ.get("LISTEN", "0.0.0.0:9480")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "30"))
USERS_REFRESH_SECONDS = int(os.environ.get("USERS_REFRESH_SECONDS", "600"))
WINDOW_SECONDS = int(os.environ.get("WINDOW_SECONDS", "86400"))
# "today" resets at local midnight in this offset (Dubai +04:00).
TZ_OFFSET = os.environ.get("TZ_OFFSET", "+04:00")
TOP_QUEUES = int(os.environ.get("TOP_QUEUES", "25"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "60"))
# CDRs are written at hangup; re-read this much before the newest key so late writes are not missed.
OVERLAP_SECONDS = int(os.environ.get("OVERLAP_SECONDS", "300"))

GREGORIAN_OFFSET = 62167219200  # Kazoo timestamps are seconds since year 0

NORMAL_END = {"NORMAL_CLEARING", "NORMAL_UNSPECIFIED", "ORIGINATOR_CANCEL"}
NO_ANSWER = {"NO_ANSWER", "NO_USER_RESPONSE", "ALLOTTED_TIMEOUT", "PROGRESS_TIMEOUT"}
STATUSES = ["answered", "dropped", "cancelled", "unanswered", "no_answer", "busy",
            "rejected", "answered_elsewhere", "failed"]


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%S ") + msg, flush=True)


def parse_offset(text):
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", text.strip())
    if not m:
        return timezone.utc
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return timezone(delta if m.group(1) == "+" else -delta)


LOCAL_TZ = parse_offset(TZ_OFFSET)


def to_int(value):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def classify(hangup_cause, billing_seconds):
    """One status per leg. Answered = billed seconds > 0."""
    hc = (hangup_cause or "UNKNOWN").upper()
    if hc == "LOSE_RACE":
        return "answered_elsewhere"  # rang several phones, another one picked up
    if billing_seconds > 0:
        return "answered" if hc in NORMAL_END else "dropped"
    if hc == "ORIGINATOR_CANCEL":
        return "cancelled"  # caller hung up before it was answered
    if hc in NO_ANSWER:
        return "no_answer"
    if hc == "USER_BUSY":
        return "busy"
    if hc == "CALL_REJECTED":
        return "rejected"
    if hc in ("NORMAL_CLEARING", "NORMAL_UNSPECIFIED"):
        return "unanswered"  # ended normally before anyone answered
    return "failed"


def esc(value):
    return str(value).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


def labels(**kw):
    return "{" + ",".join('%s="%s"' % (k, esc(v)) for k, v in kw.items()) + "}"


def split_auth(url):
    """http://user:pass@host:port -> (http://host:port, Basic auth header or None)."""
    parts = urllib.parse.urlsplit(url)
    if not parts.username:
        return url, None
    netloc = parts.hostname + (":%d" % parts.port if parts.port else "")
    token = "%s:%s" % (urllib.parse.unquote(parts.username), urllib.parse.unquote(parts.password or ""))
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc)), "Basic " + base64.b64encode(token.encode()).decode()


COUCH_URL, COUCH_AUTH = split_auth(COUCH_URL)
RABBIT_URL, RABBIT_AUTH = split_auth(RABBIT_URL)


def http_get(url, timeout=HTTP_TIMEOUT, accept="application/json", auth=None):
    req = urllib.request.Request(url, headers={"Accept": accept})
    if auth:
        req.add_header("Authorization", auth)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def couch(path, **params):
    query = urllib.parse.urlencode(params)
    return json.loads(http_get(COUCH_URL + path + ("?" + query if query else ""), auth=COUCH_AUTH))


def modb_path(account_id, year, month):
    # account/54/43/70e9...-202611, URL-encoded as one path segment
    db = "account/%s/%s/%s-%04d%02d" % (account_id[:2], account_id[2:4], account_id[4:], year, month)
    return "/" + urllib.parse.quote(db, safe="")


def account_path(account_id):
    db = "account/%s/%s/%s" % (account_id[:2], account_id[2:4], account_id[4:])
    return "/" + urllib.parse.quote(db, safe="")


def months_between(start_ts, end_ts):
    """(year, month) of every month touching [start_ts, end_ts] (UTC; Kazoo names modbs in UTC)."""
    cur = datetime.fromtimestamp(start_ts, timezone.utc).replace(day=1)
    end = datetime.fromtimestamp(end_ts, timezone.utc)
    out = []
    while (cur.year, cur.month) <= (end.year, end.month):
        out.append((cur.year, cur.month))
        cur = (cur + timedelta(days=32)).replace(day=1)
    return out


class Collector:
    def __init__(self):
        self.lock = threading.Lock()
        self.accounts = {}        # id -> name
        self.users = {}           # account_id -> {user_id: (name, extension)}
        self.users_loaded = 0.0
        self.legs = {}            # cdr id -> leg tuple
        self.newest_key = {}      # account_id -> newest crossbar_listing key seen (gregorian)
        self.counters = defaultdict(int)  # (account, user_id, call_type, status) -> legs since start
        self.talk_counter = defaultdict(int)
        self.errors = defaultdict(int)
        self.last_poll_ok = 0.0
        self.last_poll_duration = 0.0
        self.started = time.time()
        self.call_metrics = ""
        self.rabbit_metrics = ""
        self.rabbit_up = 0

    # ---------------------------------------------------------------- accounts / users
    def load_accounts(self):
        ids = ACCOUNTS
        if not ids:
            rows = couch("/accounts/_all_docs")["rows"]
            ids = [r["id"] for r in rows if not r["id"].startswith("_design/")]
        names = {}
        for account_id in ids:
            try:
                names[account_id] = couch("/accounts/" + account_id).get("name") or account_id
            except urllib.error.HTTPError:
                names[account_id] = account_id
        self.accounts = names
        log("accounts: " + ", ".join("%s (%s)" % (n, i) for i, n in names.items()))

    def load_users(self):
        users = {}
        for account_id in self.accounts:
            try:
                rows = couch(account_path(account_id) + "/_design/users/_view/crossbar_listing",
                             stable="true", update="lazy")["rows"]
            except urllib.error.HTTPError as err:
                if err.code != 404:
                    raise
                rows = []
            table = {}
            for row in rows:
                v = row.get("value") or {}
                name = " ".join(x for x in (v.get("first_name"), v.get("last_name")) if x).strip()
                table[row["id"]] = (name or v.get("username") or row["id"], v.get("presence_id") or "")
            users[account_id] = table
        self.users = users
        self.users_loaded = time.time()
        log("users loaded: " + ", ".join("%s=%d" % (self.accounts[a], len(t)) for a, t in users.items()))

    # ---------------------------------------------------------------- CDRs
    def fetch_cdrs(self, account_id, start_ts, end_ts):
        """crossbar_listing rows with key (pvt_created, gregorian) in [start_ts, end_ts] (unix)."""
        rows = []
        for year, month in months_between(start_ts, end_ts):
            path = modb_path(account_id, year, month) + "/_design/cdrs/_view/crossbar_listing"
            try:
                data = couch(path, startkey=int(start_ts) + GREGORIAN_OFFSET,
                             endkey=int(end_ts) + GREGORIAN_OFFSET, stable="true", update="lazy")
            except urllib.error.HTTPError as err:
                if err.code == 404:  # month database not created yet / no calls that month
                    continue
                raise
            rows.extend(data.get("rows", []))
        return rows

    def poll_cdrs(self, now):
        window_start = now - WINDOW_SECONDS
        added = 0
        for account_id in self.accounts:
            newest = self.newest_key.get(account_id)
            start = window_start if newest is None else max(window_start, newest - GREGORIAN_OFFSET - OVERLAP_SECONDS)
            rows = self.fetch_cdrs(account_id, start, now + 60)
            for row in rows:
                cdr_id = row.get("id")
                if not cdr_id or cdr_id in self.legs:
                    continue
                v = row.get("value") or {}
                key = to_int(row.get("key"))
                billing = to_int(v.get("billing_seconds"))
                status = classify(v.get("hangup_cause"), billing)
                owner = v.get("owner_id") or ""
                direction = v.get("direction") or "unknown"
                call_type = {"inbound": "outgoing", "outbound": "incoming"}.get(direction, "unknown") if owner else ""
                leg = (account_id, key - GREGORIAN_OFFSET, owner, direction, call_type, status,
                       (v.get("hangup_cause") or "UNKNOWN").upper(), billing)
                self.legs[cdr_id] = leg
                if newest is not None and owner:
                    # Counters only count legs seen after the first load, so a restart does not jump them.
                    self.counters[(account_id, owner, call_type, status)] += 1
                    self.talk_counter[(account_id, owner, call_type)] += billing
                if self.newest_key.get(account_id) is None or key > self.newest_key[account_id]:
                    self.newest_key[account_id] = key
                added += 1
            if account_id not in self.newest_key:
                self.newest_key[account_id] = int(start) + GREGORIAN_OFFSET
        cutoff = window_start
        for cdr_id in [k for k, leg in self.legs.items() if leg[1] < cutoff]:
            del self.legs[cdr_id]
        return added

    def render_calls(self, now):
        midnight = datetime.fromtimestamp(now, LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        windows = {"24h": now - WINDOW_SECONDS, "today": midnight}
        site = defaultdict(int)
        per_user = defaultdict(int)
        talk = defaultdict(int)
        newest = {}
        for leg in self.legs.values():
            account_id, ts, owner, direction, call_type, status, cause, billing = leg
            newest[account_id] = max(newest.get(account_id, 0), ts)
            for window, start in windows.items():
                if ts < start:
                    continue
                site[(window, account_id, direction, status, cause)] += 1
                if owner:
                    per_user[(window, account_id, owner, call_type, status, cause)] += 1
                    talk[(window, account_id, owner, call_type)] += billing

        out = []
        out.append("# HELP kazoo_calls_window CDR call legs in the window, all legs (users, trunks, IVR).")
        out.append("# TYPE kazoo_calls_window gauge")
        for (window, a, direction, status, cause), n in sorted(site.items()):
            out.append("kazoo_calls_window%s %d" % (labels(window=window, account=self.accounts.get(a, a),
                       leg_direction=direction, status=status, hangup_cause=cause), n))

        out.append("# HELP kazoo_user_calls_window Call legs per user (agent) in the window.")
        out.append("# TYPE kazoo_user_calls_window gauge")
        for (window, a, owner, call_type, status, cause), n in sorted(per_user.items()):
            name, ext = self.users.get(a, {}).get(owner, (owner, ""))
            out.append("kazoo_user_calls_window%s %d" % (labels(window=window, account=self.accounts.get(a, a),
                       user_id=owner, user=name, extension=ext, call_type=call_type, status=status,
                       hangup_cause=cause), n))

        out.append("# HELP kazoo_user_talk_seconds_window Billed (talk) seconds per user in the window.")
        out.append("# TYPE kazoo_user_talk_seconds_window gauge")
        for (window, a, owner, call_type), n in sorted(talk.items()):
            name, ext = self.users.get(a, {}).get(owner, (owner, ""))
            out.append("kazoo_user_talk_seconds_window%s %d" % (labels(window=window, account=self.accounts.get(a, a),
                       user_id=owner, user=name, extension=ext, call_type=call_type), n))

        out.append("# HELP kazoo_user_calls_total User call legs seen since the exporter started (for rate/increase).")
        out.append("# TYPE kazoo_user_calls_total counter")
        for (a, owner, call_type, status), n in sorted(self.counters.items()):
            name, ext = self.users.get(a, {}).get(owner, (owner, ""))
            out.append("kazoo_user_calls_total%s %d" % (labels(account=self.accounts.get(a, a), user_id=owner,
                       user=name, extension=ext, call_type=call_type, status=status), n))

        out.append("# HELP kazoo_user_talk_seconds_total Billed seconds of user legs seen since the exporter started.")
        out.append("# TYPE kazoo_user_talk_seconds_total counter")
        for (a, owner, call_type), n in sorted(self.talk_counter.items()):
            name, ext = self.users.get(a, {}).get(owner, (owner, ""))
            out.append("kazoo_user_talk_seconds_total%s %d" % (labels(account=self.accounts.get(a, a), user_id=owner,
                       user=name, extension=ext, call_type=call_type), n))

        out.append("# HELP kazoo_calls_last_cdr_timestamp_seconds Hangup time of the newest CDR seen.")
        out.append("# TYPE kazoo_calls_last_cdr_timestamp_seconds gauge")
        for a, ts in sorted(newest.items()):
            out.append("kazoo_calls_last_cdr_timestamp_seconds%s %d" % (labels(account=self.accounts.get(a, a)), ts))
        out.append("# HELP kazoo_calls_window_start_seconds Start of each window (unix time).")
        out.append("# TYPE kazoo_calls_window_start_seconds gauge")
        for window, start in windows.items():
            out.append("kazoo_calls_window_start_seconds%s %d" % (labels(window=window), start))
        out.append("# HELP kazoo_calls_legs_in_memory CDR rows held for the 24 h window.")
        out.append("# TYPE kazoo_calls_legs_in_memory gauge")
        out.append("kazoo_calls_legs_in_memory %d" % len(self.legs))
        return "\n".join(out) + "\n"

    # ---------------------------------------------------------------- RabbitMQ
    @staticmethod
    def queue_family(name):
        f = re.sub(r"<\d+\.\d+\.\d+>", "<pid>", name)
        f = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", f)
        f = re.sub(r"[0-9a-f]{6,}", "<id>", f)
        f = re.sub(r"(?<![a-z])\d{2,}", "<n>", f)
        return f

    def poll_rabbit(self):
        sample = re.compile(r'^(rabbitmq_detailed_queue_(?:messages_ready|messages_unacked|consumers))\{([^}]*)\}\s+(\S+)')
        lab = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
        try:
            base = http_get(RABBIT_URL + "/metrics", timeout=30, accept="text/plain",
                            auth=RABBIT_AUTH).decode("utf-8", "replace")
            detail = http_get(RABBIT_URL + "/metrics/detailed?family=queue_coarse_metrics&family=queue_consumer_count",
                              timeout=60, accept="text/plain", auth=RABBIT_AUTH).decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as err:
            self.errors["rabbitmq"] += 1
            log("rabbitmq: %s" % err)
            self.rabbit_up = 0
            self.rabbit_metrics = ""
            return
        queues = defaultdict(lambda: {"ready": 0.0, "unacked": 0.0, "consumers": 0.0})
        for line in detail.splitlines():
            m = sample.match(line)
            if not m:
                continue
            lbl = dict(lab.findall(m.group(2)))
            q = queues[(lbl.get("vhost", "/"), lbl.get("queue", ""))]
            field = {"rabbitmq_detailed_queue_messages_ready": "ready",
                     "rabbitmq_detailed_queue_messages_unacked": "unacked",
                     "rabbitmq_detailed_queue_consumers": "consumers"}[m.group(1)]
            q[field] = float(m.group(3))

        fam = defaultdict(lambda: defaultdict(float))
        for (vhost, name), q in queues.items():
            f = fam[(vhost, self.queue_family(name))]
            f["queues"] += 1
            f["ready"] += q["ready"]
            f["unacked"] += q["unacked"]
            f["consumers"] += q["consumers"]
            if q["consumers"] == 0:
                f["no_consumer"] += 1
                if q["ready"] > 0:
                    f["stuck"] += 1

        out = [base.rstrip("\n")]
        metrics = [("queues", "Queues in the family"),
                   ("ready", "Messages waiting for a consumer"),
                   ("unacked", "Messages delivered but not acknowledged"),
                   ("consumers", "Consumers on the family's queues"),
                   ("no_consumer", "Queues with no consumer"),
                   ("stuck", "Queues holding messages with no consumer")]
        names = {"queues": "rabbitmq_queue_family_queues", "ready": "rabbitmq_queue_family_messages_ready",
                 "unacked": "rabbitmq_queue_family_messages_unacked", "consumers": "rabbitmq_queue_family_consumers",
                 "no_consumer": "rabbitmq_queue_family_queues_without_consumers",
                 "stuck": "rabbitmq_queue_family_queues_stuck"}
        for key, help_text in metrics:
            out.append("# HELP %s %s." % (names[key], help_text))
            out.append("# TYPE %s gauge" % names[key])
            for (vhost, family), f in sorted(fam.items()):
                out.append("%s%s %g" % (names[key], labels(vhost=vhost, family=family), f[key]))

        top = sorted(queues.items(), key=lambda kv: kv[1]["ready"] + kv[1]["unacked"], reverse=True)[:TOP_QUEUES]
        out.append("# HELP rabbitmq_top_queue_messages Ready + unacked messages in the deepest queues (non-empty only).")
        out.append("# TYPE rabbitmq_top_queue_messages gauge")
        out.append("# HELP rabbitmq_top_queue_consumers Consumers on the deepest queues.")
        out.append("# TYPE rabbitmq_top_queue_consumers gauge")
        for (vhost, name), q in top:
            if q["ready"] + q["unacked"] <= 0:
                continue
            lb = labels(vhost=vhost, queue=name, family=self.queue_family(name))
            out.append("rabbitmq_top_queue_messages%s %g" % (lb, q["ready"] + q["unacked"]))
            out.append("rabbitmq_top_queue_consumers%s %g" % (lb, q["consumers"]))
        self.rabbit_up = 1
        self.rabbit_metrics = "\n".join(out) + "\n"

    # ---------------------------------------------------------------- loop
    def poll_once(self):
        t0 = time.time()
        try:
            if not self.accounts:
                self.load_accounts()
            if time.time() - self.users_loaded > USERS_REFRESH_SECONDS:
                self.load_users()
            added = self.poll_cdrs(t0)
            text = self.render_calls(time.time())
            with self.lock:
                self.call_metrics = text
            self.last_poll_ok = time.time()
            if added:
                log("cdrs: +%d (in window: %d)" % (added, len(self.legs)))
        except (urllib.error.URLError, OSError, ValueError, KeyError) as err:
            self.errors["couchdb"] += 1
            log("couchdb: %s" % err)
        self.poll_rabbit()
        self.last_poll_duration = time.time() - t0

    def run(self):
        while True:
            self.poll_once()
            time.sleep(max(1, POLL_SECONDS - self.last_poll_duration))

    def render(self):
        out = [self.call_metrics, self.rabbit_metrics]
        out.append("# HELP callstats_up 1 when the last CouchDB poll worked within 3 poll intervals.")
        out.append("# TYPE callstats_up gauge")
        out.append("callstats_up %d" % (1 if time.time() - self.last_poll_ok < 3 * POLL_SECONDS + 30 else 0))
        out.append("# HELP callstats_rabbitmq_up 1 when the last RabbitMQ scrape worked.")
        out.append("# TYPE callstats_rabbitmq_up gauge")
        out.append("callstats_rabbitmq_up %d" % self.rabbit_up)
        out.append("# HELP callstats_poll_errors_total Failed polls by source.")
        out.append("# TYPE callstats_poll_errors_total counter")
        for source in ("couchdb", "rabbitmq"):
            out.append("callstats_poll_errors_total%s %d" % (labels(source=source), self.errors[source]))
        out.append("# HELP callstats_poll_duration_seconds Duration of the last poll.")
        out.append("# TYPE callstats_poll_duration_seconds gauge")
        out.append("callstats_poll_duration_seconds %.3f" % self.last_poll_duration)
        out.append("# HELP callstats_last_poll_timestamp_seconds Time of the last successful CouchDB poll.")
        out.append("# TYPE callstats_last_poll_timestamp_seconds gauge")
        out.append("callstats_last_poll_timestamp_seconds %d" % self.last_poll_ok)
        return "".join(x for x in out[:2] if x) + "\n".join(out[2:]) + "\n"


COLLECTOR = Collector()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/metrics", "/"):
            self.send_error(404)
            return
        body = COLLECTOR.render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    if "--once" in sys.argv:  # print one scrape and exit (testing)
        COLLECTOR.poll_once()
        sys.stdout.write(COLLECTOR.render())
        return
    threading.Thread(target=COLLECTOR.run, daemon=True).start()
    host, port = LISTEN.rsplit(":", 1)
    log("listening on %s, couchdb %s, rabbitmq %s" % (LISTEN, COUCH_URL, RABBIT_URL))
    ThreadingHTTPServer((host, int(port)), Handler).serve_forever()


if __name__ == "__main__":
    main()
