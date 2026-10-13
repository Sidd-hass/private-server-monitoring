# SIP Trunk Monitoring — Per-Node Runbook

How to add a cluster's trunk container (`fonouc_freeswitch_trunk`) to the central monitoring
(Prometheus `181.214.10.244:9090` → Grafana **OCube → Public & Private Infrastructure Availability → SIP Trunks**).

Do this once on every node that runs the trunk container. It takes about 10 minutes per node.

---

## How it works

```
fonouc_freeswitch_trunk ──fs_cli (ESL 8023)──► /usr/local/sbin/fs_trunk_metrics.sh   (cron, every minute)
                                                     │ writes
                                                     ▼
                         /var/lib/node_exporter/textfile/freeswitch_trunk.prom
                                                     │ read via the /host mount
                                                     ▼
                         node_exporter :9100  (existing container, one extra flag)
                                                     │ collected every 15s (job debian_servers)
                                                     ▼
                         Prometheus 181.214.10.244 ──► Grafana dashboard
```

- Only the **trunk** container (`fonouc_freeswitch_trunk`, ESL port 8023) is queried.
  The Kazoo media container `fonouc_freeswitch` is never touched.
- No new ports, firewall rules or Prometheus changes: the central Prometheus already
  collects every node's `:9100` with `cluster` and `server_name` labels.
- The script is `scripts/freeswitch_trunk_metrics.sh` in this repo.

### Metrics produced

| Metric | Meaning |
|---|---|
| `trunk_container_running` | 1 = trunk container running, 0 = stopped |
| `trunk_container_start_time_seconds` | When the container last started (dashboard shows container uptime) |
| `trunk_container_restart_count` | Docker restart count |
| `freeswitch_trunk_scrape_ok` | 1 = `fs_cli` answered on ESL 8023, 0 = not answering |
| `freeswitch_gateway_up{gateway,number,profile}` | **1 = trunk UP, 0 = DOWN** (main metric) |
| `freeswitch_gateway_info{gateway,state,proxy}` | Registration state text (REGED, FAIL_WAIT, …) |
| `freeswitch_gateway_uptime_seconds{gateway}` | Seconds since the trunk registered |
| `freeswitch_gateway_calls_total{gateway,direction,result}` | Calls in/out, all/failed (failed includes busy/no-answer) |

---

## Rollout status

| Cluster | Node | IP | Trunk(s) | Status |
|---|---|---|---|---|
| SPC | sj02 | 172.16.200.103 | `du_ae_trunk_2` (065797100) UP, `du_ae_trunk` (065100200) FAIL_WAIT | ✅ Done 2026-10-13 |
| Meydan | my03 | 192.168.99.42 | `du_ae_trunk` (047777200) | ✅ Done 2026-10-13 |
| Meydan | gw | 172.21.2.17 | none — trunk container runs with **0 gateways** (standby) | ➖ Not needed |
| SHAMS | uc01 | 172.16.125.40 | `du_ae_trunk` (065794300, profile `from_du`) | ✅ Done 2026-10-13 |
| Dubai South | ds02 | 192.168.160.7 | `du_ae_trunk` (044967600) | ⏳ Pending |
| Dubai South | ds01 | 192.168.160.6 | none — container running, 0 gateways (standby) | ⏳ Pending (install for failover visibility) |
| UCPIN / UCDEMO / UCPMED | — | — | no trunk container (carrier connects by IP) | ➖ Needs a different check |
| Tahseel / Cogent | — | — | not checked (VPN unreachable) | ❓ |

Update this table as each node is finished.

---

## Values you need per node

| Placeholder | Where it comes from | Example (sj02) |
|---|---|---|
| `NODE_IP` | The node's private IP (the one Prometheus collects from) | `172.16.200.103` |
| `EXPORTER` | node_exporter container name in `docker ps` | `medan-node-exporter` (SPC/Meydan/SHAMS), `dubai-south-node-exporter` (Dubai South) |
| `DIR` | Step 2 output | `/opt/server-monitoring` |
| `SERVICE` | Step 2 output — the **compose service** name, not the container name | `node-exporter` |

