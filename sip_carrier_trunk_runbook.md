# Carrier Trunk (Tata) Monitoring: UCPIN / UCDEMO / UCPMED

These sites have no trunk container. The carrier (Tata) is an IP-authenticated Kazoo offnet
resource, so `scripts/freeswitch_trunk_metrics.sh` (gateway registration) does not apply.
`scripts/sip_peer_metrics.py` covers them instead.

Grafana: **Observability → SIP Carrier Trunks (Tata)** (`uid sip-carrier-trunks`).
Alerts: group `sip_carrier_peer_alerts` in `prometheus/alert.rules.yml`. They route to each
cluster's Flock channel by the `cluster` label, which is already set by the `debian_servers` job.

```
sip_peer_metrics.py (cron, every minute, read-only checks)
   ├─ /opt/node-exporter/textfile/sip_peer.prom   → node_exporter :9100 → Prometheus (job debian_servers)
   └─ /var/log/sip_peer_metrics.log               → Alloy kazoo-log-shipper → Loki {service="sip_trunk"}
```

## What it answers

| Question | How |
|---|---|
| Is anyone blocking the trunk? Whose end? | `sip_peer_blocked{blocked_by="local"\|"remote", reason}` and `sip_peer_block_state` (0 ok, 1 carrier end, 2 our end) |
| Our end (`local`) | fail2ban ban (`fail2ban:<jail>`), DROP/REJECT ipset (`ipset:blacklist_ip`, `ipset:f2b-*`), iptables `-s` DROP/REJECT rule, Kamailio secban `ipban`, interface to the carrier down (`iface_down:eth0`), Kamailio/FreeSWITCH not listening on the carrier-facing IP (`listener_missing:freeswitch:11000`) |
| Carrier end (`remote`) | No reply to SIP OPTIONS although it answered in the last 7 days (`options_timeout`), ICMP port unreachable, 401/403/407 to OPTIONS, 3+ `CALL_REJECTED` call legs in 5 min (`auth_failures`) |
| Which interface / IP the trunk uses | `sip_peer_info{iface, src_ip}` (from `ip route get <carrier>`) |
| Listening sockets and their interface | `sip_listener_info{process, address, port, iface}`; expected ones: `sip_listener_expected_up` |
| Interface up/down | `sip_iface_up{iface, address, operstate}`, every real NIC (eth1 = cluster net on UCPIN) |
| Whitelisted? | `sip_peer_whitelisted{list}`: `fail2ban:<jail>` ignoreip, `kamailio:trunk_ip`, `ipset:trunk_ip/whitelist_*`, `haproxy:whitelist.lst` |
| Authentication failures from Tata | `sip_peer_auth_failures_total{reason}` + Loki log lines `event="call_failure"` with `auth=yes` |
| Call outcomes | `sip_peer_final_responses_total{code}`, `sip_peer_hangups_total{cause}` (from `kazoo-debug.log`) |

The OPTIONS ping is sent from the node's carrier-facing IP (source port is random, not 5060).
If Tata never answers OPTIONS, `options_timeout` is never raised (it needs a reply in the last
7 days). Only the call-leg checks then cover the carrier end. Set `none` instead of `options`
on the `peer` line to stop the ping.

## Alerts

| Alert | Severity | Fires when |
|---|---|---|
| `SipCarrierBlockedOurEnd` | critical | any `local` reason for 1 min. The summary names the mechanism |
| `SipCarrierTunnelDown` | critical | IPsec SA or a tunnel interface down for 1 min (UCPMED) |
| `SipCarrierBlockedCarrierEnd` | critical | any `remote` reason for 3 min (and nothing local) |
| `SipCarrierAuthFailures` | warning | a 401/403/407 / CALL_REJECTED leg in 10 min (below the "blocked" threshold) |
| `SipCarrierCallsBarredHigh` | warning | > 50 % of ≥ 20 legs end `*_CALL_BARRED` for 15 min |
| `SipCarrierInterfaceDown` | critical | the interface routing to the carrier is down |
| `SipListenerMissing` | critical | an expected Kamailio/FreeSWITCH listener is gone for 2 min |
| `SipNodeInterfaceDown` | critical | any NIC that was up in the last day is down for 2 min (e.g. ucpin13 eth1, 22 Nov) |
| `SipCarrierNotWhitelisted` | warning | carrier missing from fonouc-sip ignoreip or Kamailio trunk_ip for 15 min |
| `SipCarrierMetricsStale` | warning | `sip_peer.prom` older than 5 min |

