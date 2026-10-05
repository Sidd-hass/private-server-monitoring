# Centralized Grafana & Prometheus Observability Stack

This repository contains the setup, architecture, and step-by-step roadmap for building a production-ready **Prometheus + Grafana** monitoring stack using **Docker Compose**, with a design geared for future deployment via **Ansible**.

---

## 1. Project Overview & Scaffolding

### Aims
* **Centralization:** A single pane of glass to monitor system health and application performance across all client servers.
* **Proactive Monitoring & Alerting:** Immediate notification of resource bottlenecks, traffic anomalies, or system downtime.
* **Root-Cause Diagnostics:** Network tracking and time-series historical data to perform deep post-mortems on downtime events.

### System Dependencies
* **Central Infrastructure:**
  * **Prometheus:** Metrics collection engine and time-series database.
  * **Grafana:** Visualization platform and dashboard interface.
  * **Alertmanager:** Deduplicates, groups, and routes alert notifications.
* **Server Agents:**
  * **Node Exporter:** Node-level CPU, RAM, Disk, and Network metrics.
  * **Blackbox Exporter (Phase 2):** Endpoint pings, ICMP, TCP, and HTTP health probes.
  * **Application Exporters (Phase 3):** Exposes application-level metrics (e.g., active user sessions, request latency).

### Deliverables
1. **Docker Compose Stack:** Local deployment environment for quick setup and iteration.
2. **Prometheus Configuration:** Scrape configs with dynamic client labeling.
3. **Grafana Dashboards:** Custom panel configurations for System Resources, Web Request Analysis, User Analytics, and Network Diagnostics.
4. **Alerting Rules:** Pre-configured alerting thresholds with channel routing logic.
5. **Ansible Automation (Phase 4):** Playbooks to auto-deploy agents and configurations across remote servers.

---

## 2. Project Roadmap & Milestones

* **Milestone 1 (Current Phase):** Server Statistics & Base Alerting (CPU, Memory, Disk, System Health).
* **Milestone 2:** Network Diagnostics & Post-Mortem Probing (Ping, Packet Drops, Latency, HTTP Health Checks).
* **Milestone 3:** Application Performance & Traffic Analysis (Request rates, Active user tracking, Error rates).
* **Milestone 4:** Ansible Automation & Multi-Client Scaling.

---

## 3. Directory Structure

```text
monitoring-stack/
├── docker-compose.yml
├── prometheus/
│   ├── prometheus.yml
│   └── alert.rules.yml
├── alertmanager/
│   └── alertmanager.yml
└── grafana/
    └── provisioning/
        ├── datasources/
        │   └── datasource.yml
        └── dashboards/
```

---

## 4. Milestone 1 Deployment Files

### `docker-compose.yml`
```yaml
version: '3.8'

networks:
  monitoring:
    driver: bridge

volumes:
  prometheus_data:
  grafana_data:
  alertmanager_data:

services:
  prometheus:
    image: prom/prometheus:v2.51.0
    container_name: prometheus
    restart: unless-stopped
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml
      - ./prometheus/alert.rules.yml:/etc/prometheus/alert.rules.yml
      - prometheus_data:/prometheus
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.path=/prometheus'
      - '--storage.tsdb.retention.time=15d'
      - '--web.enable-lifecycle'
    networks:
      - monitoring

  alertmanager:
    image: prom/alertmanager:v0.27.0
    container_name: alertmanager
    restart: unless-stopped
    ports:
      - "9093:9093"
    volumes:
      - ./alertmanager/alertmanager.yml:/etc/alertmanager/alertmanager.yml
      - alertmanager_data:/alertmanager
    command:
      - '--config.file=/etc/alertmanager/alertmanager.yml'
      - '--storage.path=/alertmanager'
    networks:
      - monitoring

  grafana:
    image: grafana/grafana:10.4.0
    container_name: grafana
    restart: unless-stopped
    ports:
      - "3000:3000"
    environment:
      - GF_SECURITY_ADMIN_USER=admin
      - GF_SECURITY_ADMIN_PASSWORD=admin
      - GF_USERS_ALLOW_SIGN_UP=false
    volumes:
      - ./grafana/provisioning:/etc/grafana/provisioning
      - grafana_data:/var/lib/grafana
    networks:
      - monitoring

  node-exporter:
    image: prom/node-exporter:v1.7.0
    container_name: node-exporter
    restart: unless-stopped
    ports:
      - "9100:9100"
    volumes:
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /:/rootfs:ro
    command:
      - '--path.procfs=/host/proc'
      - '--path.rootfs=/rootfs'
      - '--path.sysfs=/host/sys'
      - '--collector.filesystem.mount-points-exclude=^/(sys|proc|dev|host|etc)($$|/)'
    networks:
      - monitoring
```

