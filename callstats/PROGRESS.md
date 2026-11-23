# Calls & Agents (live 24 h) + RabbitMQ queues: progress notes

Status on 2026-11-19. Site: **Meydan**. Everything below was built and tested in the local WSL
stack only. Nothing has been installed or changed on a live server.

## Requirement

- Real-time view of how many calls were handled, over a rolling 24 h window (refreshing).
- Breakdown **per agent / user**, by every result: answered, cancelled, dropped, busy, no answer, ...
- Monitor the **RabbitMQ queues**.
- Build and test in local WSL first; the user deploys to live afterwards.

## 1. What was found on Meydan (read-only discovery)

All checks were GET requests or listings on **my01 (192.168.99.46)**. Nothing was written there.

| Item | Finding |
|---|---|
| CouchDB | `127.0.0.1:5984`, answers without credentials |
| Accounts | `544370e9c33f4d679f0f1d37778d3c7c` = **meydan** (main, ~1.41M docs in `-202611`), `83e47f11c3238e0086fff99a3574e878` = admin |
| CDR source | `_design/cdrs/_view/crossbar_listing` in the monthly DB `account/54/43/70e9…-YYYYMM`. **Already indexed** (update_seq up to date) because crossbar uses it. Gives owner_id, call_direction, hangup_cause, billing_seconds, duration. |
| Do not use | `_design/fonouc_grafana` and `_design/fonouc_agent_stats` exist but have **never been indexed** (update_seq 0). Querying them would make CouchDB index 1.4M docs, a heavy load. |
| Users | `_design/users/_view/crossbar_listing` in `account/54/43/70e9…`: 1028 users, first_name, last_name, presence_id (extension) |
| Volume | ~950 CDR legs/hour, ~91 active users/hour. `billing_seconds` is a **string**. |
| Hangup causes seen | NORMAL_CLEARING, ORIGINATOR_CANCEL, NORMAL_UNSPECIFIED, USER_BUSY, UNALLOCATED_NUMBER, NO_ANSWER, NO_USER_RESPONSE, LOSE_RACE |
| RabbitMQ | 4.1.3 on the host, `rabbitmq_prometheus` on `127.0.0.1:15692` (no auth). **~5050 queues** (per-connection kazoo_apps@…, ecallmgr@…, goportalbackend-…). Baseline: 0 ready, 0 unacked, 15 empty queues without a consumer, 0 stuck. |
| Prometheus today | Meydan has only node / ping / SSL jobs. **No RabbitMQ or Kazoo metrics yet.** |

The two view definitions (code only, no call data) were copied into `test/fixtures/` so the
local CouchDB behaves like Meydan's.

## 2. What was built (local repo)

```
callstats/
  exporter.py              exporter, Python standard library only, read-only
  Dockerfile               python:3.12-alpine, runs as nobody
  docker-compose.yml       node deployment (host network, read-only, 256 MB / 0.5 CPU cap)
  .env.example             Meydan values
  PROGRESS.md              this file
  test/
    docker-compose.yml     local test: couchdb:3.4 + rabbitmq:4.1-management + seeder + exporter
    seed.py                fake accounts, 30 agents, 24 h of CDRs using Meydan's real mix, live calls every 5 s, ~290 fake queues
    fixtures/design_cdrs.json, design_users.json   real Meydan view definitions
    prometheus.override.yml  local only: points the callstats job at the test exporter
    targets/local.json
prometheus/prometheus.yml           + job 'callstats' (file_sd: targets/callstats/*.json, 30 s)
prometheus/targets/callstats/meydan.json   192.168.99.46:9480 (cluster=meydan, server_name=my01)
prometheus/alert.rules.yml          + group callstats_alerts (5 rules)
```

### How the exporter works

- At start it loads the last 24 h of CDR rows from `crossbar_listing`. After that it polls every
  30 s and only fetches rows newer than the last one it has (with a 5 min overlap). Rows stay in RAM
  (~20k rows); rows older than 24 h are dropped.