## Per-site values (found 2026-11-25, read-only)

| Site | Nodes | Carrier peer | textfile dir | Status |
|---|---|---|---|---|
| UCPIN | ucpin11 .57 (SSH 2486), ucpin12 .59, ucpin13 .60 | Tata `27.107.220.70:5095` (offnet `virtual2_tata-sip`, `-myflotpri`) | `/opt/node-exporter/textfile` | config ready: `scripts/sip_peers/ucpin.conf` |
| UCDEMO | uc11 181.214.10.237, uc12 .238 | **no Tata trunk** (no call legs, no ACL named Tata). Two trusted peers: `mc_int_new` 103.173.93.109 = office/NAT where UCP softphones register over WSS (ping off), `mcint1` 203.199.43.5 (ping 5060, idle) | `/opt/node-exporter/textfile` | config ready: `scripts/sip_peers/ucdemo.conf` |
| UCPMED | **ucpmed02 only** (.232); ucpmed01 cannot join the tunnel | **Etisalat** One Access SBC `192.168.3.1:5060` over IPsec `etisalat-office` (xfrm202) + VXLAN (vxlan0, 192.168.3.2); trunk FreeSWITCH `fonouc_freeswitch_trunk` from_external :5060 / to_external :5080 | `/var/lib/node_exporter/textfile` | config ready: `scripts/sip_peers/ucpmed.conf` |

UCPMED has only the `sshd` fail2ban jail and no Kamailio secban (`No such htable`), so neither
whitelist is checked there. Instead its config adds:

