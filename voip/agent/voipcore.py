"""Core of the VoIP agent: packet parsing, SIP call tracking, an in-memory RTP ring buffer
and G.711 audio rebuild. No sockets or HTTP here, so it can be fed from a live
AF_PACKET socket (agent.py) or from a pcap file (tests, replay).

Nothing is written to disk: RTP packets live in a byte-capped ring in RAM and audio is
rebuilt per request from whatever the ring still holds.
"""
import re
import socket
import struct
import threading
import time
from array import array
from collections import OrderedDict, deque

# ---------------------------------------------------------------- G.711


def _alaw_to_linear(a):
    a ^= 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t = (t + 0x108) << (seg - 1)
    return t if a & 0x80 else -t


def _ulaw_to_linear(u):
    u = ~u & 0xFF
    t = (((u & 0x0F) << 3) + 0x84) << ((u & 0x70) >> 4)
    return 0x84 - t if u & 0x80 else t - 0x84


# payload type -> 256-entry table of 16-bit little-endian sample bytes
_TABLES = {
    0: [struct.pack("<h", _ulaw_to_linear(i)) for i in range(256)],   # PCMU
    8: [struct.pack("<h", _alaw_to_linear(i)) for i in range(256)],   # PCMA
}
SAMPLE_RATE = 8000
CODEC_NAMES = {0: "PCMU", 8: "PCMA", 9: "G722", 13: "CN", 18: "G729", 101: "telephone-event"}


def decode_g711(pt, payload):
    """PCM16 little-endian bytes for a G.711 payload, or None for other payload types."""
    table = _TABLES.get(pt)
    if table is None:
        return None
    return b"".join(map(table.__getitem__, payload))


def rtp_body(raw, hdr):
    """Audio payload of a stored RTP packet (header, extension and padding removed)."""
    body = raw[hdr:]
    if raw[0] & 0x20 and body and body[-1] <= len(body):
        body = body[:-body[-1]]
    return body


# ---------------------------------------------------------------- IPv4 / UDP

class Ipv4Reassembler:
    """Reassembles fragmented UDP datagrams (SIP INVITEs/200 OKs with SDP are often > MTU)."""

    def __init__(self, timeout=5.0):
        self.timeout = timeout
        self.parts = {}

    def feed(self, ip, now):
        """ip: bytes of an IPv4 packet. Returns (src, dst, proto, payload) or None."""
        if len(ip) < 20 or ip[0] >> 4 != 4:
            return None
        ihl = (ip[0] & 0x0F) * 4
        total = struct.unpack_from("!H", ip, 2)[0]
        ident, frag = struct.unpack_from("!HH", ip, 4)
        proto = ip[9]
        src, dst = ip[12:16], ip[16:20]
        data = ip[ihl:total] if total >= ihl else ip[ihl:]
        more, offset = frag & 0x2000, (frag & 0x1FFF) * 8
        if not more and offset == 0:
            return src, dst, proto, data
        key = (src, dst, ident, proto)
        entry = self.parts.setdefault(key, {"t": now, "chunks": {}, "end": None})
        entry["chunks"][offset] = data
        if not more:
            entry["end"] = offset + len(data)
        if entry["end"] is not None:
            buf, pos = bytearray(), 0
            for off in sorted(entry["chunks"]):
                if off != pos:
                    break
                buf += entry["chunks"][off]
                pos = off + len(entry["chunks"][off])
            if pos == entry["end"]:
                del self.parts[key]
                return src, dst, proto, bytes(buf)
        if len(self.parts) > 256:
            for k in [k for k, v in self.parts.items() if now - v["t"] > self.timeout]:
                del self.parts[k]
        return None


def ip_str(b):
    return "%d.%d.%d.%d" % tuple(b)


# ---------------------------------------------------------------- SIP

_COMPACT = {"i": "call-id", "f": "from", "t": "to", "c": "content-type", "l": "content-length", "m": "contact", "v": "via"}
_METHOD_RE = re.compile(r"^([A-Z]+) (sips?|tel):")
_USER_RE = re.compile(r"(?:sips?|tel):\+?([^@;>\s]+)")


