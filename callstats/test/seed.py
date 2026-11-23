#!/usr/bin/env python3
"""Local test data for callstats: a fake Kazoo CouchDB and RabbitMQ (WSL only, never a live server).

CouchDB: an "accounts" db, two account dbs with users, monthly CDR dbs with the real
_design/cdrs and _design/users views copied from Meydan (fixtures/). It back-fills 24 h of
CDRs with Meydan's mix of directions / hangup causes, then keeps adding live calls.
RabbitMQ: declares queues named like Meydan's (kazoo_apps@..., goportalbackend-..., ...)
through the management API and keeps changing their depth.
"""

import base64
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

COUCH = os.environ.get("COUCH", "http://callstats-couchdb:5984")
COUCH_AUTH = os.environ.get("COUCH_AUTH", "admin:admin")
RABBIT = os.environ.get("RABBIT", "http://callstats-rabbitmq:15672")
RABBIT_AUTH = os.environ.get("RABBIT_AUTH", "admin:admin")
LEGS_PER_HOUR = int(os.environ.get("LEGS_PER_HOUR", "400"))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
G = 62167219200

ACCOUNTS = {
    "11112222333344445555666677778888": ("meydan-test", 30),
    "aaaabbbbccccddddeeeeffff00001111": ("admin-test", 4),
}

# (direction, hangup_cause, billed, has_owner) -> legs per hour at Meydan, 2026-11-19 13:20 UTC
MIX = [
    ("inbound", "NORMAL_CLEARING", True, False, 177), ("outbound", "NORMAL_CLEARING", True, False, 146),
    ("inbound", "NORMAL_CLEARING", True, True, 135), ("inbound", "ORIGINATOR_CANCEL", False, True, 92),
    ("outbound", "ORIGINATOR_CANCEL", False, False, 90), ("inbound", "NORMAL_UNSPECIFIED", True, False, 56),
    ("outbound", "NORMAL_UNSPECIFIED", True, False, 53), ("outbound", "NORMAL_CLEARING", True, True, 50),
    ("outbound", "USER_BUSY", False, False, 38), ("inbound", "USER_BUSY", False, True, 33),
    ("outbound", "NORMAL_CLEARING", False, False, 22), ("inbound", "NORMAL_CLEARING", False, True, 17),
    ("inbound", "UNALLOCATED_NUMBER", False, True, 10), ("outbound", "USER_BUSY", True, True, 5),
    ("inbound", "NO_ANSWER", False, False, 4), ("outbound", "NO_ANSWER", False, False, 4),
    ("outbound", "NO_USER_RESPONSE", False, True, 4), ("inbound", "LOSE_RACE", False, False, 3),
    ("outbound", "LOSE_RACE", False, True, 3), ("outbound", "CALL_REJECTED", False, True, 3),
    ("outbound", "NO_ANSWER", False, True, 3), ("inbound", "MEDIA_TIMEOUT", True, True, 2),
    ("outbound", "RECOVERY_ON_TIMER_EXPIRE", True, True, 1), ("inbound", "NORMAL_TEMPORARY_FAILURE", False, True, 2),
]
WEIGHTS = [m[4] for m in MIX]


def req(method, url, body=None, auth=COUCH_AUTH, ok=(200, 201, 202, 204, 412)):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    r.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        if err.code in ok:
            return {}
        raise RuntimeError("%s %s -> %s %s" % (method, url, err.code, err.read()[:300]))


def dbname(account_id, month=None):
    db = "account/%s/%s/%s" % (account_id[:2], account_id[2:4], account_id[4:])
    if month:
        db += "-" + month
    return COUCH + "/" + urllib.parse.quote(db, safe="")


def wait_for(url, auth):
    for _ in range(120):
        try:
            req("GET", url, auth=auth)
            return
        except Exception:
            time.sleep(2)
    raise SystemExit("not reachable: " + url)


