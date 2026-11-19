#!/usr/bin/env python3
"""voip-gateway: the browser-facing side of call listening, next to Grafana on 244.

- Served under BASE_PATH (/voip) on the Grafana domain through nginx-proxy-manager, so the
  browser sends its Grafana session cookie. Every request is checked against Grafana
  (/api/user, /api/user/orgs): no Grafana login, no access.
- Roles: Admin (or Grafana server admin) = live + replay, Editor = replay,
  Viewer = call lists only.
- Every replay / live session is written to Loki (job="voip-audit") and to stdout.
- Proxies to the voip-agent on each trunk node over the VPN (AGENTS), with AGENT_TOKEN.
  No audio is stored here.
"""
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web


def env(name, default=None, required=False):
    value = os.environ.get(name, "").strip()
    if not value:
        if required:
            sys.exit(f"{name} is required")
        return default
    return value


GRAFANA_URL = env("GRAFANA_URL", "http://grafana:3000").rstrip("/")
AGENT_TOKEN = env("AGENT_TOKEN", required=True)
LOKI_URL = env("LOKI_URL", "")
BASE = "/" + env("BASE_PATH", "/voip").strip("/")
LISTEN_PORT = int(env("LISTEN_PORT", "8090"))
# "meydan/my03=http://192.168.99.42:9479,spc/sj02=http://..."
AGENTS = {}
for item in env("AGENTS", required=True).split(","):
    name, _, url = item.strip().partition("=")
    cluster, _, node = name.partition("/")
    AGENTS[node] = {"cluster": cluster, "node": node, "url": url.rstrip("/")}

ROLE_RANK = {"Viewer": 1, "Editor": 2, "Admin": 3}
NEEDS = {"list": 1, "play": 2, "live": 3}
INDEX_HTML = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, flush=True)


# ---------------------------------------------------------------- Grafana auth

_ROLE_CACHE = {}   # sha256(cookie) -> (expires, user dict)


async def grafana_user(request):
    """The Grafana user behind this request's session cookie, or None."""
    cookie = request.headers.get("Cookie", "")
    if not cookie:
        return None
    key = hashlib.sha256(cookie.encode()).hexdigest()
    hit = _ROLE_CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    session = request.app["http"]
    headers = {"Cookie": cookie}
    try:
        async with session.get(f"{GRAFANA_URL}/api/user", headers=headers) as r:
            if r.status != 200:
                return None
            user = await r.json()
        async with session.get(f"{GRAFANA_URL}/api/user/orgs", headers=headers) as r:
            orgs = await r.json() if r.status == 200 else []
    except aiohttp.ClientError as e:
        log(f"grafana auth check failed: {e}")
        return None
    role = next((o.get("role") for o in orgs if o.get("orgId") == user.get("orgId")), "Viewer")
    if user.get("isGrafanaAdmin"):
        role = "Admin"
    info = {"login": user.get("login", ""), "name": user.get("name", ""), "role": role}
    _ROLE_CACHE[key] = (time.time() + 30, info)
    if len(_ROLE_CACHE) > 1000:
        now = time.time()
        for k in [k for k, v in _ROLE_CACHE.items() if v[0] < now]:
            del _ROLE_CACHE[k]
    return info


def allowed(user, action):
    return user is not None and ROLE_RANK.get(user["role"], 0) >= NEEDS[action]


async def require(request, action):
    user = await grafana_user(request)
    if user is None:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "log in to Grafana first"}), content_type="application/json")
    if not allowed(user, action):
        await audit(request, user, action, result="denied")
        raise web.HTTPForbidden(text=json.dumps({"error": f"your Grafana role ({user['role']}) cannot {action}"}),
                                content_type="application/json")
    return user


def agent_for(request):
    node = request.query.get("node", "")
    agent = AGENTS.get(node)
    if agent is None:
        raise web.HTTPNotFound(text=json.dumps({"error": f"unknown node '{node}'"}), content_type="application/json")
    return agent


# ---------------------------------------------------------------- audit

async def audit(request, user, action, result="ok", **extra):
    entry = {
        "user": user["login"] if user else "", "role": user["role"] if user else "", "action": action,
        "result": result, "node": request.query.get("node", ""), "call_id": request.query.get("id", ""),
        "client_ip": request.headers.get("X-Real-IP") or request.headers.get("X-Forwarded-For", request.remote or ""),
        "user_agent": request.headers.get("User-Agent", "")[:200], **extra}
    log("AUDIT " + json.dumps(entry))
    if not LOKI_URL:
        return
    payload = {"streams": [{"stream": {"job": "voip-audit", "action": action, "result": result},
                            "values": [[str(time.time_ns()), json.dumps(entry, separators=(",", ":"))]]}]}
    try:
        async with request.app["http"].post(LOKI_URL, json=payload) as r:
            if r.status >= 300:
                log(f"audit push to Loki failed: HTTP {r.status}")
    except aiohttp.ClientError as e:
        log(f"audit push to Loki failed: {e}")


# ---------------------------------------------------------------- routes