def parse_sip(payload):
    """Minimal SIP parser. Returns dict or None if the payload is not SIP."""
    try:
        text = payload.decode("latin-1")
    except Exception:
        return None
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    first = lines[0]
    if first.startswith("SIP/2.0 "):
        parts = first.split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            return None
        method, code = "", parts[1]
    else:
        m = _METHOD_RE.match(first)
        if not m:
            return None
        method, code = m.group(1), ""
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep:
            continue
        name = name.strip().lower()
        name = _COMPACT.get(name, name)
        headers.setdefault(name, value.strip())
    cseq = headers.get("cseq", "").split()
    msg = {
        "first_line": first, "method": method, "code": code,
        "cseq_method": cseq[1] if len(cseq) > 1 else "",
        "call_id": headers.get("call-id", ""),
        "from": _user(headers.get("from", "")), "to": _user(headers.get("to", "")),
        "to_tag": ";tag=" in headers.get("to", "").replace(" ", ""),
        "ruri": _user(first) if method else "",
        "reason": headers.get("reason", ""),
        "ua": headers.get("user-agent") or headers.get("server", ""),
        "sdp": parse_sdp(body) if "application/sdp" in headers.get("content-type", "").lower() else None,
        "raw": payload,
    }
    return msg


def _user(value):
    m = _USER_RE.search(value)
    return m.group(1) if m else ""


def parse_sdp(body):
    """(ip, port, [payload types]) of the first audio stream, or None."""
    session_ip, media_ip, port, pts = None, None, None, []
    in_audio = False
    for line in body.splitlines():
        if line.startswith("c=IN IP4 "):
            ip = line[9:].split("/")[0].strip()
            if in_audio:
                media_ip = ip
            elif port is None:
                session_ip = ip
        elif line.startswith("m="):
            if port is not None:
                break
            fields = line[2:].split()
            if fields and fields[0] == "audio" and len(fields) >= 3:
                in_audio = True
                port = int(fields[1])
                pts = [int(p) for p in fields[3:] if p.isdigit()]
            else:
                in_audio = False
    ip = media_ip or session_ip
    if ip and port:
        return ip, port, pts
    return None


# ---------------------------------------------------------------- RTP ring buffer

PER_PACKET_OVERHEAD = 390   # measured: a stored 160-byte G.711 packet costs ~550 bytes of RAM in total


class RtpRing:
    """Byte-capped FIFO of RTP packets, indexed by destination (ip, port).

    Packets are stored as (ts, ssrc, seq, rtp_ts, pt, raw_rtp, header_len, (src_ip, src_port)):
    the whole RTP packet is kept so it can be exported to pcap. The oldest packets are
    dropped once the cap is reached, like a DVR. Live listeners get each packet as it
    arrives."""

    def __init__(self, cap_bytes):
        self.cap = cap_bytes
        self.lock = threading.Lock()
        self.order = deque()          # (key, size, ts) in arrival order
        self.by_key = {}              # (ip, port) -> deque of packets
        self.bytes = 0
        self.evicted_ts = None        # arrival time of the newest packet dropped so far
        self.listeners = {}           # (ip, port) -> set of callables(key, packet)

    def add(self, key, packet):
        size = len(packet[5]) + PER_PACKET_OVERHEAD
        with self.lock:
            q = self.by_key.get(key)
            if q is None:
                q = self.by_key[key] = deque()
            q.append(packet)
            self.order.append((key, size, packet[0]))
            self.bytes += size
            while self.bytes > self.cap and self.order:
                old_key, old_size, old_ts = self.order.popleft()
                self.bytes -= old_size
                self.evicted_ts = old_ts
                old_q = self.by_key[old_key]
                old_q.popleft()
                if not old_q:
                    del self.by_key[old_key]
            listeners = self.listeners.get(key)
        if listeners:
            for cb in list(listeners):
                cb(key, packet)

    def window_seconds(self, now):
        with self.lock:
            return now - self.order[0][2] if self.order else 0.0

    def oldest_ts(self):
        with self.lock:
            return self.order[0][2] if self.order else None

    def get(self, keys, t_from, t_to):
        out = {}
        with self.lock:
            for key in keys:
                q = self.by_key.get(key)
                if q:
                    out[key] = [p for p in q if t_from <= p[0] <= t_to]
        return out

    def subscribe(self, keys, cb):
        with self.lock:
            for key in keys:
                self.listeners.setdefault(key, set()).add(cb)

    def unsubscribe(self, keys, cb):
        with self.lock:
            for key in keys:
                s = self.listeners.get(key)
                if s:
                    s.discard(cb)
                    if not s:
                        del self.listeners[key]

    def listener_count(self):
        with self.lock:
            return len({id(cb) for s in self.listeners.values() for cb in s})


