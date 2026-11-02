#!/usr/bin/env python3
"""VoIP agent for a trunk node: live capture into RAM, call tracking, audio on demand.

- Captures IFACE with a raw AF_PACKET socket. Nothing is written to disk.
- RTP packets go into a byte-capped RAM ring (RING_MB); SIP drives the call table.
- HTTP API for voip-gateway on 244 (token protected): call lists, call details,
  rebuilt WAV of a call, and live audio over WebSocket.
- /metrics for Prometheus; finished calls and SIP messages are pushed to Loki.
"""
import asyncio
import hmac
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.request
from collections import deque

from aiohttp import WSMsgType, web

import voipcore


def env(name, default=None, required=False):
    value = os.environ.get(name, "").strip()
    if not value:
        if required:
            sys.exit(f"{name} is required")
        return default
    return value


IFACE = env("IFACE", required=True)
CLUSTER = env("CLUSTER", required=True)
SERVER_NAME = env("SERVER_NAME", required=True)
LISTEN = env("LISTEN", "0.0.0.0:9479")
AGENT_TOKEN = env("AGENT_TOKEN", required=True)
RING_MB = int(env("RING_MB", "1024"))
SIP_PORTS = tuple(int(p) for p in env("SIP_PORTS", "5060").split(","))
RECENT_CALLS = int(env("RECENT_CALLS", "2000"))
LOKI_URL = env("LOKI_URL", "")
LOG_OPTIONS = env("LOG_OPTIONS", "false").lower() == "true"


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, flush=True)


def iface_ip(name):
    import fcntl
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        return socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))[20:24])
    finally:
        s.close()


# ---------------------------------------------------------------- metrics

METRICS = {
    "voip_sip_messages_total": ("counter", "SIP messages by direction (in/out), CSeq method and response code ('request' for requests)."),
    "voip_calls_total": ("counter", "Finished call attempts (initial INVITEs) by direction, result (answered, failed, cancelled) and final code."),
    "voip_call_pdd_seconds_sum": ("counter", "Sum of post-dial delay (initial INVITE to first 180/183/200)."),
    "voip_call_pdd_seconds_count": ("counter", "Calls counted in voip_call_pdd_seconds_sum."),
    "voip_call_duration_seconds_sum": ("counter", "Sum of answered call durations."),
    "voip_call_duration_seconds_count": ("counter", "Calls counted in voip_call_duration_seconds_sum."),
    "voip_calls_active": ("gauge", "Calls in progress by direction and state (ringing, answered)."),
    "voip_call_start_timestamp_seconds": ("gauge", "One series per call in progress (start time); feeds the Grafana live-calls table."),
    "voip_rtp_packets_total": ("counter", "RTP packets by direction (in = to us, out = to the far end)."),
    "voip_rtp_lost_packets_total": ("counter", "RTP packets lost (sequence gaps) by direction."),
    "voip_rtp_jitter_ms": ("gauge", "Interarrival jitter (RFC 3550) over active streams: mean and max."),
    "voip_rtp_streams_active": ("gauge", "RTP streams that received packets in the last 10 s."),
    "voip_bytes_total": ("counter", "Captured bytes by protocol (sip, rtp, other)."),
    "voip_capture_packets_total": ("counter", "Packets read from the capture socket."),
    "voip_capture_drops_total": ("counter", "Packets the kernel dropped because the agent did not read them fast enough."),
    "voip_parse_errors_total": ("counter", "Packets that failed to parse."),
    "voip_ring_bytes": ("gauge", "Bytes held in the RTP ring buffer (RAM)."),
    "voip_ring_capacity_bytes": ("gauge", "RTP ring buffer cap (RING_MB)."),
    "voip_ring_window_seconds": ("gauge", "Age of the oldest RTP packet still in RAM: how far back calls can be replayed."),
    "voip_live_listeners": ("gauge", "Live audio listeners connected."),
    "voip_audio_requests_total": ("counter", "Audio rebuild requests by result (ok, not_in_ring, unknown_call)."),
    "voip_loki_push_errors_total": ("counter", "Failed Loki pushes."),
    "voip_pcap_requests_total": ("counter", "pcap exports served."),
    "voip_agent_up": ("gauge", "1 while the capture thread is running."),
}