### `prometheus/prometheus.yml`
```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  - "alert.rules.yml"

alerting:
  alertmanagers:
    - static_configs:
        - targets: ['alertmanager:9093']

scrape_configs:
  - job_name: 'prometheus'
    static_configs:
      - targets: ['localhost:9090']

  - job_name: 'server_stats'
    static_configs:
      - targets: ['node-exporter:9100']
        labels:
          environment: 'production'
          server_name: 'central-server'
```

### `prometheus/alert.rules.yml`
```yaml
groups:
  - name: system_resource_alerts
    rules:
      - alert: CpuSpike
        expr: 100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100) > 85
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "CPU Spike on {{ $labels.instance }}"
          description: "CPU usage exceeded 85% for 5 minutes."

      - alert: RamCritical
        expr: (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100 > 90
        for: 3m
        labels:
          severity: warning
        annotations:
          summary: "RAM Critical on {{ $labels.instance }}"
          description: "Memory usage exceeded 90% for 3 minutes."

      - alert: ServerDown
        expr: up == 0
        for: 1m
        labels:
          severity: page
        annotations:
          summary: "Server {{ $labels.instance }} offline"
          description: "Exporter target is unreachable."
```

### `alertmanager/alertmanager.yml`
```yaml
global:
  resolve_timeout: 5m

route:
  group_by: ['alertname', 'instance']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  receiver: 'default-receiver'

receivers:
  - name: 'default-receiver'
```

### `grafana/provisioning/datasources/datasource.yml`
```yaml
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    editable: true
```

---

## 5. PromQL Queries Cheat Sheet

### System Metrics (Node Exporter)
* **CPU Usage (%):**
  `100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)`
* **RAM Memory Usage (%):**
  `(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100`
* **Disk Usage (%):**
  `100 - ((node_filesystem_avail_bytes{mountpoint="/"} * 100) / node_filesystem_size_bytes{mountpoint="/"})`

### Application Request Metrics (Trunk Analysis)
* **Average Request Rate (req/sec):**
  `avg(rate(http_requests_total[5m]))`
* **Peak Request Rate (req/sec):**
  `max(rate(http_requests_total[5m]))`
* **Minimum Request Rate (req/sec):**
  `min(rate(http_requests_total[5m]))`
* **Active Online Users:**
  `sum(app_active_user_sessions)`

### Network Post-Mortem & Diagnostics
* **Network Throughput (Inbound/Outbound):**
  `rate(node_network_receive_bytes_total[5m])`
  `rate(node_network_transmit_bytes_total[5m])`
* **Packet Drop / Error Rates:**
  `rate(node_network_receive_drop_total[5m]) + rate(node_network_transmit_drop_total[5m])`

---

## 6. Quick Start Guide

1. Create the project folders:
   ```bash
   mkdir -p monitoring-stack/{prometheus,alertmanager,grafana/provisioning/{datasources,dashboards}}
   cd monitoring-stack
   ```
2. Populate the files as configured above.
3. Start the environment:
   ```bash
   docker compose up -d
   ```
4. Access services:
   * **Prometheus:** `http://localhost:9090`
   * **Alertmanager:** `http://localhost:9093`
   * **Grafana:** `http://localhost:3000` (User: `admin`, Pass: `admin`)