- It queries with `stable=true&update=lazy`, so a request never waits for, or forces, a view index build.
- Month boundaries are handled: it queries every monthly DB the 24 h window touches; a missing one (404) is skipped.
- User names and extensions are refreshed every 10 min.
- One CDR = one call leg. Legs with an `owner_id` are counted per user. Direction is as the user sees it:
  `call_direction=inbound` means **outgoing** (the user's phone placed it), `outbound` means **incoming** (the switch rang the user).

### Result classification (`status` label)

| status | rule |
|---|---|
| answered | billed > 0 and NORMAL_CLEARING / NORMAL_UNSPECIFIED / ORIGINATOR_CANCEL |
| dropped | billed > 0 and any other cause (media timeout, network error, …) |
| cancelled | not billed, ORIGINATOR_CANCEL (caller hung up before answer) |
| no_answer | NO_ANSWER, NO_USER_RESPONSE, ALLOTTED_TIMEOUT, PROGRESS_TIMEOUT |
| unanswered | not billed, NORMAL_CLEARING / NORMAL_UNSPECIFIED (ended before anyone answered) |
| busy | USER_BUSY |
| rejected | CALL_REJECTED |
| answered_elsewhere | LOSE_RACE (rang several phones, another one answered) |
| failed | anything else unbilled (UNALLOCATED_NUMBER, temporary failure, …) |

The raw `hangup_cause` is kept as a label too, so every individual result is visible.

### Metrics

| Metric | Labels | Use |
|---|---|---|
| `kazoo_user_calls_window` | window (`24h` / `today` = since 00:00 Dubai), account, user_id, user, extension, call_type, status, hangup_cause | per-agent counts |
| `kazoo_user_talk_seconds_window` | window, account, user, extension, call_type | talk time |
| `kazoo_calls_window` | window, account, leg_direction, status, hangup_cause | all legs incl. trunks / IVR |
| `kazoo_user_calls_total`, `kazoo_user_talk_seconds_total` | counters (since exporter start) | per-15-min trends |
| `kazoo_calls_last_cdr_timestamp_seconds`, `kazoo_calls_legs_in_memory` | | freshness |
| `rabbitmq_*` (passed through from `/metrics`) | | totals, global publish/deliver/ack rates; also feeds the existing `rabbitmq_alerts` group |
| `rabbitmq_queue_family_{queues,messages_ready,messages_unacked,consumers,queues_without_consumers,queues_stuck}` | vhost, family | ~5000 queues grouped by name pattern (`<pid>`, `<id>`, `<uuid>`) |
| `rabbitmq_top_queue_messages`, `rabbitmq_top_queue_consumers` | queue, family | 25 deepest non-empty queues |
| `callstats_up`, `callstats_rabbitmq_up`, `callstats_poll_errors_total`, `callstats_poll_duration_seconds` | | exporter health |

### Alerts (`callstats_alerts`)

| Alert | Fires when |
|---|---|
| CallStatsExporterDown | exporter not scraped or not reading CouchDB for 5 min |
| CallStatsNoNewCalls | newest CDR of an account (not admin) older than 1 h, for 10 min |
| CallStatsRabbitMQUnreachable | :15692 unreadable for 5 min |
| RabbitMQQueueStuckNoConsumer | a queue family has queues holding messages with no consumer, for 10 min |
| RabbitMQQueueFamilyBacklog | > 100 ready messages in one family for 5 min (Meydan normal = 0) |

## 3. Local test results (WSL)

- Images were pulled anonymously (`DOCKER_CONFIG=/tmp/anon-docker`) because the saved Docker Hub login in WSL is rejected.
- The local monitoring stack was found stopped and was started again with the override:
  `docker compose -f docker-compose.yml -f callstats/test/prometheus.override.yml up -d`.
- Seeder: 6275 legs back-filled, 291 queues declared, live calls added.
- Exporter: 2 accounts, 34 users, ~6090 legs in the window, live rows arriving every poll, 18 queue families, **0 poll errors**, poll ~0.15 s.
- Two bugs were found and fixed: `user:pass@` in the CouchDB URL (now sent as a Basic auth header), and RabbitMQ answering 406 to `Accept: application/json` (now `text/plain`).
- `promtool`: config valid, 70 rules OK. Local Prometheus target `callstats-exporter:9480` is **up**. The query
  `sum by (status) (kazoo_user_calls_window{window="24h"})` returns all 9 statuses.

## 4. Dashboard and deployment documentation

1. **Dashboard.** The JSON is now present at `grafana/provisioning/dashboards/json/calls_agents_rabbitmq.json`, uid `callstats-agents`,
   title "Calls & Agents (live 24 h) · RabbitMQ", refresh 30 s, filters Site / Window (24 h or today) / Account / Direction / Agent:
   - Tiles: total, answered, cancelled, no answer, busy/rejected, dropped, failed, answer rate %, talk time, avg talk, active agents, calls in the last 15 min.
   - Calls per 15 min stacked by result, plus a result donut.
   - **Per-agent table**: agent, ext, total, incoming, outgoing, one column per result, answer %, talk time (sortable, filterable, with a totals footer).
   - Top 15 agents stacked bar, and a top-10 agents trend.
   - Hangup-cause tables (agent legs, and all legs).
   - RabbitMQ: tiles (queues, ready, unacked, stuck, without consumer, connections), throughput, backlog per family, families table, deepest queues.
   - Exporter health row (collapsed).
   - Fixed colour per result everywhere (answered green, dropped red, …).
2. Load it into the local Grafana, check every panel with the test data, and take screenshots.
3. Fire each alert locally (stop the exporter; leave the test backlog queue without a consumer) and check it reaches Alertmanager.
4. `callstats/README.md` contains local test and future Meydan deployment steps.
5. **Then the user deploys to live** (planned steps):
   - my01: copy `callstats/` to `/opt/callstats`, `cp .env.example .env`, then `docker compose up -d --build`.
   - my01 firewall: allow 9480 from the VPN, like the voip agent rule:
     `iptables -I INPUT 3 -s 10.200.26.0/23 -d 192.168.99.46/32 -p tcp --dport 9480 -m conntrack --ctstate NEW -j ACCEPT` (and in `/etc/iptables/rules.v4`).
   - 244: `git pull`, then `curl -X POST localhost:9090/-/reload` (job + alerts), Grafana picks up the dashboard by itself.
6. Clean up the local test: `cd callstats/test && docker compose down -v`, and restart Prometheus without the override.

### Follow-up in Windows workspace (2026-11-19)

- Confirmed the dashboard JSON exists, parses successfully, has 34 panels, refreshes every 30 seconds, and includes Site / Window / Account / Direction / Agent filters.
- Added `callstats/README.md` with local test commands, checks, safe teardown, deployment steps, and reporting limits.
- The forwarded local endpoints responded: Grafana `/api/health` and Prometheus `/-/healthy` returned 200, as did the exporter `/metrics`. Prometheus target `callstats` was **up**; `callstats_up=1`, `callstats_rabbitmq_up=1`, and the test broker reported 291 queues. The rolling 24 h query returned all nine statuses.
- Imported the dashboard into the local Grafana database for review. All **75 PromQL expressions** across its panels parsed successfully against the local Prometheus API. The five `callstats_alerts` rules were loaded.
- `git diff --check` passed. WSL verification commands still cannot be run from this Windows session: `wsl.exe` cannot launch `bash` (`execvpe ... No such file or directory`), and Python/Docker are not installed on the Windows PATH. Alert firing tests and a rendered screenshot remain to be run in WSL; Grafana's local render endpoint returned HTTP 500 (renderer unavailable).

## Notes / limits

- Counts are for **finished** calls (a CDR is written at hangup). Calls in progress are not counted until they end.
- `kazoo_user_calls_total` counters start at 0 when the exporter starts, so the 15-min trend panels fill up from deploy time. The 24 h / today numbers are complete straight away.
- Calls that go through a queue are counted for the agent whose phone took the leg. The queue name is not in `crossbar_listing`, so this is not a per-queue (ACD) report.

## Other work in this session (for the record)

- **Dubai South incident log, 11 Nov** (`Downloads/DS_UCP_incident_2026-11-11/`). The portal is on ds01 (91.72.107.250).
  The server sent every JS bundle in full (~30 ms backend); delivery was slow only to the Egyptian oCube IPs 41.33.x / 41.65.x, and was already slow on 7 and 9 Nov. Conclusion: a network-path problem.
- **voip-gateway on 244.** Bound to `127.0.0.1:8090` (no longer reachable from the internet). The console has to be served
  through nginx-proxy-manager at `/voip` (custom location → `voip-gateway:8090`, plus the WebSocket lines), with Console URL `/voip`.
  Checked: the nginx-proxy-manager container reaches `voip-gateway:8090` (HTTP 200). The `/voip` location still had to be saved in nginx-proxy-manager at the last check.