class StreamStats:
    """RFC 3550 loss and interarrival jitter for one RTP stream (one SSRC to one destination)."""
    __slots__ = ("ssrc", "pt", "base_seq", "max_seq", "cycles", "received", "jitter", "transit", "last_ts")

    def __init__(self, ssrc, seq, pt):
        self.ssrc, self.pt = ssrc, pt
        self.base_seq = self.max_seq = seq
        self.cycles = 0
        self.received = 0
        self.jitter = 0.0
        self.transit = None
        self.last_ts = 0.0

    def update(self, seq, rtp_ts, arrival):
        self.received += 1
        self.last_ts = arrival
        delta = (seq - self.max_seq) & 0xFFFF
        if 0 < delta < 3000:
            if seq < self.max_seq:
                self.cycles += 0x10000
            self.max_seq = seq
        transit = arrival * SAMPLE_RATE - rtp_ts
        if self.transit is not None:
            d = abs(transit - self.transit)
            if d < SAMPLE_RATE * 10:      # ignore timestamp jumps (new talk spurt, re-INVITE)
                self.jitter += (d - self.jitter) / 16.0
        self.transit = transit

    def expected(self):
        return self.cycles + self.max_seq - self.base_seq + 1

    def lost(self):
        return max(0, self.expected() - self.received)

    def jitter_ms(self):
        return self.jitter / SAMPLE_RATE * 1000.0


# ---------------------------------------------------------------- call tracking