def design(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


def ensure_db(url, design_doc=None):
    req("PUT", url)
    if design_doc:
        doc = dict(design_doc)
        cur = {}
        try:
            cur = req("GET", url + "/" + doc["_id"])
        except RuntimeError:
            pass
        if cur.get("_rev"):
            doc["_rev"] = cur["_rev"]
        req("PUT", url + "/" + doc["_id"], doc)


def setup_couch():
    wait_for(COUCH + "/", COUCH_AUTH)
    for system_db in ("_users", "_replicator", "accounts"):
        req("PUT", COUCH + "/" + system_db)
    users = {}
    for account_id, (name, n_users) in ACCOUNTS.items():
        req("PUT", COUCH + "/accounts/" + account_id, {"name": name, "pvt_type": "account"})
        ensure_db(dbname(account_id), design("design_users.json"))
        docs = []
        for i in range(1, n_users + 1):
            uid = uuid.uuid5(uuid.NAMESPACE_DNS, "%s-%d" % (account_id, i)).hex
            docs.append({"_id": uid, "pvt_type": "user", "first_name": "Agent", "last_name": "%02d" % i,
                         "username": "agent%02d@%s" % (i, name), "presence_id": str(1000 + i),
                         "priv_level": "user", "email": "agent%02d@example.com" % i})
        existing = {r["id"] for r in req("GET", dbname(account_id) + "/_all_docs").get("rows", [])}
        req("POST", dbname(account_id) + "/_bulk_docs", {"docs": [d for d in docs if d["_id"] not in existing]})
        users[account_id] = [d["_id"] for d in docs]
    return users


def make_cdr(account_users, ts):
    direction, cause, billed, owned, _ = random.choices(MIX, WEIGHTS)[0]
    billing = random.randint(8, 600) if billed else 0
    call_id = str(uuid.uuid4())
    month = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m")
    ccv = {"owner_id": random.choice(account_users)} if owned else {}
    if direction == "inbound" and not owned:
        ccv["inception"] = "+97140000000@du"
    number = "+9715%08d" % random.randint(0, 99999999)
    return month, {
        "_id": "%s-%s" % (month, call_id), "pvt_type": "cdr", "pvt_created": int(ts) + G,
        "timestamp": int(ts) + G, "call_id": call_id, "other_leg_call_id": str(uuid.uuid4()),
        "call_direction": direction, "hangup_cause": cause, "billing_seconds": str(billing),
        "duration_seconds": str(billing + random.randint(3, 30)),
        "caller_id_name": number, "caller_id_number": number, "callee_id_name": "", "callee_id_number": "1001",
        "from_uri": number + "@test", "to_uri": "1001@test", "request": "1001@test",
        "custom_channel_vars": ccv,
    }


def insert(users, cdrs_by_account):
    for account_id, cdrs in cdrs_by_account.items():
        by_month = {}
        for month, doc in cdrs:
            by_month.setdefault(month, []).append(doc)
        for month, docs in by_month.items():
            url = dbname(account_id, month)
            ensure_db(url, design("design_cdrs.json"))
            for i in range(0, len(docs), 1000):
                req("POST", url + "/_bulk_docs", {"docs": docs[i:i + 1000]})


def legs_per_hour(ts):
    # busier 08-20 Dubai time, like a call centre
    hour = (datetime.fromtimestamp(ts, timezone.utc).hour + 4) % 24
    return LEGS_PER_HOUR * (1.0 if 8 <= hour < 20 else 0.25)


def backfill(users):
    now = time.time()
    batch = {}
    t = now - 86400 - 1800
    while t < now:
        t += random.expovariate(legs_per_hour(t) / 3600.0)
        account_id = "11112222333344445555666677778888" if random.random() < 0.9 else "aaaabbbbccccddddeeeeffff00001111"
        batch.setdefault(account_id, []).append(make_cdr(users[account_id], min(t, now)))
    insert(users, batch)
    print("backfilled %d legs" % sum(len(v) for v in batch.values()), flush=True)


QUEUE_NAMES = (
    ["kazoo_apps@my0%d.test-<0.%d.0>-%08x" % (n, random.randint(100, 3000), random.getrandbits(32)) for n in (1, 2, 3, 4) for _ in range(40)]
    + ["ecallmgr@my0%d.test-<0.%d.0>-%08x" % (n, random.randint(100, 3000), random.getrandbits(32)) for n in (1, 2, 3, 4) for _ in range(25)]
    + ["goportalbackend-%s-listener-%s" % (kind, uuid.uuid4()) for kind in ("firehose", "presence", "chat", "registrar") for _ in range(4)]
    + ["conference_controller_%s" % uuid.uuid4().hex for _ in range(10)]
    + ["registrar_listener", "stepswitch_listener", "webhooks_shared_listener", "teletype_shared_listener",
       "callmgr_test_backlog"]
)


def rabbit(method, path, body=None):
    return req(method, RABBIT + path, body, auth=RABBIT_AUTH)


def setup_rabbit():
    wait_for(RABBIT + "/api/overview", RABBIT_AUTH)
    for q in QUEUE_NAMES:
        rabbit("PUT", "/api/queues/%2F/" + urllib.parse.quote(q, safe=""), {"durable": False, "auto_delete": False})
    print("declared %d queues" % len(QUEUE_NAMES), flush=True)


def publish(queue, n):
    for _ in range(n):
        rabbit("POST", "/api/exchanges/%2F/amq.default/publish",
               {"properties": {}, "routing_key": queue, "payload": "{}", "payload_encoding": "string"})


def main():
    random.seed()
    users = setup_couch()
    backfill(users)
    setup_rabbit()
    tick = 0
    while True:
        time.sleep(5)
        tick += 1
        # live calls
        n = random.choices([0, 1, 2, 3], [40, 35, 18, 7])[0] if legs_per_hour(time.time()) > 200 else random.choice([0, 0, 0, 1])
        if n:
            insert(users, {"11112222333344445555666677778888": [make_cdr(users["11112222333344445555666677778888"], time.time()) for _ in range(n)]})
        # RabbitMQ: a backlog that grows for ~2 min and is purged, plus random messages
        if tick % 36 < 24:
            publish("callmgr_test_backlog", random.randint(5, 25))
        elif tick % 36 == 24:
            rabbit("DELETE", "/api/queues/%2F/callmgr_test_backlog/contents")
        if random.random() < 0.3:
            publish(random.choice(QUEUE_NAMES[:160]), random.randint(1, 5))


if __name__ == "__main__":
    main()
