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
| UCDEMO | uc11 181.214.10.237, uc12 .238 | **unconfirmed**: `203.199.43.5` (Tata Communications range, in fonouc-sip ignoreip), port assumed 5060 | `/opt/node-exporter/textfile` | confirm IP/port first |
| UCPMED | ucpmed01 .231, ucpmed02 .232 | **unknown**: no Tata IP in fail2ban, HAProxy or recent logs | `/var/lib/node_exporter/textfile` (dubai-south-node-exporter) | fill in `scripts/sip_peers/ucpmed.conf` |

UCPMED has only the `sshd` fail2ban jail, so `fail2ban:fonouc-sip` whitelist rows do not
exist there and `SipCarrierNotWhitelisted` only checks Kamailio trunk_ip.

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

The log panels need the Alloy shipper (`alloy/kazoo-node`) on the node. It is not running on
UCPIN/UCDEMO/UCPMED yet. Deploy it as the header of `alloy/kazoo-node/docker-compose.yml` says
(copy the folder, `.env` from `.env.example`, `docker compose up -d`). The `sip_trunk` pipeline
in `config.alloy` tails `/var/log/sip_peer_metrics.log` (the script rotates it at 20 MB).
Without Alloy, metrics and alerts still work and only the log panels stay empty.

### Monitoring server (244)

```bash
cd /opt/private-server-monitoring && git pull
curl -X POST http://localhost:9090/-/reload      # new alert group
# Grafana picks the dashboard up from provisioning within 10 s.
```

## Tested locally (WSL Docker, 2026-11-25)

Fake SIP peer + stubbed fail2ban-client/kamcmd/ipset/iptables-save/ss, real node_exporter,
Prometheus 2.51, Loki 3.5.8, Alloy 1.11.3, Grafana. Checked: baseline → no alert; 5×403 legs →
`remote/auth_failures` + log lines; fail2ban ban → `local/fail2ban:fonouc-sip`; blacklist ipset →
`local/ipset:blacklist_ip`; peer silent → `remote/options_timeout`; FreeSWITCH listener gone →
`local/listener_missing:freeswitch:11000`. promtool: metrics and rules OK. Not run on a real node yet.
