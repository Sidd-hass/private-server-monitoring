#!/usr/bin/env python3
"""SIP carrier peer (IP-authenticated trunk) health as Prometheus metrics.

For the Kazoo sites whose carrier connects by IP (UCPIN, UCDEMO, UCPMED: no trunk container,
so scripts/freeswitch_trunk_metrics.sh does not apply). Per carrier peer it reports:

  * who is blocking whom:
      our end    fail2ban ban, DROP/REJECT ipset or iptables rule, Kamailio secban ipban,
                 the interface towards the peer down, an expected SIP listener missing
      their end  no reply to SIP OPTIONS (when the peer answered before), 401/403/407 to
                 OPTIONS, repeated 401/403/407 or CALL_REJECTED on call legs
  * the route (interface + source IP) to the peer and every SIP listener with its interface
  * whitelist membership: fail2ban ignoreip, allow ipsets, Kamailio trunk_ip, HAProxy whitelist
  * SIP final responses and hangup causes of the peer's call legs (FreeSWITCH log)

Failure lines and block/unblock events go to EVENT_LOG for the Alloy shipper (Loki, the
dashboard's log panels). Nothing on the node is changed: everything is read-only except
the .prom file, EVENT_LOG and STATE_DIR.

Install (one cron line, every minute, as root):
  install -m 0755 sip_peer_metrics.py /usr/local/sbin/sip_peer_metrics.py
  install -m 0644 sip_peers/<site>.conf /etc/sip_peer_metrics.conf
  echo '* * * * * root /usr/local/sbin/sip_peer_metrics.py >/dev/null 2>&1' > /etc/cron.d/sip_peer_metrics
Test without writing anything: sip_peer_metrics.py --stdout
"""
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid

# cron runs with PATH=/usr/bin:/bin; kamcmd, ipset and iptables-save are in /usr/sbin, and a
# missing tool would silently hide that kind of block. Appended, so a PATH given to us wins.
os.environ["PATH"] = os.environ.get("PATH", "") + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

CONF =os.environ.get("SIP_PEERS_CONF", "/etc/sip_peer_metrics.conf")
# node_exporter --collector.textfile.directory: /opt/node-exporter/textfile on UCPIN/UCDEMO,
# /var/lib/node_exporter/textfile on UCPMED. First existing one wins unless OUT_DIR is set.
OUT_DIRS = [os.environ["OUT_DIR"]] if os.environ.get("OUT_DIR") else [
    "/opt/node-exporter/textfile", "/var/lib/node_exporter/textfile"]
STATE_DIR = os.environ.get("STATE_DIR", "/var/lib/sip_peer_metrics")
EVENT_LOG = os.environ.get("EVENT_LOG", "/var/log/sip_peer_metrics.log")
EVENT_LOG_MAX = 20 * 1024 * 1024        # then rotated once to .1
HAPROXY_WHITELIST = os.environ.get("HAPROXY_WHITELIST", "/etc/kazoo/haproxy/whitelist.lst")
MAX_READ = 200 * 1024 * 1024            # log bytes read per run (kazoo-debug.log ~0.5 MB/min)
MAX_EVENT_LINES = 200                   # failure lines copied to EVENT_LOG per run
AUTH_WINDOW = 300                       # seconds of auth failures that count as "rejecting"
AUTH_MIN = int(os.environ.get("AUTH_MIN", "3"))
PROBE_TIMEOUT = 2.0
PROBE_TRIES = 2
STATE_EVENT_EVERY = 3600               # block_state event at least this often (also on change)
PROBE_MEMORY = 7 * 86400               # an OPTIONS timeout counts once the peer answered this recently

