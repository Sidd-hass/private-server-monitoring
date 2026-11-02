# App-node log shipper (Docker + pm2 + systemd)

Ships a server's logs to Loki so they can be browsed **server-wise then app-wise** in Grafana.
Use this on hosts whose apps run under Docker, pm2 and/or systemd. (Kazoo nodes use the
separate `../kazoo-node` shipper, which is errors-only per service.)

## What it collects

| job      | extra labels                         | volume            |
|----------|--------------------------------------|-------------------|
| `docker` | `container_name`                     | all lines         |
| `pm2`    | `app`, `stream` (out\|error)         | all lines         |
| `system` | `systemd_unit`, `transport`, `priority` | warning and above |

All carry `server_name` and `cluster` (set in `.env`) to match the host's Prometheus labels,
plus **`service_name`** (docker→container, pm2→app, systemd→unit) — the label Grafana Logs
Drilldown groups by.

## Viewing in Grafana Logs Drilldown

Drilldown (Grafana 11.3+, bundled) → **Logs** → pick the `service` tab: every app on the host
(docker containers, pm2 apps, systemd units) appears as a service with its own volume chart and
log stream. Filter to one host first with the `server_name` label tab. This is the view in the
reference screenshot. (If the nav has no Drilldown, the Grafana is older than 11.3.)

## Deploy (pilot on one server)

1. Copy this folder to the server, e.g. `/opt/server-monitoring/alloy-app`.
2. `cp .env.example .env` and set `LOKI_URL`, `SERVER_NAME`, `CLUSTER`, and `PM2_LOG_DIR`
   (find pm2's path with `ls ~/.pm2/logs` or `pm2 info <app> | grep 'log path'`).
3. `docker compose up -d`
4. Check it is shipping: `docker logs app-log-shipper` (no repeated push errors), then in
   Grafana **Explore** pick the Loki datasource and run `{server_name="<SERVER_NAME>"}`.

## Viewing in Grafana (Explore)

Server-wise, then drill into an app:

```logql
# everything from the server
{server_name="my01"}

# one source type
{server_name="my01", job="docker"}
{server_name="my01", job="pm2"}
{server_name="my01", job="system"}

# a specific app
{server_name="my01", job="docker", container_name="api"}
{server_name="my01", job="pm2", app="api-server"}
{server_name="my01", job="pm2", app="api-server", stream="error"}
{server_name="my01", job="system", systemd_unit="nginx.service"}

# errors across every source on the server
{server_name="my01"} |~ "(?i)(error|fail|panic|fatal|exception)"
```

## Local test (WSL) — before pushing to live

Validates `config.alloy` end-to-end against the local stack, without publishing ports or
touching the main compose. All three sources work in WSL (systemd journal needs
`systemd=true` in `/etc/wsl.conf` and persistent journald; otherwise that pipeline is just empty).

```bash
# 1) Bring up the central stack (Loki/Grafana/Prometheus) in WSL
cd monitoring && docker compose up -d
docker network ls | grep monitoring        # confirm the network name (usually monitoring_monitoring)

# 2) (optional) generate pm2 logs to test that pipeline
#    pm2 start "node -e 'setInterval(()=>console.error(\"test error\"),1000)'" --name demo-app

# 3) Run the shipper joined to the stack network so it reaches loki:3100 directly.
#    This reuses the real config.alloy; only the networking differs from production.
cd monitoring/alloy/app-node
docker run -d --name app-log-shipper-test \
  --network monitoring_monitoring \
  -e LOKI_URL=http://loki:3100/loki/api/v1/push \
  -e SERVER_NAME=wsl-test -e CLUSTER=lab \
  -v "$PWD/config.alloy:/etc/alloy/config.alloy:ro" \
  -v /var/log:/var/log:ro \
  -v "$HOME/.pm2/logs:/pm2logs:ro" \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  grafana/alloy:v1.11.3 \
  run --server.http.listen-addr=127.0.0.1:12345 --storage.path=/var/lib/alloy/data /etc/alloy/config.alloy

# 4) Verify
docker logs app-log-shipper-test            # no repeated push errors
#    Grafana (http://localhost:3000) -> Explore -> Loki:
#      {server_name="wsl-test"}
#      {server_name="wsl-test", job="docker"}
#      {server_name="wsl-test", job="pm2", app="demo-app"}

# 5) Clean up the test container (the standalone compose is what goes to live)
docker rm -f app-log-shipper-test
```

On the live server you don't use the command above — you deploy the folder with
`docker compose up -d` (host networking, real `.env`), exactly as in the Deploy section.

## Notes
- Docker/pm2 have a high rate cap (1000 lines/s, burst 5000) as a crash-loop guardrail only;
  normal logs are never dropped. Lower it in `config.alloy` if a host is very chatty.
- Loki retention is set centrally — shipping *all* docker/pm2 logs across many hosts grows
  storage quickly, so roll out host-by-host and watch Loki disk usage.
