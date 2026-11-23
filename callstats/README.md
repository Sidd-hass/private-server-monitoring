# Call statistics and RabbitMQ monitoring

This exporter reads Kazoo CDRs from the existing CouchDB `crossbar_listing` views and RabbitMQ metrics from `rabbitmq_prometheus`. It serves Prometheus metrics on port 9480. It does not write to either service. CDR counts represent completed call legs; in-progress calls appear after Kazoo writes their CDR.

## Local test (WSL)

Run these commands from the repository root in the WSL distribution that runs the monitoring Docker stack:

```sh
# Start the fake CouchDB, RabbitMQ, seed data, and exporter.
docker compose -f callstats/test/docker-compose.yml up -d --build

# Point local Prometheus at the test exporter instead of the Meydan target.
docker compose -f docker-compose.yml -f callstats/test/prometheus.override.yml up -d prometheus
```

The seeder creates two fake accounts, agents, a day of CDR legs, and queues. It continues adding calls and queue traffic. The RabbitMQ test backlog is deliberately temporary (it grows and is purged on a cycle).

Check the exporter at `http://127.0.0.1:9480/metrics`, Prometheus at `http://127.0.0.1:9090/targets`, and Grafana at `http://127.0.0.1:3000`. In Grafana, open **Observability → Calls & Agents (live 24 h) · RabbitMQ**. The dashboard refreshes every 30 seconds. Prometheus queries to check:

```promql
sum by (status) (kazoo_user_calls_window{window="24h"})
sum by (family) (rabbitmq_queue_family_messages_ready)
```

The test Prometheus override replaces only the `callstats` target directory. The normal Meydan target remains in the repo and is not contacted by this local setup. Avoid starting the production `callstats` compose file on a developer machine; it uses host networking and localhost service addresses.

To stop and remove only the test services and their disposable CouchDB/RabbitMQ data:

```sh
docker compose -f callstats/test/docker-compose.yml down -v
```

Then restore normal Prometheus configuration:

```sh
docker compose -f docker-compose.yml up -d prometheus
```

## Meydan deployment (run by the operator later)

Nothing in this section has been applied to Meydan. Deploy only after reviewing the local test results.

1. Copy this `callstats/` directory to `/opt/callstats` on `my01` (`192.168.99.46`). Create `/opt/callstats/.env` from `.env.example`; the Meydan defaults use CouchDB and RabbitMQ Prometheus on localhost and bind metrics to `192.168.99.46:9480`.
2. On `my01`, start it with `cd /opt/callstats && docker compose up -d --build`.
3. Allow Prometheus on the VPN to scrape the exporter. The planned host firewall rule is:

   ```sh
   iptables -I INPUT 3 -s 10.200.26.0/23 -d 192.168.99.46/32 -p tcp --dport 9480 -m conntrack --ctstate NEW -j ACCEPT
   ```

   Persist the equivalent rule in `/etc/iptables/rules.v4` according to the node's existing firewall convention.
4. Commit and deploy the repository changes to the Prometheus/Grafana host (244), then reload Prometheus with `curl -X POST http://localhost:9090/-/reload`. Grafana provisions the dashboard from `grafana/provisioning/dashboards/ocube/`.
5. Verify target `callstats` is up and open **Observability → Calls & Agents (live 24 h) · RabbitMQ**. Confirm CouchDB poll freshness and RabbitMQ metrics before relying on alerts.

## Metric and reporting limits

- One CDR is one call leg. Agent totals count legs carrying `owner_id`; trunk/IVR legs appear in the all-legs panels.
- Kazoo `call_direction=inbound` maps to outgoing for an agent, while `outbound` maps to incoming.
- The exporter holds the rolling 24-hour CDR window in memory. Its `_total` counters reset on exporter restart, so short-term trend panels warm up after startup.
- The source view does not include the queue name. This dashboard reports agent legs, not ACD queue performance.
- Queue details are grouped into queue-name families to avoid creating thousands of Prometheus series. The deepest-queue panel shows only non-empty queues, capped by `TOP_QUEUES`.