AUTH_CODES = {"401", "403", "407"}
AUTH_CAUSES = {"CALL_REJECTED"}          # also 603 Decline; the event line shows the real code
NORMAL_CAUSES = {"NORMAL_CLEARING", "ORIGINATOR_CANCEL", "LOSE_RACE", "PICKED_OFF"}
RE_STATE = re.compile(r"entering state \[(\w+)\]\[(\d{3})\]")
RE_HANGUP = re.compile(r"Hangup sofia/\S+ \[(\w+)\] \[(\w+)\]")
RE_AUTH_TEXT = re.compile(r"(?i)\b(unauthori[sz]ed|forbidden|proxy authentication required)\b")
SKIP_IFACES = re.compile(r"^(lo|docker\d*|veth.*|br-.*|virbr.*)$")


def run(*cmd, timeout=10):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout, text=True)
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def labels(**kv):
    return "{" + ",".join('%s="%s"' % (k, esc(v)) for k, v in kv.items()) + "}"


def in_net(ip, entry):
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(entry, strict=False)
    except ValueError:
        return False


# ------------------------------------------------------------------ config

def load_conf(path):
    """peer <carrier> <ip> <port> [options|none] / listen <process> <port> [udp|tcp] / log <path>"""
    peers, listens, logs = [], [], []
    with open(path) as f:
        for raw in f:
            w = raw.split("#", 1)[0].split()
            if not w:
                continue
            if w[0] == "peer" and len(w) >= 4:
                peers.append({"carrier": w[1], "ip": w[2], "port": w[3],
                              "probe": (w[4] if len(w) > 4 else "options") == "options"})
            elif w[0] == "listen" and len(w) >= 3:
                listens.append({"process": w[1], "port": w[2], "proto": w[3] if len(w) > 3 else "udp"})
            elif w[0] == "log" and len(w) >= 2:
                logs.append(w[1])
    return peers, listens, logs