class Registry:
    def __init__(self):
        self.lock = threading.Lock()
        self.values = {name: {} for name in METRICS}

    def inc(self, name, labels=None, value=1.0):
        key = tuple(sorted((labels or {}).items()))
        with self.lock:
            self.values[name][key] = self.values[name].get(key, 0.0) + value

    def set(self, name, labels=None, value=0.0):
        with self.lock:
            self.values[name][tuple(sorted((labels or {}).items()))] = float(value)

    def replace(self, name, series):
        with self.lock:
            self.values[name] = {tuple(sorted(l.items())): float(v) for l, v in series}

    def render(self):
        out = []
        with self.lock:
            for name, (kind, help_text) in METRICS.items():
                out += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
                for key, value in self.values[name].items():
                    labels = ",".join('%s="%s"' % (k, str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")) for k, v in key)
                    out.append(f"{name}{{{labels}}} {value!r}" if labels else f"{name} {value!r}")
        return "\n".join(out) + "\n"


REG = Registry()

# ---------------------------------------------------------------- Loki

LOKI_QUEUE = deque(maxlen=50000)   # (kind, ts, line)


def loki_flush():
    if not LOKI_URL or not LOKI_QUEUE:
        return
    batch = []
    while LOKI_QUEUE and len(batch) < 5000:
        batch.append(LOKI_QUEUE.popleft())
    streams = {}
    for kind, ts, line in batch:
        streams.setdefault(kind, []).append([str(int(ts * 1e9)), json.dumps(line, separators=(",", ":"))])
    payload = {"streams": [{"stream": {"job": "voip", "cluster": CLUSTER, "server_name": SERVER_NAME, "kind": kind},
                            "values": sorted(values)} for kind, values in streams.items()]}
    try:
        req = urllib.request.Request(LOKI_URL, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        REG.inc("voip_loki_push_errors_total")
        log(f"loki push failed ({len(batch)} lines): {e}")


# ---------------------------------------------------------------- engine + capture

LOCK = threading.RLock()     # guards the engine (capture thread vs API/periodic readers)
LOCAL_IP = env("LOCAL_IP") or iface_ip(IFACE)


def on_finish(call):
    s = voipcore.summary(call)
    labels = {"direction": s["direction"], "result": s["result"], "code": s["code"]}
    REG.inc("voip_calls_total", labels)
    if s["pdd_s"] is not None:
        REG.inc("voip_call_pdd_seconds_sum", {"direction": s["direction"]}, s["pdd_s"])
        REG.inc("voip_call_pdd_seconds_count", {"direction": s["direction"]})
    if s["result"] == "answered":
        REG.inc("voip_call_duration_seconds_sum", {"direction": s["direction"]}, s["duration_s"])
        REG.inc("voip_call_duration_seconds_count", {"direction": s["direction"]})
    line = dict(s, server_name=SERVER_NAME, cluster=CLUSTER,
                start=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(s["start"])))
    for k in ("answered", "end"):
        line.pop(k, None)
    LOKI_QUEUE.append(("call", call["end"], line))


def on_sip_message(ts, src, sport, dst, dport, msg):
    if not LOG_OPTIONS and (msg["cseq_method"] or msg["method"]) == "OPTIONS":
        return
    LOKI_QUEUE.append(("sip", ts, {
        "dir": "out" if src == LOCAL_IP else "in", "src": f"{src}:{sport}", "dst": f"{dst}:{dport}",
        "method": msg["method"], "code": msg["code"], "cseq": msg["cseq_method"], "call_id": msg["call_id"],
        "from": msg["from"], "to": msg["to"], "reason": msg["reason"], "ua": msg["ua"],
        "sdp": "%s:%s" % msg["sdp"][:2] if msg["sdp"] else ""}))


ENGINE = voipcore.Engine(LOCAL_IP, RING_MB * 1024 * 1024, sip_ports=SIP_PORTS,
                         recent_max=RECENT_CALLS, on_finish=on_finish, metrics=REG)
ENGINE.on_sip_message = on_sip_message
CAPTURE_SOCK = None


def capture_loop():
    global CAPTURE_SOCK
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
    try:
        sock.setsockopt(socket.SOL_SOCKET, 33, 64 * 1024 * 1024)      # SO_RCVBUFFORCE (CAP_NET_ADMIN)
    except OSError:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
    sock.bind((IFACE, 0))
    CAPTURE_SOCK = sock
    REG.set("voip_agent_up", None, 1)
    log(f"capturing on {IFACE} ({LOCAL_IP}), ring {RING_MB} MB, SIP ports {SIP_PORTS}")
    while True:
        data = sock.recv(65535)
        ts = time.time()
        REG.inc("voip_capture_packets_total")
        try:
            etype, off = struct.unpack_from("!H", data, 12)[0], 14
            while etype == 0x8100 and len(data) >= off + 4:
                etype, off = struct.unpack_from("!H", data, off + 2)[0], off + 4
            if etype == 0x0800:
                with LOCK:
                    ENGINE.ipv4(data[off:], ts)
        except Exception:
            REG.inc("voip_parse_errors_total")


def replay_loop(path):
    """Test mode (REPLAY_PCAP): feed a pcap at real speed, shifted to the current time."""
    REG.set("voip_agent_up", None, 1)
    log(f"replaying {path} as if captured now (LOCAL_IP {LOCAL_IP})")
    start, first = time.time(), None
    for ts, ip in voipcore.read_pcap(path):
        first = ts if first is None else first
        delay = start + (ts - first) - time.time()
        if delay > 0:
            time.sleep(delay)
        REG.inc("voip_capture_packets_total")
        with LOCK:
            ENGINE.ipv4(ip, start + (ts - first))
    log("replay finished")
    while True:
        time.sleep(3600)


def capture_thread():
    try:
        if env("REPLAY_PCAP"):
            replay_loop(env("REPLAY_PCAP"))
        capture_loop()
    except Exception as e:
        log(f"capture stopped: {e}")
    REG.set("voip_agent_up", None, 0)
    os._exit(1)       # let Docker restart the container


def read_drops():
    if CAPTURE_SOCK is None:
        return
    try:
        _packets, drops = struct.unpack("II", CAPTURE_SOCK.getsockopt(263, 6, 8))   # SOL_PACKET, PACKET_STATISTICS
        if drops:
            REG.inc("voip_capture_drops_total", None, drops)
    except OSError:
        pass


async def periodic(app):
    loop = asyncio.get_running_loop()
    while True:
        now = time.time()
        with LOCK:
            ENGINE.expire(now)
            mean_j, max_j = ENGINE.stream_jitter(now)
            active_streams = sum(1 for st in ENGINE.streams.values() if now - st.last_ts <= 10)
            live = ENGINE.tracker.snapshot("live")
        REG.set("voip_rtp_jitter_ms", {"stat": "mean"}, mean_j)
        REG.set("voip_rtp_jitter_ms", {"stat": "max"}, max_j)
        REG.set("voip_rtp_streams_active", None, active_streams)
        counts = {}
        for c in live:
            counts[(c["direction"], c["state"])] = counts.get((c["direction"], c["state"]), 0) + 1
        REG.replace("voip_calls_active", [({"direction": d, "state": s}, counts.get((d, s), 0))
                                          for d in ("inbound", "outbound") for s in ("ringing", "answered")])
        REG.replace("voip_call_start_timestamp_seconds", [(
            {"call_id": c["call_id"], "direction": c["direction"], "from": c["from"], "to": c["to"], "state": c["state"]},
            c["start"]) for c in live])
        REG.set("voip_ring_bytes", None, ENGINE.ring.bytes)
        REG.set("voip_ring_capacity_bytes", None, ENGINE.ring.cap)
        REG.set("voip_ring_window_seconds", None, ENGINE.ring.window_seconds(now))
        REG.set("voip_live_listeners", None, ENGINE.ring.listener_count())
        read_drops()
        await loop.run_in_executor(None, loki_flush)
        await asyncio.sleep(5)


# ---------------------------------------------------------------- HTTP API

@web.middleware
async def auth(request, handler):
    if request.path.startswith("/api/"):
        token = request.headers.get("X-Agent-Token", "")
        if not hmac.compare_digest(token, AGENT_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


async def metrics(_request):
    return web.Response(text=REG.render(), content_type="text/plain")


async def status(_request):
    now = time.time()
    with LOCK:
        active = len(ENGINE.tracker.active)
    return web.json_response({
        "cluster": CLUSTER, "server_name": SERVER_NAME, "iface": IFACE, "local_ip": LOCAL_IP,
        "ring_bytes": ENGINE.ring.bytes, "ring_capacity_bytes": ENGINE.ring.cap,
        "ring_window_s": round(ENGINE.ring.window_seconds(now), 1), "active_calls": active})


async def calls(request):
    which = "live" if request.query.get("state") == "live" else "recent"
    limit = min(int(request.query.get("limit", "200")), 2000)
    with LOCK:
        data = ENGINE.tracker.snapshot(which, limit)
    oldest = ENGINE.ring.oldest_ts()
    evicted = ENGINE.ring.evicted_ts
    for c in data:
        c["audio_available"] = bool(c["media"]) and (c["end"] is None or oldest is None or c["end"] >= oldest)
        c["audio_complete"] = evicted is None or evicted < c["start"]
    return web.json_response(data)


def find_call(request):
    call_id = request.query.get("id", "")
    with LOCK:
        call = ENGINE.tracker.get(call_id)
        if call is None:
            return None, None
        snap = dict(call, media=list(call["media"]), codecs=set(call["codecs"]), sip=list(call["sip"]), sip_raw=list(call["sip_raw"]))
    return call_id, snap


async def call_detail(request):
    _cid, call = find_call(request)
    if call is None:
        return web.json_response({"error": "unknown call (not seen on this node, or older than the recent-call list)"}, status=404)
    data = voipcore.summary(call)
    data["sip"] = call["sip"]
    return web.json_response(data)


async def audio(request):
    _cid, call = find_call(request)
    if call is None:
        REG.inc("voip_audio_requests_total", {"result": "unknown_call"})
        return web.json_response({"error": "unknown call"}, status=404)
    loop = asyncio.get_running_loop()
    wav, info = await loop.run_in_executor(None, voipcore.build_wav, call, ENGINE.ring, LOCAL_IP)
    if wav is None:
        REG.inc("voip_audio_requests_total", {"result": "not_in_ring"})
        return web.json_response({"error": "audio no longer in the RAM buffer", **info}, status=410)
    REG.inc("voip_audio_requests_total", {"result": "ok"})
    return web.Response(body=wav, content_type="audio/wav", headers={
        "Cache-Control": "no-store", "X-Audio-Complete": "1" if info["complete"] else "0",
        "X-Audio-Seconds": str(info["seconds"])})


def epoch(value):
    """Seconds from a query value in seconds or milliseconds (Grafana's ${__from})."""
    if not value:
        return None
    v = float(value)
    return v / 1000.0 if v > 1e11 else v


async def pcap(request):
    """pcap of one call (?id=) or of every call for a number in a time range
    (?number=&from=&to=, epoch seconds or ms). SIP + RTP from RAM only."""
    q = request.query
    try:
        t_from, t_to = epoch(q.get("from")), epoch(q.get("to"))
    except ValueError:
        return web.json_response({"error": "from/to must be epoch seconds or milliseconds"}, status=400)
    if t_from is not None and t_to is not None and t_to - t_from > 24 * 3600:
        return web.json_response({"error": "time range is limited to 24 hours"}, status=400)
    with LOCK:
        if q.get("id"):
            found = [c for c in [ENGINE.tracker.get(q["id"])] if c is not None]
        elif voipcore.digits(q.get("number")):
            found = voipcore.find_calls(ENGINE.tracker, q["number"], t_from, t_to)
        else:
            return web.json_response({"error": "give id=<Call-ID> or number=<digits> (with from/to)"}, status=400)
        calls = [dict(c, media=list(c["media"]), sip_raw=list(c["sip_raw"])) for c in found]
    if not calls:
        return web.json_response({"error": "no matching calls in this agent's memory (call list holds the last "
                                           f"{RECENT_CALLS} calls; audio only the RAM window)"}, status=404)
    loop = asyncio.get_running_loop()
    data, info = await loop.run_in_executor(None, voipcore.build_pcap, calls, ENGINE.ring, LOCAL_IP, t_from, t_to)
    REG.inc("voip_pcap_requests_total")
    label = q.get("id", "")[:40] if q.get("id") else f"{voipcore.digits(q.get('number'))}"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(t_from or calls[0]["start"]))
    name = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in f"{SERVER_NAME}_{label}_{stamp}.pcap")
    return web.Response(body=data, content_type="application/vnd.tcpdump.pcap", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store",
        "X-Pcap-Calls": str(info["calls"]), "X-Pcap-Packets": str(info["packets"]),
        "X-Pcap-Rtp-Packets": str(info["rtp_packets"])})