---

## Steps

Run everything as root on the node. After each step, compare with **Expect**; if it differs, stop.

### Step 0 — Make sure you are on the right server

Several clusters have a host called `uc01`, so check the full name and IP:

```bash
hostname -f
hostname -I
```

**Expect:** the cluster name in the hostname (e.g. `...ira-shams-sj...`) and `NODE_IP` in the list.

### Step 1 — Confirm the trunk container and its gateways

```bash
docker ps --format '{{.Names}} {{.Status}}' | grep freeswitch_trunk
docker exec fonouc_freeswitch_trunk /opt/freeswitch/bin/fs_cli -P 8023 -x "sofia status gateway"
```

**Expect:** `fonouc_freeswitch_trunk Up ...` and one or more gateway lines such as
`from_external::du_ae_trunk  sip:065794300@du.ae  REGED`.

- **No container** → this node is not a trunk node; skip it.
- **`0 gateways`** → standby node. Installing is optional but recommended (failover becomes
  visible). The dashboard shows it under *Trunk Container Status* only while its container runs.

### Step 2 — Read node_exporter's settings (changes nothing)

```bash
docker inspect EXPORTER --format 'DIR={{index .Config.Labels "com.docker.compose.project.working_dir"}} SERVICE={{index .Config.Labels "com.docker.compose.service"}}
CMD={{json .Config.Cmd}}'
```

**Expect:**

```
DIR=/opt/server-monitoring SERVICE=node-exporter
CMD=["--path.procfs=/host/proc","--path.sysfs=/host/sys","--path.rootfs=/host","--web.listen-address=NODE_IP:9100"]
```