async def index(request):
    return web.Response(text=INDEX_HTML.replace("__BASE__", BASE), content_type="text/html",
                        headers={"Cache-Control": "no-store"})


async def me(request):
    user = await grafana_user(request)
    if user is None:
        return web.json_response({"logged_in": False}, status=401)
    return web.json_response({"logged_in": True, **user,
                              "can": {a: allowed(user, a) for a in NEEDS}})


async def nodes(request):
    await require(request, "list")
    return web.json_response([{"node": a["node"], "cluster": a["cluster"]} for a in AGENTS.values()])


async def agent_get(request, path, params):
    agent = agent_for(request)
    try:
        async with request.app["http"].get(f"{agent['url']}{path}", params=params,
                                           headers={"X-Agent-Token": AGENT_TOKEN}) as r:
            body = await r.read()
            return web.Response(body=body, status=r.status, content_type=r.content_type,
                                headers={k: v for k, v in r.headers.items()
                                         if k.startswith(("X-Audio", "X-Pcap")) or k == "Content-Disposition"})
    except aiohttp.ClientError as e:
        raise web.HTTPBadGateway(text=json.dumps({"error": f"agent {agent['node']} unreachable: {e}"}),
                                 content_type="application/json")


async def calls(request):
    await require(request, "list")
    return await agent_get(request, "/api/calls", {"state": request.query.get("state", "recent"),
                                                   "limit": request.query.get("limit", "200")})


async def call_detail(request):
    await require(request, "list")
    return await agent_get(request, "/api/call", {"id": request.query.get("id", "")})


async def status(request):
    await require(request, "list")
    return await agent_get(request, "/api/status", {})


async def audio(request):
    user = await require(request, "play")
    resp = await agent_get(request, "/api/audio.wav", {"id": request.query.get("id", "")})
    await audit(request, user, "play", result="ok" if resp.status == 200 else f"http_{resp.status}",
                seconds=resp.headers.get("X-Audio-Seconds", ""))
    if resp.status == 200:
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Content-Disposition"] = "inline"
    return resp


async def pcap(request):
    """pcap of a call (?id=) or of a number over a time range (?number=&from=&to=). Same role as replay."""
    user = await require(request, "play")
    q = request.query
    params = {k: q[k] for k in ("id", "number", "from", "to") if q.get(k)}
    resp = await agent_get(request, "/api/pcap", params)
    await audit(request, user, "pcap", result="ok" if resp.status == 200 else f"http_{resp.status}",
                number=q.get("number", ""), time_from=q.get("from", ""), time_to=q.get("to", ""),
                packets=resp.headers.get("X-Pcap-Packets", ""), calls=resp.headers.get("X-Pcap-Calls", ""))
    return resp


async def live(request):
    # Cookies ride along on cross-site WebSocket handshakes, so check the Origin.
    origin = request.headers.get("Origin", "")
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host", "")
    if origin and origin.split("://", 1)[-1] != host:
        raise web.HTTPForbidden(text="bad origin")
    user = await require(request, "live")
    agent = agent_for(request)
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    started = time.time()
    await audit(request, user, "live", result="started")
    try:
        async with request.app["http"].ws_connect(
                f"{agent['url']}/api/live", params={"id": request.query.get("id", "")},
                headers={"X-Agent-Token": AGENT_TOKEN}, heartbeat=20) as upstream:

            async def browser_to_agent():
                async for msg in ws:
                    if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        break
                await upstream.close()

            pump = asyncio.create_task(browser_to_agent())
            async for msg in upstream:
                if msg.type == WSMsgType.BINARY:
                    await ws.send_bytes(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    await ws.send_str(msg.data)
                else:
                    break
            pump.cancel()
    except aiohttp.WSServerHandshakeError as e:
        await ws.send_json({"type": "error", "error": f"agent refused live stream (HTTP {e.status})"})
    except ConnectionResetError:
        pass                                  # browser went away
    except aiohttp.ClientError as e:
        await ws.send_json({"type": "error", "error": f"agent unreachable: {e}"})
    finally:
        await audit(request, user, "live", result="stopped", seconds=round(time.time() - started, 1))
        await ws.close()
    return ws


async def on_startup(app):
    app["http"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=120))


async def on_cleanup(app):
    await app["http"].close()


def main():
    app = web.Application()
    r = app.router
    r.add_get(BASE, index)
    r.add_get(BASE + "/", index)
    r.add_get(BASE + "/api/me", me)
    r.add_get(BASE + "/api/nodes", nodes)
    r.add_get(BASE + "/api/status", status)
    r.add_get(BASE + "/api/calls", calls)
    r.add_get(BASE + "/api/call", call_detail)
    r.add_get(BASE + "/api/audio.wav", audio)
    r.add_get(BASE + "/api/live", live)
    r.add_get(BASE + "/api/pcap", pcap)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    log(f"voip-gateway on :{LISTEN_PORT}{BASE}, agents: {', '.join(AGENTS)}")
    web.run_app(app, port=LISTEN_PORT, access_log=None, print=None)


if __name__ == "__main__":
    main()
