# Centralized Observability Stack (Debian & Multi-Server Ready)

A production-grade **Prometheus, Grafana, Alertmanager, and Node Exporter** monitoring stack designed for Linux/Debian environments, packaged with Docker Compose, automated provisioning, dynamic service discovery, and custom health dashboards.

---

## 🏗️ Architecture

```
                       +----------------------------------+
                       |      Web Browser / Operator      |
                       |       http://<IP>:3000           |
                       +-----------------+----------------+
                                         |
                                         v
+-------------------------------------------------------------------------------+
| Central Monitoring Server (Debian)                                            |
|                                                                               |
|   +-------------------+       Queries PromQL         +--------------------+   |
|   |  Grafana :3000    | ---------------------------> | Prometheus :9090   |   |
|   |  (Pre-provisioned |                              |                    |   |
|   |   Dashboards)     |                              | - Scrape (15s)     |   |
|   +-------------------+                              | - Alert Rules      |   |
|                                                      | - File Discovery   |   |
|                                                      +---------+----------+   |
|                                                                |              |
|                                                     Fires      | Dispatches   |
|                                                     Alerts     | Alerts       |
|                                                                v              |
|   +--------------------+                             +---------+----------+   |
|   | Node Exporter      | <------- Scrapes (Local) -- | Alertmanager :9093 |   |
|   | :9100 (Host Stats) |                             | (Routing/Webhook)  |   |
|   +--------------------+                             +--------------------+   |
+-----------------------------------------------------------------+-------------+
                                                                  |
                                              Scrapes (Remote)    |
                              +-----------------------------------+
                              |
     +------------------------+------------------------+
     |                                                 |
     v                                                 v
+-----------------------------+   +-----------------------------+
| Debian Client Server 01     |   | Debian Client Server 02     |
| Node Exporter (:9100)       |   | Node Exporter (:9100)       |
| (Native systemd / apt)      |   | (Native systemd / apt)      |
+-----------------------------+   +-----------------------------+
```

---

## 📁 Repository Structure

```
├── docker-compose.yml              # Central services orchestration
├── .env.example                    # Environment configuration template
├── prometheus/
│   ├── prometheus.yml              # Global scrape, rule paths, and dynamic targets
│   ├── alert.rules.yml             # CPU, RAM, Disk, and Target Down alert rules
│   └── targets/
│       └── debian_servers.json     # Dynamic client server discovery registry
├── alertmanager/
│   └── alertmanager.yml            # Alert routing, deduplication, and receivers
├── grafana/
│   └── provisioning/
│       ├── datasources/
│       │   └── datasource.yml      # Automatic Prometheus datasource connection
│       └── dashboards/
│           ├── dashboards.yml      # Automated dashboard provider
│           └── json/
│               └── debian_system_overview.json # Full fleet & host dashboard
└── scripts/
    └── setup-debian-node.sh        # Turnkey agent installer for remote Debian nodes
```

---

## 🚀 Quick Start (Central Monitoring Server)

### 1. Requirements
* Docker Engine 20.10+ & Docker Compose v2.0+
* Ports `3000` (Grafana), `9090` (Prometheus), `9093` (Alertmanager), and `9100` (Node Exporter) open on the central host.

### 2. Configure Environment
```bash
cp .env.example .env
# Edit .env to set your custom GF_ADMIN_PASSWORD if desired
```

### 3. Launch Stack
```bash
docker compose up -d
```

### 4. Check Status
```bash
docker compose ps
```

### 5. Access Services
| Service | URL | Credentials |
| :--- | :--- | :--- |
| **Grafana** | `http://<SERVER_IP>:3000` | User: `admin` / Password: `admin` (or `.env` value) |
| **Prometheus** | `http://<SERVER_IP>:9090` | Public / Open |
| **Alertmanager** | `http://<SERVER_IP>:9093` | Public / Open |
| **Node Exporter** | `http://<SERVER_IP>:9100/metrics` | Raw metrics stream |

---

## 🐧 Onboarding Remote Debian Client Servers

To add any external Debian 11/12 server into the monitoring stack:

### Step 1: Run the Agent Installer on the Client Node
Transfer and run the installer on the remote Debian server:
```bash
# Run with the central Prometheus IP as the argument to automatically configure firewall rules:
sudo bash setup-debian-node.sh 192.168.1.100
```
This will:
- Install `prometheus-node-exporter` via `apt`.
- Enable and start `prometheus-node-exporter.service` via `systemctl`.
- Permit port 9100 through `ufw` from your Central Prometheus IP.

### Step 2: Register Node on Central Server
On your central monitoring server, edit `prometheus/targets/debian_servers.json`:
```json
[
  {
    "targets": ["192.168.1.101:9100"],
    "labels": {
      "environment": "production",
      "server_name": "web-frontend-01",
      "os": "debian",
      "role": "web-app"
    }
  },
  {
    "targets": ["192.168.1.102:9100"],
    "labels": {
      "environment": "production",
      "server_name": "db-primary-01",
      "os": "debian",
      "role": "database"
    }
  }
]
```
> **No restart required!** Prometheus scans this file every 60 seconds and auto-discovers newly registered nodes.

---

## 📊 Pre-Configured Grafana Dashboard

Once you log into Grafana at `http://<SERVER_IP>:3000`, open **Dashboards -> Observability -> Debian Server Fleet Overview**:
- **Multi-Server Filter:** Dropdown selector to switch between nodes.
- **Server Status & Uptime:** Live `up` indicator and uptime counters.
- **Resource Gauges:** Real-time gauges for CPU utilization (%), RAM saturation (%), and Root `/` Disk usage (%).
- **System Load:** 1-minute, 5-minute, and 15-minute load trends.
- **Memory Breakdown:** Stacked breakdown of Used, Free, Buffers, and Cached memory.
- **Network Throughput & Drops:** Bandwidth in bytes/sec and packet error/drop counters.

---

## 🚨 Alert Rules Overview

Configured in `prometheus/alert.rules.yml`:
1. **`CpuSpike`**: CPU usage > 85% for 5 minutes (`critical`).
2. **`RamCritical`**: Memory usage > 90% for 3 minutes (`warning`).
3. **`DiskCritical`**: Root disk usage > 85% for 5 minutes (`warning`).
4. **`ServerDown`**: Any monitored target unreachable for 1 minute (`page`).
