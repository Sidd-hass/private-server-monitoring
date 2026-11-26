# Wazuh (SIEM / HIDS) for the VoIP fleet

Wazuh 4.14.11 single node: **manager** (agents, rules, alerts), **indexer** (OpenSearch, stores
alerts), **dashboard** (web UI). Based on the official `wazuh-docker` `single-node` deployment
(tag v4.14.11). Our changes are listed at the top of `docker-compose.yml`.

```
agents (ucpin11/12/13, uc11/12, ucpmed01/02, later OCube)
   │ 1514/tcp events (TLS) · 1515/tcp enrollment (password)
   ▼
wazuh.manager ──► wazuh.indexer (not published) ◄── wazuh.dashboard 127.0.0.1:5443 (https)
```

| What | Where |
|---|---|
| SIP rules: Kamailio secban scans/bans, fail2ban bans, **carrier/own IP banned (level 14)** | `rules/local_rules.xml`, `decoders/local_decoder.xml`, `lists/sip-carriers` |
| Root SSH login, changes to authorized_keys / passwd / sshd_config / interfaces / iptables / fail2ban / Kazoo + Kamailio config / cron / `/usr/local/sbin` | `rules/local_rules.xml` + `shared/kazoo/agent.conf` (FIM) |
| New listening port (built-in rule 533) | `shared/kazoo/agent.conf` (`ss` every 6 h) |
| Vulnerabilities (CVEs), CIS config score (SCA), rootkit checks | Wazuh defaults |
| Never-block list for active response | `<white_list>` in `config/wazuh_cluster/wazuh_manager.conf` |

Active response (automatic blocking) is **off**: one week alert-only first.

Alerts of level 12+ go to Flock (`integrations/custom-flock.py`), to the agent's cluster channel
(ucpin* -> FLOCK_UCPIN, ...) from the same `flock_channels.env` as Alertmanager
(`FLOCK_CHANNELS_FILE` in `.env`). Add `FLOCK_WAZUH=<webhook>` to that file to send all Wazuh
alerts to one security channel instead.

`init/0z-local-permissions` runs inside the manager at start and fixes the ownership of the
mounted files (otherwise the agent group config is never sent to agents).

## Tested locally (2026-11-26, WSL Docker)

- Stack up, no manager errors; custom rules/decoders/list loaded; dashboard https://localhost:5443 = 200.
- wazuh-logtest with real UCPIN lines: SECBAN_WOULD -> 100101 (5), SECBAN -> 100102 (8),
  SECBAN_FW fonouc-sip -> 100106 (8), fonouc-sip-r -> 100107 (10), **SECBAN_FW ban of
  27.107.220.70 -> 100110 (14)**, Tata flagged -> 100111 (12), root SSH login -> 100120 (10).
- Test agent enrolled with the password (a wrong password is refused), joined group kazoo and
  received `shared/kazoo/agent.conf`.
- FIM: Kamailio config edit -> 100132 (10) with diff; authorized_keys edit -> 100130 (12).
- Tata ban line in the agent's secban log -> 100110 -> Flock message to the ucpin channel
  (fake webhook); a normal ban (level 8) is not sent.

## Local test (WSL Debian)

Docker must be able to pull from Docker Hub (`docker pull hello-world`). If it says
"incorrect username or password", run `docker logout` (or `docker login`).

```bash
cd /mnt/c/Users/OcAfee02/Desktop/monitoring-1/monitoring/wazuh
cp .env.example .env                                   # local defaults; WSL already runs a native
                                                       # wazuh-manager on 1514/1515/55000, so set
                                                       # AGENT_PORT=11514 ENROLL_PORT=11515 API_PORT=15500
openssl rand -hex 16 > config/authd.pass               # agent enrollment password
sysctl vm.max_map_count                                # must be >= 262144 (indexer)
docker compose -f generate-indexer-certs.yml run --rm generator   # once: TLS certs
docker compose up -d
docker compose logs -f wazuh.manager                   # wait for "wazuh-modulesd ... started"
# the "kazoo" agent group is created from shared/kazoo at start
```

Dashboard: https://localhost:5443 (self-signed), user `admin`, password = `INDEXER_PASSWORD`.

Test agent on the same machine:

```bash
docker run -d --name wazuh-test-agent --network wazuh_default \
  -e WAZUH_MANAGER=wazuh.manager -e WAZUH_REGISTRATION_PASSWORD="$(cat config/authd.pass)" \
  -e WAZUH_AGENT_NAME=test-agent -e WAZUH_AGENT_GROUP=kazoo wazuh/wazuh-agent:4.14.11
```