def load_state():
    try:
        with open(os.path.join(STATE_DIR, "state.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = os.path.join(STATE_DIR, "state.json.tmp")
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, os.path.join(STATE_DIR, "state.json"))


# ------------------------------------------------------------------ network

def interfaces():
    """{iface: {"up": 0/1, "operstate": str, "addrs": [ip, ...]}} for real interfaces."""
    out = {}
    _, txt = run("ip", "-o", "-4", "addr", "show")
    addrs = {}
    for line in txt.splitlines():
        w = line.split()
        if len(w) >= 4:
            addrs.setdefault(w[1].split("@")[0], []).append(w[3].split("/")[0])
    try:
        names = os.listdir("/sys/class/net")
    except OSError:
        names = list(addrs)
    for n in names:
        if SKIP_IFACES.match(n):
            continue
        oper = read("/sys/class/net/%s/operstate" % n).strip() or "unknown"
        carrier = read("/sys/class/net/%s/carrier" % n).strip()
        # virtio NICs can report "unknown"; the carrier bit is authoritative then.
        up = 1 if (oper == "up" or (oper == "unknown" and carrier == "1")) and carrier != "0" else 0
        out[n] = {"up": up, "operstate": oper, "addrs": addrs.get(n, [])}
    return out


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def route_to(ip):
    _, txt = run("ip", "-o", "route", "get", ip)
    dev = re.search(r"\bdev (\S+)", txt)
    src = re.search(r"\bsrc (\S+)", txt)
    return (dev.group(1) if dev else ""), (src.group(1) if src else "")


def listeners(ifaces):
    """[{process, proto, address, port, iface}] for every listening socket with a process name."""
    by_ip = {a: n for n, i in ifaces.items() for a in i["addrs"]}
    by_ip.update({"127.0.0.1": "lo"})
    seen, out = set(), []
    for proto, flag in (("udp", "-lunpH"), ("tcp", "-ltnpH")):
        _, txt = run("ss", flag)
        for line in txt.splitlines():
            w = line.split()
            if len(w) < 6:
                continue
            m = re.search(r'users:\(\("([^"]+)"', line)
            if not m:
                continue
            local = w[3]
            addr, _, port = local.rpartition(":")
            addr = addr.strip("[]").split("%")[0]
            iface = "all" if addr in ("0.0.0.0", "*", "::") else by_ip.get(addr, "unknown")
            key = (m.group(1), proto, addr, port)
            if key in seen:
                continue
            seen.add(key)
            out.append({"process": m.group(1), "proto": proto, "address": addr,
                        "port": port, "iface": iface})
    return out


def sip_options(peer, src_ip):
    """Send SIP OPTIONS from src_ip; return (status code or 0 on timeout, rtt seconds)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((src_ip or "0.0.0.0", 0))
        lport = s.getsockname()[1]
        lip = src_ip or s.getsockname()[0]
        for _ in range(PROBE_TRIES):
            branch = "z9hG4bK" + uuid.uuid4().hex[:16]
            callid = uuid.uuid4().hex
            msg = ("OPTIONS sip:%(ip)s:%(port)s SIP/2.0\r\n"
                   "Via: SIP/2.0/UDP %(lip)s:%(lport)d;branch=%(br)s;rport\r\n"
                   "Max-Forwards: 70\r\n"
                   "From: <sip:monitor@%(lip)s>;tag=%(tag)s\r\n"
                   "To: <sip:%(ip)s:%(port)s>\r\n"
                   "Call-ID: %(cid)s@%(lip)s\r\n"
                   "CSeq: 1 OPTIONS\r\n"
                   "Contact: <sip:monitor@%(lip)s:%(lport)d>\r\n"
                   "Accept: application/sdp\r\n"
                   "User-Agent: sip-peer-monitor\r\n"
                   "Content-Length: 0\r\n\r\n") % {
                "ip": peer["ip"], "port": peer["port"], "lip": lip, "lport": lport,
                "br": branch, "tag": uuid.uuid4().hex[:8], "cid": callid}
            t0 = time.monotonic()
            s.sendto(msg.encode(), (peer["ip"], int(peer["port"])))
            deadline = t0 + PROBE_TIMEOUT
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                s.settimeout(left)
                try:
                    data, _ = s.recvfrom(65535)
                except socket.timeout:
                    break
                except OSError:          # ICMP port unreachable surfaces here on Linux
                    return -1, time.monotonic() - t0
                txt = data.decode("utf-8", "replace")
                m = re.match(r"SIP/2\.0 (\d{3})", txt)
                if m and callid in txt and m.group(1) != "100":
                    return int(m.group(1)), time.monotonic() - t0
        return 0, 0.0
    except OSError:
        return -1, 0.0
    finally:
        s.close()


# ------------------------------------------------------------------ local blocking

def firewall():
    """(ipset members {set: [entries]}, ipset actions {set: (chain, target)}, iptables rules)."""
    members, cur = {}, None
    _, txt = run("ipset", "save")
    for line in txt.splitlines():
        w = line.split()
        if len(w) >= 3 and w[0] == "create":
            members.setdefault(w[1], [])
        elif len(w) >= 3 and w[0] == "add":
            members.setdefault(w[1], []).append(w[2])
    actions, rules = {}, []
    _, txt = run("iptables-save", timeout=15)
    for line in txt.splitlines():
        if not line.startswith("-A "):
            continue
        chain = line.split()[1]
        tgt = re.search(r" -j (\S+)", line)
        tgt = tgt.group(1) if tgt else ""
        ms = re.search(r"--match-set (\S+) src", line)
        if ms:
            actions.setdefault(ms.group(1), (chain, tgt))
        src = re.search(r" -s (\S+)", line)
        if src and tgt in ("DROP", "REJECT"):
            rules.append((chain, src.group(1), tgt))
    return members, actions, rules


def fail2ban():
    """{jail: {"banned": [ip], "ignore": [cidr]}}"""
    out = {}
    rc, txt = run("fail2ban-client", "status", timeout=15)
    if rc != 0:
        return out
    m = re.search(r"Jail list:\s*(.*)", txt)
    for jail in [j.strip() for j in (m.group(1).split(",") if m else []) if j.strip()]:
        _, st = run("fail2ban-client", "status", jail, timeout=15)
        b = re.search(r"Banned IP list:\s*(.*)", st)
        _, ig = run("fail2ban-client", "get", jail, "ignoreip", timeout=15)
        ignore = re.findall(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(?:/[0-9]+)?", ig)
        out[jail] = {"banned": b.group(1).split() if b else [], "ignore": ignore}
    return out


def kamailio_has(table, ip):
    rc, txt = run("kamcmd", "htable.get", table, ip, timeout=5)
    if rc == 127:
        return None
    return 1 if rc == 0 and "item" in txt else 0


# ------------------------------------------------------------------ logs

def scan_logs(paths, peers, st, events):
    """Count SIP responses / hangup causes of the peers' call legs since the last run."""
    pos = st.setdefault("logpos", {})
    cnt = st.setdefault("counters", {})
    tokens = {p["carrier"]: ["@%s:%s" % (p["ip"], p["port"]), "@%s " % p["ip"], "@%s>" % p["ip"]]
              for p in peers}
    now = time.time()
    auth_now = {p["carrier"]: 0 for p in peers}
    for path in paths:
        try:
            fst = os.stat(path)
        except OSError:
            continue
        key = path
        prev = pos.get(key)
        if prev is None or prev.get("ino") != fst.st_ino or prev.get("off", 0) > fst.st_size:
            # First run starts at the end; a rotated/truncated file is read from the start.
            off = fst.st_size if prev is None else 0
        else:
            off = prev["off"]
        if fst.st_size - off > MAX_READ:
            off = fst.st_size - MAX_READ
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read(fst.st_size - off)
        # Only whole lines; the tail is read next time.
        cut = data.rfind(b"\n") + 1
        pos[key] = {"ino": fst.st_ino, "off": off + cut}
        for raw in data[:cut].splitlines():
            line = raw.decode("utf-8", "replace")
            for carrier, toks in tokens.items():
                if not any(t in line for t in toks):
                    continue
                c = cnt.setdefault(carrier, {"responses": {}, "hangups": {}, "auth": {}, "lines": 0})
                c["lines"] += 1
                st.setdefault("last_seen", {})[carrier] = now
                # One leg logs both its final response and its hangup cause; auth failures are
                # counted once per leg, on the hangup (FreeSWITCH maps 401/403/407 to CALL_REJECTED).
                fail, auth = None, None
                ms = RE_STATE.search(line)
                if ms and ms.group(1) == "terminated":
                    code = ms.group(2)
                    c["responses"][code] = c["responses"].get(code, 0) + 1
                    if code >= "300":
                        fail = "sip_%s" % code
                mh = RE_HANGUP.search(line)
                if mh:
                    cause = mh.group(2)
                    c["hangups"][cause] = c["hangups"].get(cause, 0) + 1
                    if cause not in NORMAL_CAUSES:
                        fail = cause
                    if cause in AUTH_CAUSES:
                        auth = cause
                if not ms and not mh and RE_AUTH_TEXT.search(line):
                    auth = fail = "auth_text"
                if fail and fail[4:] in AUTH_CODES:
                    auth_line = True
                else:
                    auth_line = bool(auth)
                if auth:
                    c["auth"][auth] = c["auth"].get(auth, 0) + 1
                    auth_now[carrier] += 1
                if fail and len(events) < MAX_EVENT_LINES:
                    events.append("level=%s event=call_failure carrier=%s reason=%s auth=%s line=%s" % (
                        "error" if auth_line else "warning", carrier, fail, "yes" if auth_line else "no",
                        line.strip()[:600]))
    # Auth failures per run, for the "rejecting in the last AUTH_WINDOW seconds" decision.
    hist = st.setdefault("auth_hist", {})
    for carrier, n in auth_now.items():
        h = [x for x in hist.get(carrier, []) if now - x[0] <= AUTH_WINDOW]
        if n:
            h.append([now, n])
        hist[carrier] = h
    return {c: sum(x[1] for x in hist.get(c, [])) for c in auth_now}


def write_events(lines):
    if not lines:
        return
    try:
        if os.path.exists(EVENT_LOG) and os.path.getsize(EVENT_LOG) > EVENT_LOG_MAX:
            os.replace(EVENT_LOG, EVENT_LOG + ".1")
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(EVENT_LOG, "a") as f:
            for l in lines:
                f.write("%s %s\n" % (ts, l))
    except OSError:
        pass


# ------------------------------------------------------------------ main

def gen(dry):
    t0 = time.time()
    peers, expect, logs = load_conf(CONF)
    st = load_state()
    events = []
    ifaces = interfaces()
    lst = listeners(ifaces)
    members, actions, rules = firewall()
    f2b = fail2ban()
    haproxy = [l.strip() for l in read(HAPROXY_WHITELIST).splitlines() if l.strip()]
    auth_recent = scan_logs(logs, peers, st, events)
    m = {}   # metric name -> [sample lines]

    def put(name, lab, val):
        m.setdefault(name, []).append("%s%s %s" % (name, lab, val))

    for n, i in sorted(ifaces.items()):
        put("sip_iface_up", labels(iface=n, operstate=i["operstate"],
                                   address=",".join(i["addrs"]) or "none"), i["up"])
    for l in lst:
        put("sip_listener_info", labels(process=l["process"], proto=l["proto"], address=l["address"],
                                        port=l["port"], iface=l["iface"]), 1)
    for e in expect:
        ok = [l for l in lst if l["process"].startswith(e["process"][:15]) and l["port"] == e["port"]
              and l["proto"] == e["proto"]]
        put("sip_listener_expected_up", labels(process=e["process"], proto=e["proto"], port=e["port"],
                                               iface=",".join(sorted({l["iface"] for l in ok})) or "none"),
            1 if ok else 0)

    prev_blocks = st.get("blocks", {})
    blocks_now = {}
    probe_hist = st.setdefault("probe_ok_at", {})
    for p in peers:
        car, ip = p["carrier"], p["ip"]
        peer = "%s:%s" % (ip, p["port"])
        base = dict(carrier=car, peer=peer)
        dev, src = route_to(ip)
        iface_up = ifaces.get(dev, {}).get("up", 0) if dev else 0
        put("sip_peer_info", labels(carrier=car, peer=peer, iface=dev or "none", src_ip=src or "none"), 1)
        put("sip_peer_iface_up", labels(carrier=car, peer=peer, iface=dev or "none"), iface_up)
        local, remote = [], []
        if not iface_up:
            local.append("iface_down:%s" % (dev or "no_route"))
        for e in expect:
            bound = any(l["process"].startswith(e["process"][:15]) and l["port"] == e["port"]
                        and l["proto"] == e["proto"] and l["address"] in (src, "0.0.0.0", "*", "::")
                        for l in lst)
            put("sip_peer_listener_up", labels(carrier=car, peer=peer, process=e["process"],
                                               port=e["port"], address=src or "none", iface=dev or "none"),
                1 if bound else 0)
            if not bound:
                local.append("listener_missing:%s:%s" % (e["process"], e["port"]))

        # ---- our end: firewall, fail2ban, Kamailio secban
        for s, ents in sorted(members.items()):
            if not any(in_net(ip, x) for x in ents):
                continue
            chain, tgt = actions.get(s, ("", "unused"))
            put("sip_peer_ipset_member", labels(carrier=car, peer=peer, set=s, chain=chain or "none",
                                                action=tgt or "none"), 1)
            if tgt in ("DROP", "REJECT"):
                local.append("ipset:%s" % s)
        for chain, cidr, tgt in rules:
            if in_net(ip, cidr):
                local.append("iptables:%s:%s" % (chain, tgt))
        for jail, j in sorted(f2b.items()):
            if "ssh" in jail:        # SSH jails never see SIP traffic; their whitelist is irrelevant
                if ip in j["banned"]:
                    local.append("fail2ban:%s" % jail)
                continue
            banned = 1 if ip in j["banned"] else 0
            ign = 1 if any(in_net(ip, x) for x in j["ignore"]) else 0
            put("sip_peer_fail2ban_banned", labels(carrier=car, peer=peer, jail=jail), banned)
            put("sip_peer_whitelisted", labels(carrier=car, peer=peer, list="fail2ban:%s" % jail), ign)
            if banned:
                local.append("fail2ban:%s" % jail)
        for table in ("ipban", "trunk_ip"):
            v = kamailio_has(table, ip)
            if v is None:
                continue
            if table == "ipban":
                put("sip_peer_kamailio_banned", labels(carrier=car, peer=peer), v)
                if v:
                    local.append("kamailio:ipban")
            else:
                put("sip_peer_whitelisted", labels(carrier=car, peer=peer, list="kamailio:trunk_ip"), v)
        for s in ("trunk_ip", "trunk_net", "whitelist_ip", "whitelist_net"):
            if s in members:
                put("sip_peer_whitelisted", labels(carrier=car, peer=peer, list="ipset:%s" % s),
                    1 if any(in_net(ip, x) for x in members[s]) else 0)
        if os.path.exists(HAPROXY_WHITELIST):
            put("sip_peer_whitelisted", labels(carrier=car, peer=peer, list="haproxy:whitelist.lst"),
                1 if any(in_net(ip, x) for x in haproxy) else 0)

        # ---- their end: OPTIONS probe and auth rejections on call legs
        if p["probe"] and iface_up and not local:
            code, rtt = sip_options(p, src)
            if code > 0:
                probe_hist[car] = time.time()
            answered_before = time.time() - probe_hist.get(car, 0) <= PROBE_MEMORY
            put("sip_peer_options_status", labels(**base), code)
            put("sip_peer_options_up", labels(**base), 1 if code > 0 and str(code) not in AUTH_CODES else 0)
            put("sip_peer_options_rtt_seconds", labels(**base), "%.4f" % rtt)
            put("sip_peer_options_answered_recently", labels(**base), 1 if answered_before else 0)
            if code == -1:
                remote.append("icmp_unreachable")
            elif code == 0 and answered_before:
                remote.append("options_timeout")
            elif str(code) in AUTH_CODES:
                remote.append("options_%d" % code)
        if auth_recent.get(car, 0) >= AUTH_MIN:
            remote.append("auth_failures")
        put("sip_peer_auth_failures_recent", labels(**base), auth_recent.get(car, 0))

        blocked_by = "local" if local else ("remote" if remote else "none")
        reasons = local or remote or ["none"]
        put("sip_peer_block_state", labels(**base), {"none": 0, "remote": 1, "local": 2}[blocked_by])
        for r in reasons:
            put("sip_peer_blocked", labels(carrier=car, peer=peer, blocked_by=blocked_by, reason=r),
                0 if blocked_by == "none" else 1)
        blocks_now[car] = {"by": blocked_by, "reasons": reasons}
        old = prev_blocks.get(car)
        # On every change, and hourly so the dashboard's event panel always shows the state.
        last_ev = st.setdefault("state_event_at", {})
        if old != blocks_now[car] or time.time() - last_ev.get(car, 0) >= STATE_EVENT_EVERY:
            last_ev[car] = time.time()
            events.append("level=%s event=block_state carrier=%s peer=%s blocked_by=%s reason=%s iface=%s src=%s" % (
                "info" if blocked_by == "none" else "error", car, peer, blocked_by, ",".join(reasons),
                dev or "none", src or "none"))

        c = st.get("counters", {}).get(car, {})
        for code, v in sorted(c.get("responses", {}).items()):
            put("sip_peer_final_responses_total", labels(carrier=car, peer=peer, code=code), v)
        for cause, v in sorted(c.get("hangups", {}).items()):
            put("sip_peer_hangups_total", labels(carrier=car, peer=peer, cause=cause), v)
        for r, v in sorted(c.get("auth", {}).items()):
            put("sip_peer_auth_failures_total", labels(carrier=car, peer=peer, reason=r), v)
        put("sip_peer_log_lines_total", labels(**base), c.get("lines", 0))
        put("sip_peer_last_seen_timestamp_seconds", labels(**base), int(st.get("last_seen", {}).get(car, 0)))

    st["blocks"] = blocks_now
    put("sip_peer_metrics_duration_seconds", "", "%.3f" % (time.time() - t0))
    put("sip_peer_metrics_last_run_timestamp_seconds", "", int(time.time()))

    helps = {
        "sip_iface_up": ("gauge", "1 if the interface is up (operstate up, carrier present)"),
        "sip_listener_info": ("gauge", "Listening socket with its process and interface"),
        "sip_listener_expected_up": ("gauge", "1 if an expected SIP listener (config 'listen') is bound"),
        "sip_peer_info": ("gauge", "Route to the carrier peer: interface and source IP"),
        "sip_peer_iface_up": ("gauge", "1 if the interface that routes to the peer is up"),
        "sip_peer_listener_up": ("gauge", "1 if the expected listener is bound on the peer-facing IP"),
        "sip_peer_ipset_member": ("gauge", "Peer IP is a member of this ipset (action = iptables target)"),
        "sip_peer_fail2ban_banned": ("gauge", "1 if fail2ban has the peer IP banned in this jail"),
        "sip_peer_whitelisted": ("gauge", "1 if the peer IP is on this allow/whitelist"),
        "sip_peer_kamailio_banned": ("gauge", "1 if Kamailio secban has the peer IP in ipban"),
        "sip_peer_options_status": ("gauge", "SIP status code to OPTIONS; 0 = no reply, -1 = ICMP unreachable"),
        "sip_peer_options_up": ("gauge", "1 if the peer answered OPTIONS (not 401/403/407)"),
        "sip_peer_options_rtt_seconds": ("gauge", "OPTIONS round trip time"),
        "sip_peer_options_answered_recently": ("gauge", "1 if the peer answered OPTIONS in the last 7 days"),
        "sip_peer_auth_failures_recent": ("gauge", "401/403/407/CALL_REJECTED on call legs in the last 5 minutes"),
        "sip_peer_block_state": ("gauge", "0 = not blocked, 1 = blocked at the carrier end, 2 = blocked at our end"),
        "sip_peer_blocked": ("gauge", "1 per active block reason; blocked_by = local (our end) or remote (carrier end)"),
        "sip_peer_final_responses_total": ("counter", "Final SIP responses on the peer's call legs (FreeSWITCH log)"),
        "sip_peer_hangups_total": ("counter", "Hangup causes of the peer's call legs (FreeSWITCH log)"),
        "sip_peer_auth_failures_total": ("counter", "401/403/407/CALL_REJECTED on the peer's call legs"),
        "sip_peer_log_lines_total": ("counter", "Log lines that mention the peer"),
        "sip_peer_last_seen_timestamp_seconds": ("gauge", "Last time a log line mentioned the peer"),
        "sip_peer_metrics_duration_seconds": ("gauge", "Run time of sip_peer_metrics.py"),
        "sip_peer_metrics_last_run_timestamp_seconds": ("gauge", "Unix time of the last run"),
    }
    out = []
    for name, rows in m.items():
        t, h = helps.get(name, ("gauge", name))
        out += ["# HELP %s %s" % (name, h), "# TYPE %s %s" % (name, t)] + rows
    if not dry:
        save_state(st)
        write_events(events)
    return "\n".join(out) + "\n", events


def main():
    dry = "--stdout" in sys.argv
    text, events = gen(dry)
    if dry:
        sys.stdout.write(text)
        for e in events:
            sys.stderr.write("event: %s\n" % e)
        return 0
    out_dir = next((d for d in OUT_DIRS if os.path.isdir(d)), OUT_DIRS[0])
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, "sip_peer.prom.tmp")
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, os.path.join(out_dir, "sip_peer.prom"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