class CallTracker:
    """Follows initial INVITEs to their final answer and BYE. Keeps active calls and the
    most recent finished calls in memory, with each call's media endpoints from SDP."""

    def __init__(self, local_ip, recent_max=2000, on_finish=None, timeout=4 * 3600):
        self.local_ip = local_ip
        self.active = OrderedDict()
        self.recent = deque(maxlen=recent_max)
        self.by_id = {}
        self.on_finish = on_finish
        self.timeout = timeout
        self.lock = threading.Lock()

    def on_sip(self, ts, src, sport, dst, dport, msg):
        direction = "out" if src == self.local_ip else "in"
        call_id = msg["call_id"]
        if not call_id:
            return
        with self.lock:
            call = self.active.get(call_id)
            if call is None and msg["method"] == "INVITE" and not msg["to_tag"]:
                call = {
                    "call_id": call_id, "direction": "outbound" if direction == "out" else "inbound",
                    "from": msg["from"], "to": msg["to"] or msg["ruri"], "start": ts,
                    "ring": None, "answered": None, "end": None, "state": "ringing",
                    "result": "", "code": "", "reason": "", "hangup_by": "",
                    "media": [], "codecs": set(), "sip": [], "sip_raw": [], "quality": {}}
                self.active[call_id] = call
                self.by_id[call_id] = call
            ended = None
            if call is None:
                # 200 OK to BYE, ACK to a failure, retransmissions: keep them with a call that just ended
                ended = self.by_id.get(call_id)
                if ended is None or ended["end"] is None or ts - ended["end"] > 60:
                    return
                call = ended
            if len(call["sip_raw"]) < 200:
                call["sip_raw"].append((ts, src, sport, dst, dport, msg["raw"]))
            if len(call["sip"]) < 200:
                call["sip"].append({"t": round(ts, 3), "dir": direction, "src": f"{src}:{sport}",
                                    "dst": f"{dst}:{dport}", "line": msg["first_line"][:120]})
            if ended is not None:
                return
            if msg["sdp"]:
                ip, port, pts = msg["sdp"]
                if (ip, port) not in call["media"]:
                    call["media"].append((ip, port))
                call["codecs"].update(CODEC_NAMES.get(p, str(p)) for p in pts if p in (0, 8, 9, 18))
            finished = None
            if msg["code"] and msg["cseq_method"] == "INVITE":
                status = int(msg["code"])
                if status in (180, 183, 200) and call["ring"] is None:
                    call["ring"] = ts
                if status >= 200 and status not in (401, 407) and call["answered"] is None and not call["result"]:
                    if status < 300:
                        call["answered"], call["state"] = ts, "answered"
                    else:
                        call["result"] = "cancelled" if status == 487 else "failed"
                        call["code"], call["reason"] = msg["code"], msg["reason"]
                        finished = call
            elif msg["method"] == "BYE" and call["answered"] is not None:
                call["result"], call["code"], call["reason"] = "answered", "200", msg["reason"]
                call["hangup_by"] = "us" if direction == "out" else "far_end"
                finished = call
            if finished is not None:
                self._finish(finished, ts)

    def _finish(self, call, ts):
        call["end"], call["state"] = ts, "ended"
        self.active.pop(call["call_id"], None)
        if len(self.recent) == self.recent.maxlen:
            old = self.recent[0]
            if self.by_id.get(old["call_id"]) is old:
                del self.by_id[old["call_id"]]
        self.recent.append(call)
        if self.on_finish:
            self.on_finish(call)

    def expire(self, now):
        with self.lock:
            for call in [c for c in self.active.values() if now - c["start"] > self.timeout]:
                call["result"] = call["result"] or ("answered" if call["answered"] else "failed")
                call["code"] = call["code"] or ("200" if call["answered"] else "timeout")
                call["hangup_by"] = "unknown"
                self._finish(call, now)

    def get(self, call_id):
        with self.lock:
            return self.by_id.get(call_id)

    def snapshot(self, which, limit=200):
        with self.lock:
            calls = list(self.active.values()) if which == "live" else list(self.recent)[-limit:][::-1]
            return [summary(c) for c in calls]


def summary(call, now=None):
    now = now or time.time()
    end = call["end"] or now
    return {
        "call_id": call["call_id"], "direction": call["direction"], "from": call["from"], "to": call["to"],
        "state": call["state"], "result": call["result"], "code": call["code"], "reason": call["reason"],
        "hangup_by": call["hangup_by"], "start": call["start"], "answered": call["answered"], "end": call["end"],
        "pdd_s": round(call["ring"] - call["start"], 3) if call["ring"] else None,
        "duration_s": round(end - call["answered"], 1) if call["answered"] else 0,
        "codecs": sorted(call["codecs"]), "media": [f"{ip}:{port}" for ip, port in call["media"]],
        "quality": call["quality"]}


# ---------------------------------------------------------------- audio rebuild

def media_keys(call, local_ip):
    """Split a call's SDP endpoints into (to_us, to_far_end) ring keys.
    Packets sent to our own address carry the far end's voice and the other way round."""
    to_us = [k for k in call["media"] if k[0] == local_ip]
    to_far = [k for k in call["media"] if k[0] != local_ip]
    return to_us, to_far