async def live(request):
    _cid, call = find_call(request)
    if call is None:
        return web.json_response({"error": "unknown call"}, status=404)
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=1000)
    to_us, to_far = voipcore.media_keys(call, LOCAL_IP)
    channel = {k: 0 for k in to_us}
    channel.update({k: 1 for k in to_far})

    def on_packet(key, packet):          # capture thread
        pcm = voipcore.decode_g711(packet[4], voipcore.rtp_body(packet[5], packet[6]))
        if pcm is not None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, bytes([channel.get(key, 0)]) + pcm)
            except Exception:
                pass                     # queue full (slow client) or loop closing: drop the frame

    keys = list(channel)
    await ws.send_json({"type": "start", "call": voipcore.summary(call), "sample_rate": voipcore.SAMPLE_RATE,
                        "channels": {"0": "far end", "1": "our side"}})
    ENGINE.ring.subscribe(keys, on_packet)
    known_media = set(keys)

    async def read_until_closed():       # processes the client's close frame / disconnect
        async for _msg in ws:
            pass

    reader = asyncio.create_task(read_until_closed())
    try:
        while not ws.closed and not reader.done():
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=2)
                await ws.send_bytes(frame)
            except ConnectionResetError:
                break
            except asyncio.TimeoutError:
                with LOCK:
                    current = ENGINE.tracker.get(call["call_id"])
                    state = current["state"] if current else "ended"
                    media = list(current["media"]) if current else []
                new = [k for k in media if k not in known_media]      # re-INVITE / late SDP
                if new:
                    for k in new:
                        channel[k] = 0 if k[0] == LOCAL_IP else 1
                    known_media.update(new)
                    ENGINE.ring.subscribe(new, on_packet)
                if state == "ended":
                    await ws.send_json({"type": "ended"})
                    break
    except ConnectionResetError:
        pass
    finally:
        ENGINE.ring.unsubscribe(list(known_media), on_packet)
        reader.cancel()
        await ws.close()
    return ws


async def start_background(app):
    app["periodic"] = asyncio.create_task(periodic(app))


def main():
    threading.Thread(target=capture_thread, daemon=True, name="capture").start()
    app = web.Application(middlewares=[auth])
    app.router.add_get("/metrics", metrics)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/calls", calls)
    app.router.add_get("/api/call", call_detail)
    app.router.add_get("/api/audio.wav", audio)
    app.router.add_get("/api/live", live)
    app.router.add_get("/api/pcap", pcap)
    app.on_startup.append(start_background)
    host, port = LISTEN.rsplit(":", 1)
    web.run_app(app, host=host, port=int(port), access_log=None, print=None)


if __name__ == "__main__":
    main()