- `tunnel ipsec etisalat-office`, `iface xfrm202`, `iface vxlan0`: any of them down gives the verdict
  **TUNNEL** (`sip_peer_block_state` 3, alert `SipCarrierTunnelDown`), never "carrier end", and the
  OPTIONS ping is skipped. The far side (Meydan's FortiGate) can also cause it.
- `allow udp 5060`, `allow udp 50000`: the ucpmed-tunnel iptables ACCEPT rules for the SBC
  (vxlan0 drops everything else). A missing rule = **OUR END** (`fw_allow_missing:udp/5060`).
- `log /var/log/freeswitch_trunk/kazoo-debug.log`: the trunk FreeSWITCH log (SBC legs are
  `sofia/from_external/<number>@192.168.3.1`).

Observations made while building this (nothing was changed):

- UCPIN `iptables`: UDP **11000** (FreeSWITCH sipinterface_1) goes to chain `TRUNKS`, which
  only accepts the ipsets `trunk_ip` / `trunk_net`. **Both sets are empty**, so every *new*
  packet to 11000 is REJECTed (42 so far on ucpin12). Replies to calls FreeSWITCH starts are
  ESTABLISHED and pass. A request Tata sends to :11000 after the conntrack entry expired
  (e.g. a late BYE or re-INVITE) is rejected. The dashboard shows `ipset:trunk_ip = NO`.
- About 26 % of recent Tata legs on ucpin12 ended `OUTGOING_CALL_BARRED`, 11 % `NO_USER_RESPONSE` (480).

## Deploy (per node, root). Do not run until the site's peer values are confirmed

Same pattern as `trunk_monitoring_runbook.md`. Nothing restarts; Kazoo is not touched.

```bash
# 0. right server?
hostname -f; ip -br addr

# 1. script + config
install -m 0755 sip_peer_metrics.py /usr/local/sbin/sip_peer_metrics.py
install -m 0644 sip_peers/ucpin.conf /etc/sip_peer_metrics.conf      # ucdemo.conf / ucpmed.conf

# 2. dry run: prints metrics, writes nothing (sends one OPTIONS to the carrier)
/usr/local/sbin/sip_peer_metrics.py --stdout | grep -E 'block_state|blocked|options_status|iface_up|listener'
#    Expect: sip_peer_block_state ... 0 and the right iface/src_ip in sip_peer_info.

# 3. cron
echo '* * * * * root /usr/local/sbin/sip_peer_metrics.py >/dev/null 2>&1' > /etc/cron.d/sip_peer_metrics

# 4. after 2 min: the file and node_exporter
ls -la /opt/node-exporter/textfile/sip_peer.prom      # UCPMED: /var/lib/node_exporter/textfile
curl -s localhost:9100/metrics | grep -c '^sip_peer_'
```

The first run starts reading `kazoo-debug.log` at its end, so call-leg counters begin at 0.

### Logs in the dashboard (Loki)

The log panels need the Alloy shipper (`alloy/kazoo-node`, container `kazoo-log-shipper`) on
each node. Its `sip_trunk` pipeline tails `/var/log/sip_peer_metrics.log` (the script rotates
it at 20 MB). The same shipper also sends the Kazoo/CouchDB/FreeSWITCH/RabbitMQ error lines
(already filtered and rate-capped). Without it, metrics and alerts still work and only the log
panels stay empty.

These sites have no VPN to 244, so the shipper pushes over HTTPS:

```
kazoo-log-shipper (node) ──HTTPS + basic auth──► nginx-proxy-manager /loki-push/  (node IP allowlist)
                                                  └─► alloy:3501 (244, npm network, not published)
                                                        keeps job="kazoo-node", cluster ucpin|ucdemo|ucpmed
                                                        └─► loki:3100
```

**On 244, once:**

```bash
cd /opt/private-server-monitoring && git pull
# password for the node shippers (keep it for the node .env files)
htpasswd -c -B /opt/docker/nginx-proxy-manager/data/loki-push.htpasswd kazoo-logs
chmod 644 /opt/docker/nginx-proxy-manager/data/loki-push.htpasswd
docker compose up -d alloy            # joins the npm network, receiver on 3501
```

nginx-proxy-manager → Proxy Hosts → ocubedashboards.voicemeetme.net → Edit → Advanced: paste the
whole updated `nginx-proxy-manager/ocubedashboards-advanced.conf` (it now has the `/loki-push/`
location) → Save.

Check from a UCPIN node (expect 401 without the password, 200 with it):

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://ocubedashboards.voicemeetme.net/loki-push/ready
curl -s -o /dev/null -w "%{http_code}\n" -u kazoo-logs:'<password>' https://ocubedashboards.voicemeetme.net/loki-push/ready
```

From any IP not in the allowlist it is 403.

**On each node** (copy `alloy/kazoo-node/` to `/opt/server-monitoring/alloy` first):

```bash
cd /opt/server-monitoring/alloy
cat > .env <<'EOF'
LOKI_URL=https://ocubedashboards.voicemeetme.net/loki-push/loki/api/v1/push
LOKI_USER=kazoo-logs
LOKI_PASSWORD=<the htpasswd password>
SERVER_NAME=ucpin11
CLUSTER=ucpin
EOF
chmod 600 .env
docker compose up -d
docker logs --tail 20 kazoo-log-shipper     # no "401"/"403"/"connection refused"
```

`SERVER_NAME` must be the node's name (ucpin11/12/13, uc11/12, ucpmed01/02) so the log panels
match the `server` variable. The container is capped at 256 MB RAM and 0.5 CPU.

### Monitoring server (244)

```bash
cd /opt/private-server-monitoring && git pull
curl -X POST http://localhost:9090/-/reload      # new alert group
# Grafana picks the dashboard up from provisioning within 10 s.
```

## Tested locally (WSL Docker, 2026-11-25; UCPMED tunnel/firewall checks 2026-11-27: baseline none,
IPsec down -> tunnel + SipCarrierTunnelDown firing, SBC silent -> remote/options_timeout, allow rule removed ->
local/fw_allow_missing; all dashboard queries return data for cluster ucpmed)

Fake SIP peer + stubbed fail2ban-client/kamcmd/ipset/iptables-save/ss, real node_exporter,
Prometheus 2.51, Loki 3.5.8, Alloy 1.11.3, Grafana. Checked: baseline → no alert; 5×403 legs →
`remote/auth_failures` + log lines; fail2ban ban → `local/fail2ban:fonouc-sip`; blacklist ipset →
`local/ipset:blacklist_ip`; peer silent → `remote/options_timeout`; FreeSWITCH listener gone →
`local/listener_missing:freeswitch:11000`. promtool: metrics and rules OK. Not run on a real node yet.