def build_channel(packets, t0):
    """PCM16 samples for one direction: packets placed by RTP timestamp, gaps left silent."""
    pcm = array("h")
    by_ssrc = {}
    for p in packets:
        by_ssrc.setdefault(p[1], []).append(p)
    for plist in by_ssrc.values():
        plist.sort(key=lambda p: p[0])
        first = plist[0]
        base = int((first[0] - t0) * SAMPLE_RATE)
        for _ts, _ssrc, _seq, rtp_ts, pt, raw, hdr, _src in plist:
            samples = decode_g711(pt, rtp_body(raw, hdr))
            if samples is None:
                continue
            offset = base + ((rtp_ts - first[3]) & 0xFFFFFFFF)
            if offset < 0 or offset > SAMPLE_RATE * 6 * 3600:
                continue
            chunk = array("h")
            chunk.frombytes(samples)
            end = offset + len(chunk)
            if end > len(pcm):
                pcm.extend(array("h", bytes(2 * (end - len(pcm)))))
            pcm[offset:end] = chunk
    return pcm


def build_wav(call, ring, local_ip, now=None):
    """Stereo WAV (left = far end, right = our side) rebuilt from the ring buffer.
    Returns (wav_bytes, info) or (None, info) when the ring no longer holds the call."""
    now = now or time.time()
    to_us, to_far = media_keys(call, local_ip)
    t_from, t_to = call["start"] - 2, (call["end"] or now) + 2
    got = ring.get(to_us + to_far, t_from, t_to)
    far_pkts = [p for k in to_us for p in got.get(k, [])]
    our_pkts = [p for k in to_far for p in got.get(k, [])]
    info = {"packets_far_end": len(far_pkts), "packets_our_side": len(our_pkts),
            "ring_oldest": ring.oldest_ts(), "call_start": call["start"]}
    if not far_pkts and not our_pkts:
        return None, info
    t0 = min(p[0] for p in far_pkts + our_pkts)
    left, right = build_channel(far_pkts, t0), build_channel(our_pkts, t0)
    n = max(len(left), len(right))
    for ch in (left, right):
        if len(ch) < n:
            ch.extend(array("h", bytes(2 * (n - len(ch)))))
    stereo = array("h", bytes(4 * n))
    stereo[0::2] = left
    stereo[1::2] = right
    info["seconds"] = round(n / SAMPLE_RATE, 1)
    # complete = the ring has not dropped anything from this call yet
    info["complete"] = ring.evicted_ts is None or ring.evicted_ts < call["start"]
    return wav_header(n * 4, channels=2) + stereo.tobytes(), info


def wav_header(data_len, channels=2, rate=SAMPLE_RATE, bits=16):
    block = channels * bits // 8
    return (b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block, block, bits) +
            b"data" + struct.pack("<I", data_len))


# ---------------------------------------------------------------- pcap export

def digits(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def find_calls(tracker, number="", t_from=None, t_to=None, limit=200):
    """Calls (active and recent) whose From/To contains `number` (digits only, so +971 / 00971
    styles both match on the common part) and that overlap [t_from, t_to]."""
    wanted = digits(number)
    with tracker.lock:
        calls = list(tracker.active.values()) + list(tracker.recent)
    out = []
    for call in calls:
        if wanted and wanted not in digits(call["from"]) and wanted not in digits(call["to"]):
            continue
        end = call["end"] or time.time()
        if t_from is not None and end < t_from:
            continue
        if t_to is not None and call["start"] > t_to:
            continue
        out.append(call)
    out.sort(key=lambda c: c["start"])
    return out[-limit:]


def _ipv4_udp(src, sport, dst, dport, payload):
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    hdr = bytearray(struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0x4000, 64, 17, 0,
                                socket.inet_aton(src), socket.inet_aton(dst)))
    total = sum(struct.unpack("!10H", hdr))
    total = (total & 0xFFFF) + (total >> 16)
    total = (total & 0xFFFF) + (total >> 16)
    struct.pack_into("!H", hdr, 10, ~total & 0xFFFF)
    return bytes(hdr) + udp


