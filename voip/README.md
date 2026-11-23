# VoIP call listening + SIP/RTP observability (Grafana)

Listen to trunk calls live, replay recent ones and download PCAPs from Grafana, with SIP/RTP
metrics, without storing pcaps or audio on disk.

```
my03 (Meydan, ens19 = du trunk)                244 (monitoring)                         Browser
──────────────────────────────────            ──────────────────────────────           ────────────────────────────
voip-agent  (voip/agent)                       voip-gateway  (voip/gateway)             Grafana "VoIP Calls"
 • raw AF_PACKET socket on ens19                • checks the Grafana session + role       • embedded call console:
 • RTP in a RAM ring (RING_MB)              ◄─VPN─ • audits every listen → Loki             🎧 live · ▶ replay · ⬇ PCAP
 • SIP call tracker (INVITE→200→BYE, SDP)       • call console page, WebSocket proxy     • SIP / RTP / ASR panels
 • G.711 → WAV, PCAP export, live PCM           • served at https://ocubedashboards…/voip  • listening audit
 • :9479 /metrics → Prometheus (job voip_agent)
 • finished calls + SIP → Loki (job voip, 48 h)
```

Nothing is written to disk on the node: the container is read-only with no volumes.
When the ring is full the oldest packets drop out; replay and PCAP audio work while a call is
still in it (the dashboard's "Replay window" shows how far back that is).

Meydan: only my03 runs the agent: it is the only Meydan node with the trunk (ens19 is down on
my01/my02/my04). ens19 carries the du side only (SIP 10.59.108.25:5060, RTP 10.59.109.x).
Dubai South: ds02 runs the agent on ens19 (du_ae_trunk 044967600); ds01 is standby.

## Access

| Grafana role | Call lists | Replay + PCAP download | Live |
|---|---|---|---|
| Admin (or Grafana server admin) | ✓ | ✓ | ✓ |
| Editor | ✓ | ✓ | |
| Viewer | ✓ | | |

Not logged in to Grafana = no access. Every replay, live session and PCAP download (and every
denied attempt) is logged to Loki `{job="voip-audit"}` with user, role, call, client IP; the
dashboard shows it. The audit keeps the default 14-day retention.

Listening to calls is call interception: make sure company policy and the TDRA rules allow it
before giving anyone the Admin or Editor role for this.

## PCAP download

SIP + RTP for one call, or for every call of a number in a time range, built from the agent's
RAM. Opens straight in Wireshark: Telephony → VoIP Calls → Flow Sequence / Play Streams.

- In the console: the "Download PCAP" box (number, from, to), or ⬇ PCAP on a call row / in the player.
- In Grafana: type the number in **Search**, set the time range, click the "Download PCAP for … in
  this time range" link at the top; or a row in Live / Recent calls → "⬇ Download PCAP".
- A call older than the replay window gives SIP only. Numbers match on digits
  (`+971 4 777 7333` finds `97147777333`). Max 24 h per request.
- Nothing is stored on any server: the file is built in RAM and saved only by your browser.

## Console URL in the dashboard

The dashboard embeds the console and links to it through the **Console URL** variable.
`/voip` is right for production (NPM serves the gateway on the Grafana domain). For a local test
with the gateway on port 8090 set it to `http://localhost:8090/voip` (and save the dashboard).
Embedding needs `GF_PANELS_DISABLE_SANITIZE_HTML=true` on Grafana (set in docker-compose.yml):
users who can save dashboards can then put HTML/JS in text panels.

## Deploy

### 1. 244 (monitoring)

```bash
cd /opt/private-server-monitoring && git pull
cp voip/gateway/.env.example voip/gateway/.env
# set AGENT_TOKEN (openssl rand -hex 32), same value as on the node
iptables -I DOCKER-USER -p tcp --dport 3500 ! -i tun0 -j DROP     # Loki receiver: VPN only
docker compose up -d --build voip-gateway
docker compose up -d grafana alloy loki                            # iframe setting, receiver port, 48 h retention
curl -X POST http://localhost:9090/-/reload                        # job voip_agent + alerts
```

nginx-proxy-manager → proxy host **ocubedashboards.voicemeetme.net** → Custom locations → Add:

- Location: `/voip` · Scheme `http` · Forward host `voip-gateway` · Port `8090`
- ⚙ (advanced) for that location:

  ```nginx
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection $http_connection;
  proxy_read_timeout 3600s;
  ```

### 2. my03 (trunk node)

```bash
mkdir -p /opt/voip-agent
# copy voip/agent/{Dockerfile,agent.py,voipcore.py,docker-compose.yml,.env.example}
cd /opt/voip-agent && cp .env.example .env      # set AGENT_TOKEN; Meydan values are filled in
docker compose up -d --build
docker logs -f voip-agent                        # "capturing on ens19 (10.15.228.226), ring … MB"
```

my03's firewall must allow 9479 from the VPN, next to the existing 9100 rule:

```bash
iptables -I INPUT 3 -s 10.200.26.0/23 -d 192.168.99.42/32 -p tcp -m tcp --dport 9479 -m conntrack --ctstate NEW -j ACCEPT
# and the same line in /etc/iptables/rules.v4
```

### 3. ds02 (Dubai South trunk node)

ds02 carries du_ae_trunk (044967600) on ens19; ds01 is standby (its ens19 is down).

Check first, read-only: ens19 address, the du SIP peer/port and that 244's Loki receiver is reachable:

```bash
ip -4 addr show ens19
timeout 20 tcpdump -ni ens19 -c 20 'udp and not portrange 10000-65535'   # SIP port(s) -> SIP_PORTS
curl -s -m 5 -o /dev/null -w "loki %{http_code}\n" http://10.200.26.6:3500/ready
free -g; docker --version
```

Then deploy like my03, with `voip/agent/sites/ds02.env.example` as `.env` (same AGENT_TOKEN
as my03 and the gateway):

```bash
mkdir -p /opt/voip-agent
# copy voip/agent/{Dockerfile,agent.py,voipcore.py,docker-compose.yml} and sites/ds02.env.example as .env
cd /opt/voip-agent && docker compose up -d --build
docker logs -f voip-agent                        # "capturing on ens19 (…), ring … MB"
iptables -I INPUT 3 -s 10.200.26.0/23 -d 192.168.160.7/32 -p tcp -m tcp --dport 9479 -m conntrack --ctstate NEW -j ACCEPT
# and the same line in /etc/iptables/rules.v4, after the 9100 rule
```

On 244: add `dubai_south/ds02=http://192.168.160.7:9479` to `AGENTS` in `voip/gateway/.env`, then
`docker compose up -d voip-gateway` and `curl -X POST http://localhost:9090/-/reload`.

### Local test (WSL)

Publish the gateway port with a `docker-compose.override.yml` (not committed):

```yaml
services:
  voip-gateway:
    ports:
      - "8090:8090"
```

then `docker compose up -d --build voip-gateway grafana`, log in to Grafana at localhost:3000 and
set Console URL to `http://localhost:8090/voip`.

## Agent settings (`.env`)

| Setting | Default | |
|---|---|---|
| `IFACE` | ens19 | Trunk interface |
| `SIP_PORTS` | 5060 | UDP SIP ports on IFACE |
| `RING_MB` | 1024 | RAM for RTP (~560 bytes per packet; at ~700 RTP packets/s 1 GB ≈ 25-45 min) |
| `MEM_LIMIT` | 1600m | Container RAM cap (RING_MB + ~50%) |
| `RECENT_CALLS` | 2000 | Finished calls kept in the list (metadata + SIP) |
| `LISTEN` | 192.168.99.42:9479 | API + metrics, private IP only |
| `AGENT_TOKEN` | | Shared with the gateway |
| `LOKI_URL` | 10.200.26.6:3500 | Alloy receiver on 244 over the VPN |

Codecs: G.711 A-law/µ-law are decoded (the du trunk uses A-law). Other codecs are tracked
for metrics but have no audio.

## Test without live traffic

`REPLAY_PCAP=/path/file.pcap` (plus `LOCAL_IP=10.15.228.226`) makes the agent replay a pcap at
real speed instead of capturing, so the console, live listening and replay can be tried end to end.

## Stop / remove

```bash
cd /opt/voip-agent && docker compose down        # my03: capture stops, RAM is freed
docker compose rm -sf voip-gateway               # 244
```