- `DIR` empty → the container is **not** compose-managed; see [Not compose-managed](#not-compose-managed).
- `--path.rootfs=/host` missing → the host disk is not mounted; see [No /host mount](#no-host-mount).

### Step 3 — Install the script

Copy the contents of `scripts/freeswitch_trunk_metrics.sh` from this repo.

```bash
cd DIR
nano freeswitch_trunk_metrics.sh          # paste, save (Ctrl+O, Enter, Ctrl+X)
sed -i 's/\r$//' freeswitch_trunk_metrics.sh
install -m 0755 freeswitch_trunk_metrics.sh /usr/local/sbin/fs_trunk_metrics.sh
mkdir -p /var/lib/node_exporter/textfile
bash -n /usr/local/sbin/fs_trunk_metrics.sh && echo SYNTAX-OK
grep -c 'trunk_container_running' /usr/local/sbin/fs_trunk_metrics.sh
grep -n '^esc()' /usr/local/sbin/fs_trunk_metrics.sh
```

**Expect:** `SYNTAX-OK`, `3`, and the `esc()` line exactly:

```
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
```

(Pasting sometimes drops backslashes — if this line differs, re-paste.)

### Step 4 — Dry run (prints only, writes nothing)

```bash
/usr/local/sbin/fs_trunk_metrics.sh --stdout | grep -v '^#'
```

**Expect:**

```
trunk_container_running{container="fonouc_freeswitch_trunk"} 1
freeswitch_trunk_scrape_ok 1
freeswitch_gateway_up{gateway="du_ae_trunk",profile="from_external",number="0657..."} 1
freeswitch_gateway_info{gateway="du_ae_trunk",state="REGED",proxy="sip:du.ae"} 1
...
```

A standby node shows the first two lines and **no** `freeswitch_gateway_*` lines — that is correct.

### Step 5 — Run it every minute

```bash
echo '* * * * * root /usr/local/sbin/fs_trunk_metrics.sh >/dev/null 2>&1' > /etc/cron.d/fs_trunk_metrics
```

Wait **at least one full minute** (cron fires at the start of each minute), then:

```bash
ls -l --time-style=+%T /var/lib/node_exporter/textfile/
date +%T
journalctl -u cron --since "-3 min" --no-pager | grep fs_trunk
```

**Expect:** `freeswitch_trunk.prom` timestamped within the last minute, and `CMD (/usr/local/sbin/fs_trunk_metrics.sh ...)` lines.

### Step 6 — Add the textfile flag to node_exporter

The container already mounts the host disk at `/host`, so only one flag is needed — no new volume.

```bash
cd DIR
cp docker-compose.yml docker-compose.yml.bak-$(date +%F)
sed -i "/--web.listen-address=/a\      - '--collector.textfile.directory=/host/var/lib/node_exporter/textfile'" docker-compose.yml
grep -A6 'command:' docker-compose.yml
docker compose config --quiet && echo COMPOSE-OK
```

**Expect:** the new line last under `command:`, then `COMPOSE-OK`:

```yaml
    command:
      - '--path.procfs=/host/proc'
      - '--path.sysfs=/host/sys'
      - '--path.rootfs=/host'
      - '--web.listen-address=${NODE_EXPORTER_LISTEN_ADDRESS:?Set this host private IP in .env}'
      - '--collector.textfile.directory=/host/var/lib/node_exporter/textfile'
```

If the new line is missing, the file has no `--web.listen-address` line — add the line by hand
with `nano` under `command:`, matching the indentation of the other entries.

### Step 7 — Apply (recreates only node_exporter; a few seconds' gap in node metrics, no call impact)

```bash
docker compose up -d SERVICE
docker ps --filter name=EXPORTER --format '{{.Names}} {{.Status}}'
docker inspect EXPORTER -f '{{json .Config.Cmd}}' | grep -o 'textfile[^"]*'
```

**Expect:** `Up X seconds` and `textfile.directory=/host/var/lib/node_exporter/textfile`.

> ⚠️ Editing the file is not enough — the container keeps its old flags until this command runs.

### Step 8 — Verify on the node

```bash
curl -s http://NODE_IP:9100/metrics | grep -E '^(trunk_container_running|freeswitch_gateway_up|freeswitch_trunk_scrape_ok|node_textfile_scrape_error)'
```

Use `NODE_IP`, not `localhost` — node_exporter only listens on the private IP.

**Expect:** `trunk_container_running ... 1`, `freeswitch_trunk_scrape_ok 1`, one
`freeswitch_gateway_up` line per trunk, and `node_textfile_scrape_error 0`.

### Step 9 — Verify on the central Prometheus

Open `http://181.214.10.244:9090` and run (replace the server name):

```promql
freeswitch_gateway_up{server_name="shams-uc01"}
trunk_container_running{server_name="shams-uc01"}
time() - node_textfile_mtime_seconds{file=~".*freeswitch_trunk.prom"}
```

**Expect:** results within ~30 seconds; file age under 60 s. The node then appears on the
dashboard under **SIP Trunks**.

Server names as Prometheus knows them: `spc-sj02`, `my03`, `shams-uc01`, `ds01`, `ds02`.

---

## Rollback

```bash
cd DIR
cp docker-compose.yml.bak-YYYY-MM-DD docker-compose.yml
docker compose up -d SERVICE
rm -f /etc/cron.d/fs_trunk_metrics /var/lib/node_exporter/textfile/freeswitch_trunk.prom
```

---

## Troubleshooting (things that actually happened)

| Symptom | Cause | Fix |
|---|---|---|
| `install: cannot stat 'fs_trunk_metrics.sh'` / `'freeswitch_trunk_metrics.sh'` | File was saved under a different name in nano (seen: `fs_trunk_metrics.sh` vs `freeswitch_trunk_metrics.sh`, and the typo `freeswitch_trunk_mertrics.sh`) | `ls`, then `mv` it to `freeswitch_trunk_metrics.sh` and rerun step 3 |
| `/usr/local/sbin/fs_trunk_metrics.sh: No such file` | Script created in the current folder, never installed | Run the `install` line from step 3 |
| textfile folder empty right after adding cron | Checked before cron's first run | Wait a full minute and check again |
| `curl` shows `node_textfile_scrape_error 0` but no `freeswitch_*` | Compose file edited but `docker compose up -d` not run (container still "Up 7 days") | Run step 7 |
| `bash: hostname; docker ps ...: No such file or directory` | Ran an `ssh host '...'` command after already logging in, with the quotes | Drop the quotes and run the commands directly |
| `freeswitch_trunk_scrape_ok 0` | Trunk container stopped or ESL 8023 not answering | `docker ps`, then run the `fs_cli` command from step 1 by hand |
| Node's metrics vanish from Prometheus | A malformed line in the `.prom` file makes node_exporter reject the whole file | Check `node_textfile_scrape_error` (1 = bad file); use the repo script, not older copies |
| Dashboard shows trunk DOWN but it is fine | Cron stopped → file older than 3 minutes | `journalctl -u cron`, `systemctl status cron` |

---

## Special cases

### Not compose-managed

If step 2 prints an empty `DIR`, recreate the container by hand, keeping the old one for rollback.
Copy the volumes and arguments from `docker inspect EXPORTER` exactly and add the one flag:

```bash
docker stop EXPORTER && docker rename EXPORTER EXPORTER-old
docker run -d --name EXPORTER --restart unless-stopped --net host --pid host \
  -v /:/host:ro,rslave \
  quay.io/prometheus/node-exporter:v1.12.1 \
  --path.procfs=/host/proc --path.sysfs=/host/sys --path.rootfs=/host \
  --web.listen-address=NODE_IP:9100 \
  --collector.textfile.directory=/host/var/lib/node_exporter/textfile
# when step 8 passes:   docker rm EXPORTER-old
# rollback:             docker rm -f EXPORTER && docker rename EXPORTER-old EXPORTER && docker start EXPORTER
```

### No /host mount

If node_exporter does not mount `/` at `/host`, add a dedicated volume instead and point the flag at it:

```yaml
    volumes:
      - /var/lib/node_exporter/textfile:/var/lib/node_exporter/textfile:ro
    command:
      - '--collector.textfile.directory=/var/lib/node_exporter/textfile'
```

### Trunk host the central Prometheus cannot reach

(Seen at Meydan: `gw` 172.21.2.17 is reachable only from my02/my03 — it turned out to be a standby,
so it was not needed.) If such a host ever carries a live trunk:

1. Install the script on that host (steps 3–4, no cron).
2. On a reachable neighbour **that has no trunk container of its own**, run every minute:
   `ssh -o BatchMode=yes root@HOST /usr/local/sbin/fs_trunk_metrics.sh --stdout > TMP && mv TMP /var/lib/node_exporter/textfile/freeswitch_trunk.prom`
3. Enable the textfile flag on that neighbour (steps 6–7).
4. Add a `metric_relabel_configs` rule on the `debian_servers` job so the series carry the real host's
   `server_name` instead of the neighbour's.

---

## Dashboard reference

Grafana → **OCube → Public & Private Infrastructure Availability**:

- **Overview** — *SIP Trunks* and *Trunk Containers* tiles (running / down counts)
- **SIP Trunks** section — per-trunk tiles (`cluster / gateway (number)`), per-node container tiles,
  details table (registration, proxy, trunk and container uptime), status timeline, calls per minute

A trunk shows **DOWN** when its gateway Status is not UP *or* its metrics file is older than 3 minutes.
Standby nodes never raise a container-down alarm unless they carried a trunk in the last 24 hours.

### Suggested alerts (not yet configured)

```promql
(freeswitch_gateway_up == 0) unless freeswitch_gateway_up{server_name="spc-sj02",gateway="du_ae_trunk"}   # for 2m
freeswitch_trunk_scrape_ok == 0                                                                        # on active trunk nodes
```

Exclude SPC's `du_ae_trunk` (065100200, FAIL_WAIT) until dev confirms whether it is still used.