def build_pcap(calls, ring, local_ip, t_from=None, t_to=None, now=None):
    """Classic pcap (raw IPv4) with each call's SIP messages and RTP packets, ordered by time.
    IP/UDP headers are rebuilt from the stored addresses; SIP and RTP bytes are exactly as
    captured. RTP is only there while it is still in the RAM ring."""
    now = now or time.time()
    records = []
    rtp_count = 0
    for call in calls:
        lo = call["start"] - 2 if t_from is None else max(call["start"] - 2, t_from)
        hi = (call["end"] or now) + 2 if t_to is None else min((call["end"] or now) + 2, t_to)
        for ts, src, sport, dst, dport, raw in call["sip_raw"]:
            if lo - 60 <= ts <= hi + 60:          # keep the whole SIP dialog around the window
                records.append((ts, src, sport, dst, dport, raw))
        got = ring.get(list(call["media"]), lo, hi)
        for (dst, dport), packets in got.items():
            for p in packets:
                records.append((p[0], p[7][0], p[7][1], dst, dport, p[5]))
                rtp_count += 1
    records.sort(key=lambda r: r[0])
    out = [struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 101)]   # LINKTYPE_RAW
    for ts, src, sport, dst, dport, payload in records:
        pkt = _ipv4_udp(src, sport, dst, dport, payload)
        sec = int(ts)
        out.append(struct.pack("<IIII", sec, int((ts - sec) * 1e6), len(pkt), len(pkt)))
        out.append(pkt)
    info = {"calls": len(calls), "packets": len(records), "rtp_packets": rtp_count,
            "sip_packets": len(records) - rtp_count, "ring_oldest": ring.oldest_ts()}
    return b"".join(out), info


# ---------------------------------------------------------------- packet entry point

def is_rtp(payload):
    return len(payload) >= 12 and payload[0] >> 6 == 2 and (payload[1] & 0x7F) not in range(72, 77)


class Engine:
    """Feeds packets into the SIP tracker, the RTP ring and per-stream stats."""

    def __init__(self, local_ip, ring_bytes, sip_ports=(5060, 5080), recent_max=2000, on_finish=None, metrics=None):
        self.local_ip = local_ip
        self.sip_ports = set(sip_ports)
        self.reasm = Ipv4Reassembler()
        self.ring = RtpRing(ring_bytes)
        self.tracker = CallTracker(local_ip, recent_max, on_finish=self._finish_hook)
        self.user_on_finish = on_finish
        self.streams = {}              # (dst_ip, dst_port, ssrc) -> StreamStats
        self._src_keys = {}            # shared (src_ip, src_port) tuples for stored packets
        self.metrics = metrics
        self.on_sip_message = None     # optional callback(ts, src, sport, dst, dport, msg)

    def _finish_hook(self, call):
        call["quality"] = self.call_quality(call)
        if self.user_on_finish:
            self.user_on_finish(call)

    def call_quality(self, call):
        quality = {}
        to_us, to_far = media_keys(call, self.local_ip)
        for label, keys in (("far_end_to_us", to_us), ("us_to_far_end", to_far)):
            received = lost = 0
            jitter = 0.0
            for (ip, port, _ssrc), st in list(self.streams.items()):
                if (ip, port) in keys and st.last_ts >= call["start"]:
                    received += st.received
                    lost += st.lost()
                    jitter = max(jitter, st.jitter_ms())
            expected = received + lost
            quality[label] = {"packets": received, "lost": lost,
                              "loss_pct": round(100.0 * lost / expected, 2) if expected else 0.0,
                              "jitter_ms": round(jitter, 2)}
        return quality

    def ipv4(self, ip_packet, ts):
        res = self.reasm.feed(ip_packet, ts)
        if res is None:
            return
        src_b, dst_b, proto, data = res
        if proto != 17 or len(data) < 8:
            if self.metrics:
                self.metrics.inc("voip_bytes_total", {"proto": "other"}, len(ip_packet))
            return
        sport, dport = struct.unpack_from("!HH", data, 0)
        payload = data[8:]
        src, dst = ip_str(src_b), ip_str(dst_b)
        if sport in self.sip_ports or dport in self.sip_ports:
            msg = parse_sip(payload)
            if msg is not None:
                if self.metrics:
                    self.metrics.inc("voip_bytes_total", {"proto": "sip"}, len(data) + 20)
                    self.metrics.inc("voip_sip_messages_total", {
                        "direction": "out" if src == self.local_ip else "in",
                        "method": msg["cseq_method"] or msg["method"], "code": msg["code"] or "request"})
                self.tracker.on_sip(ts, src, sport, dst, dport, msg)
                if self.on_sip_message:
                    self.on_sip_message(ts, src, sport, dst, dport, msg)
                return
        if is_rtp(payload):
            pt = payload[1] & 0x7F
            seq, rtp_ts, ssrc = struct.unpack_from("!HII", payload, 2)
            cc = payload[0] & 0x0F
            hdr = 12 + 4 * cc
            if payload[0] & 0x10 and len(payload) >= hdr + 4:      # header extension
                hdr += 4 + 4 * struct.unpack_from("!H", payload, hdr + 2)[0]
            skey = (dst, dport, ssrc)
            st = self.streams.get(skey)
            if st is None:
                st = self.streams[skey] = StreamStats(ssrc, seq, pt)
            before_lost = st.lost()
            st.update(seq, rtp_ts, ts)
            if self.metrics:
                direction = "in" if dst == self.local_ip else "out"
                self.metrics.inc("voip_bytes_total", {"proto": "rtp"}, len(data) + 20)
                self.metrics.inc("voip_rtp_packets_total", {"direction": direction})
                gained = st.lost() - before_lost
                if gained > 0:
                    self.metrics.inc("voip_rtp_lost_packets_total", {"direction": direction}, gained)
            src_key = self._src_keys.get((src, sport))
            if src_key is None:
                if len(self._src_keys) > 100000:
                    self._src_keys.clear()
                src_key = self._src_keys[(src, sport)] = (src, sport)
            self.ring.add((dst, dport), (ts, ssrc, seq, rtp_ts, pt, payload, hdr, src_key))
        elif self.metrics:
            self.metrics.inc("voip_bytes_total", {"proto": "other"}, len(data) + 20)

    def expire(self, now, stream_idle=120):
        self.tracker.expire(now)
        for key in [k for k, st in self.streams.items() if now - st.last_ts > stream_idle]:
            del self.streams[key]

    def stream_jitter(self, now, active_within=10):
        """(mean, max) jitter in ms over streams that received packets recently."""
        values = [st.jitter_ms() for st in self.streams.values() if now - st.last_ts <= active_within]
        if not values:
            return 0.0, 0.0
        return sum(values) / len(values), max(values)