Rule test without an agent:
`docker exec -it wazuh-wazuh.manager-1 /var/ossec/bin/wazuh-logtest` and paste a log line.

## Deploy on 246 (181.214.10.246, private 10.20.0.246)

The manager runs on 246. The dashboard is only on 246's private IP and is viewed through the
existing Grafana domain: **https://ocubedashboards.voicemeetme.net/wazuh/** (NPM on 244 ->
https://10.20.0.246:5443 over 10.20.0.0/24). Agents connect to 181.214.10.246:1514/1515.

```
Browser -> ocubedashboards.voicemeetme.net/wazuh/ (NPM, 244) -> 10.20.0.246:5443 (dashboard, 246)
Agents  -> 181.214.10.246:1514 events / 1515 enrolment (DOCKER-USER: node IPs only)
```

1. Code and kernel setting (246, SSH port 5414):
   ```bash
   cd /opt && git clone https://github.com/Sidd-hass/private-server-monitoring.git
   cd /opt/private-server-monitoring/wazuh
   sysctl -w vm.max_map_count=262144 && echo 'vm.max_map_count=262144' > /etc/sysctl.d/99-wazuh.conf
   ```
2. Secrets: `cp .env.example .env && chmod 600 .env`, set `DASHBOARD_BIND=10.20.0.246`, the three
   passwords (see "Passwords" below, before the first start) and `FLOCK_CHANNELS_FILE` (copy
   `alertmanager/secrets/flock_channels.env` from 244 to 246, e.g. `/root/flock_channels.env`, chmod 600).
   `openssl rand -hex 24 > config/authd.pass && chmod 600 config/authd.pass`
3. Certificates and start:
   ```bash
   docker compose -f generate-indexer-certs.yml run --rm generator
   docker compose up -d && docker compose ps
   ```
4. Firewall (246 had no INPUT rules and DOCKER-USER returns everything). DOCKER-USER sees the
   container ports after DNAT (1514, 1515, 5601). Insert above the final RETURN:
   ```bash
   iptables -I DOCKER-USER 1 -p tcp -m multiport --dports 1514,1515,5601 -j DROP
   iptables -I DOCKER-USER 1 -p tcp --dport 5601 -s 10.20.0.244 -j RETURN
   for ip in 151.243.12.57 151.243.12.59 151.243.12.60 181.214.10.237 181.214.10.238 143.14.172.231 143.14.172.232; do
     iptables -I DOCKER-USER 1 -p tcp -m multiport --dports 1514,1515 -s $ip -j RETURN
   done
   netfilter-persistent save
   ```
5. 244: paste the updated `nginx-proxy-manager/ocubedashboards-advanced.conf` (it now has the
   `/wazuh/` location) into NPM -> ocubedashboards.voicemeetme.net -> Advanced -> Save.
   Check from 244: `curl -sk -o /dev/null -w "%{http_code}
" https://10.20.0.246:5443/wazuh/app/login` = 200.

## Passwords (before any server)

The defaults in `.env.example` are public. Change all three:

1. `INDEXER_PASSWORD` (user `admin`) and `DASHBOARD_PASSWORD` (user `kibanaserver`): put the new
   bcrypt hashes in `config/wazuh_indexer/internal_users.yml`. Hash with
   `docker run --rm -ti wazuh/wazuh-indexer:4.14.11 bash /usr/share/wazuh-indexer/plugins/opensearch-security/tools/hash.sh`.
2. `API_PASSWORD` (user `wazuh-wui`): also in `config/wazuh_dashboard/wazuh.yml`.
3. Same values in `.env`, then `docker compose down && docker compose up -d`.

Official procedure: https://documentation.wazuh.com/current/deployment-options/docker/wazuh-container.html#change-the-password-of-wazuh-users

## Agent on a node (Debian), after the server is up

```bash
curl -s https://packages.wazuh.com/key/GPG-KEY-WAZUH | gpg --dearmor -o /usr/share/keyrings/wazuh.gpg
echo "deb [signed-by=/usr/share/keyrings/wazuh.gpg] https://packages.wazuh.com/4.x/apt/ stable main" > /etc/apt/sources.list.d/wazuh.list
apt update
WAZUH_MANAGER=<manager address> WAZUH_REGISTRATION_PASSWORD='<authd.pass>' \
  WAZUH_AGENT_GROUP=kazoo WAZUH_AGENT_NAME=$(hostname -s) apt install -y wazuh-agent=4.14.11-1
apt-mark hold wazuh-agent          # upgrade agents together with the manager
systemctl enable --now wazuh-agent
```

About 50-100 MB RAM per agent. Server firewall: allow 1514/1515 only from the node IPs.