# ---------------------------------------------------------------- pcap reader (tests / replay)

def read_pcap(path_or_file):
    """Yields (ts, ipv4_bytes) from a classic pcap (Ethernet, Linux SLL or SLL2)."""
    f = open(path_or_file, "rb") if isinstance(path_or_file, str) else path_or_file
    header = f.read(24)
    magic = struct.unpack("<I", header[:4])[0]
    endian = "<" if magic in (0xA1B2C3D4, 0xA1B23C4D) else ">"
    nano = magic in (0xA1B23C4D, 0x4D3CB2A1)
    linktype = struct.unpack(endian + "I", header[20:24])[0]
    while True:
        rec = f.read(16)
        if len(rec) < 16:
            break
        sec, frac, incl, _orig = struct.unpack(endian + "IIII", rec)
        data = f.read(incl)
        ts = sec + frac / (1e9 if nano else 1e6)
        if linktype == 1:            # Ethernet
            off, etype = 14, struct.unpack_from("!H", data, 12)[0]
            while etype == 0x8100:
                etype = struct.unpack_from("!H", data, off + 2)[0]
                off += 4
        elif linktype == 113:        # Linux cooked v1
            off, etype = 16, struct.unpack_from("!H", data, 14)[0]
        elif linktype == 276:        # Linux cooked v2
            off, etype = 20, struct.unpack_from("!H", data, 0)[0]
        elif linktype in (101, 228):  # raw IP / raw IPv4 (what build_pcap writes)
            off, etype = 0, 0x0800 if data and data[0] >> 4 == 4 else 0
        else:
            continue
        if etype == 0x0800:
            yield ts, data[off:]
