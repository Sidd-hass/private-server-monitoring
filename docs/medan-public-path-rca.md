# RCA: Public-path Medan monitoring coverage gap

**Investigation date:** 6 October 2026  
**Follow-up configuration update:** 7 October 2026  
**Scope:** Monitoring from the central Prometheus stack through Medan public SSH endpoints, plus discovery from `my01` toward Medan and the Shams, SPC, and Dubai South networks.  
**Status:** Public-path discovery and connectivity evidence collected. The inter-site network cause is narrowed to routing/firewall/return-path reachability, but the exact Fortinet rule or route has not been inspected, so the network-layer root cause is not yet isolated to a specific configuration item.

## Executive summary

The central monitoring stack can reach the public SSH endpoint for Medan `my01` (`94.206.56.70:11446`), but the dashboard showed the `my02` endpoint (`94.206.56.71:11447`) down. These checks confirm only whether the SSH TCP ports respond; they do not collect operating-system metrics.

From `my01` (`192.168.99.46`), local Medan discovery succeeded: Nmap reported 92 responding addresses across `192.168.99.0/24` and `10.15.228.0/24`. Reverse DNS named five hosts. In contrast, tests to representative Shams, SPC, and Dubai South hosts received no ping replies, no traceroute hop replies, and TCP port 22 timeouts. The route lookup sends those packets to the Fortinet gateway at `192.168.99.1`, but the tests did not establish onward connectivity.

**Result:** Public SSH access gives a way into `my01`; it does not automatically make the other site networks reachable. A working route and return path, firewall permission, and a monitoring/metrics relay on the Medan side are required. Full metrics over the public path were not demonstrated: the Grafana screenshot showed no public SSH tunnel metrics for either host.

## User-visible symptom

The **Medan Public Path Resilience** dashboard screenshot showed:

| Check | Observed result | What it establishes |
| --- | --- | --- |
| `my01` public SSH TCP endpoint | UP | TCP connection to `94.206.56.70:11446` succeeded from the Blackbox probe location at that time. |
| `my02` public SSH TCP endpoint | DOWN | TCP connection to `94.206.56.71:11447` did not succeed from the probe location at that time. |
| `my01` / `my02` SSH tunnel metrics | NO METRICS | No successful Prometheus scrape samples were present for the configured local tunnel targets. |
| Public path interface charts | No data | These charts depend on Node Exporter samples through the SSH tunnels. |
| Public IP ICMP ping | Added as a follow-up Prometheus job on 7 October; live results not yet confirmed. | Measures ICMP response/RTT independently of the VPN if the public host/provider allows ping. |

The public-port panel is an availability check for SSH, not a login test or a metrics test. The tunnel scrape definitions point to `host.docker.internal:19101` and `:19102`; persistent SSH forwards and their local listeners must be installed and reachable before those jobs can return metrics. The investigation did not verify running `autossh` services or active tunnel listeners.

## Investigation performed and evidence

### 1. Route and neighbor inspection on my01

Reported `ip route`:

```text
default via 192.168.99.1 dev ens18 onlink
10.15.228.0/24 via 192.168.99.42 dev ens18
192.168.99.0/24 dev ens18 src 192.168.99.46
```

The host also had local Docker bridge routes (`172.17.0.0/16` and
`172.18.0.0/16`); these are container networks, not evidence of reachability to
the branch sites.

`ip neigh` showed recently learned Layer 2 peers on `192.168.99.0/24`, including
the gateway `.1` and Medan servers `.40`, `.42`, and `.47`. A neighbor cache is
not a complete device inventory: it contains only peers recently resolved on
the local Layer 2 network.

Route lookups for representative remote hosts returned:

```text
172.16.125.40 via 192.168.99.1 dev ens18 src 192.168.99.46
172.16.200.102 via 192.168.99.1 dev ens18 src 192.168.99.46
192.168.160.6 via 192.168.99.1 dev ens18 src 192.168.99.46
```

This confirms Linux selected the default gateway as the next hop. It does not
confirm that the Fortinet gateway has a route onward, permits the traffic, or
has a working return path.

### 2. Local network discovery from my01

Nmap was run against `192.168.99.0/24` and `10.15.228.0/24` (512 addresses in
total). It reported **92 hosts up**: 91 results in `192.168.99.0/24` including
my01 itself, and one result in `10.15.228.0/24` (`10.15.228.226`). The first
run used `-n`, which suppresses name lookups. A repeat with `-R` returned these
five DNS names:

| IP | Reverse-DNS name | Inventory correlation |
| --- | --- | --- |
| `192.168.99.40` | `my04.ira-meydan-du.ucprem.voicemeetme.com` | my04 |
| `192.168.99.42` | `my03.ira-meydan-du.ucprem.voicemeetme.com` | my03; next hop for `10.15.228.0/24` |
| `192.168.99.46` | `my01.ira-meydan-du.ucprem.voicemeetme.com` | my01; discovery source |
| `192.168.99.47` | `my02.ira-meydan-du.ucprem.voicemeetme.com` | my02 |
| `192.168.99.175` | `meydan-repo.ocubeservices.com` | Inventory labels this address as a Medan CDR custom report server; confirm its current role. |

Nmap reported MAC vendor information for 90 local-subnet responders. Vendor
identification is based on the MAC address and is not a confirmed device name
or role. The scan returned Aruba, Proxmox, NVIDIA, Huawei, Synology, Vantage,
Dahua, Dell, Broadcom, Matrix Comsec, Salto, Yeastar, Fortinet, HP/HPE, and
unknown vendor entries. Only `10.15.228.226` was found in the routed
`10.15.228.0/24` subnet by this scan.

This discovery did **not** scan the Shams (`172.16.125.0/24`), SPC
(`172.16.200.0/24`), or Dubai South (`192.168.160.0/24`) subnets.

### 3. Remote site reachability tests from my01

Representative host tests were run for:

| Site | Test target | Ping | UDP traceroute | TCP traceroute to port 22 | TCP connection to port 22 (`nc`) |
| --- | --- | --- | --- | --- | --- |
| Shams | `172.16.125.40` | 3 sent, 0 received | All 8 hops `*` | All 8 hops `*` | Timed out |
| SPC | `172.16.200.102` | 3 sent, 0 received | All 8 hops `*` | All 8 hops `*` | Timed out |
| Dubai South | `192.168.160.6` | 3 sent, 0 received | All 8 hops `*` | All 8 hops `*` | Timed out |

`nc` also printed `inverse host lookup failed: Unknown host`; this means no
reverse-DNS name was found and is not the cause of the connection timeout.
Traceroute stars mean no hop responses were received; routers can suppress
traceroute messages. The TCP connection timeouts are the stronger evidence:
my01 did not complete a TCP handshake to port 22 on these sample targets.
These tests do not prove whether the specific cause is a missing route, a
Fortinet policy, a downstream firewall, or a missing return route.

## Root cause assessment

### Confirmed cause of the monitoring gap

There is **no demonstrated network path from my01 (`192.168.99.46`) to the
tested Shams, SPC, and Dubai South hosts**. Traffic is sent to `192.168.99.1`,
but the remote SSH connection attempts time out. Therefore, my01 cannot
currently be used as a probe or metrics relay for those tested targets.

### Probable contributing causes; require network-team verification

- The Fortinet gateway may lack routes or forwarding policy from the Medan
  subnet to the three remote site networks.
- A policy may drop ICMP and TCP/22, or the destination-side firewall may do
  so.
- The destination sites may lack a return route to `192.168.99.46`, creating
  asymmetric or incomplete connectivity.
- The working VPN routes used by the central monitoring host (if currently
  active) are not automatically inherited by my01. The supplied `ip route`
  output shows no explicit route or VPN interface for these site prefixes.

The exact network device/rule responsible is **not yet confirmed** because no
Fortinet routing table, firewall policy, VPN status, packet capture, or
destination-side evidence was supplied.

## Current monitoring capability and limits

| Capability | Current evidence/status | Coverage boundary |
| --- | --- | --- |
| Public SSH-port availability | my01 endpoint was UP; my02 endpoint was DOWN in the screenshot. | Tests only TCP port reachability from the Blackbox probe host. |
| Public IP ping | `blackbox_public_ping` job is now configured in the repository for both public IPs. | Not yet confirmed in the running Prometheus instance; ICMP may be filtered even when SSH works. |
| Discovered-device ping via my01 | A Blackbox relay tunnel and file-SD snapshot of 92 Nmap responders are configured in the repository. | Not yet confirmed live; the target list is a 6 October snapshot and new devices require a refreshed inventory. |
| Full metrics for my01/my02 over public SSH | Tunnel metrics showed NO METRICS. Tunnel listeners/services were not verified. | Needs working persistent SSH forwards and Node Exporter reachable on the corresponding Medan host. |
| Host directory usage | A textfile collector and hourly low-priority timer are provided for top-level root-filesystem directories. | Must be installed on each server and the Node Exporter textfile collector redeployed; not yet live-verified. |
| Local Medan device discovery | Nmap reported 92 IPs up across `.99/24` and `.228/24`; five reverse-DNS names returned. | A point-in-time scan; responders may block probes, and names require DNS/inventory records. |
| Monitoring of other `.99/24` hosts | The Prometheus file-SD registry includes my01, my02, my03, and my04 Node Exporter targets. | Configuration presence is not proof the central Prometheus scrape is currently healthy. |
| Shams, SPC, Dubai South metrics/ICMP from my01 | Representative ping, trace, and SSH tests failed to establish reachability. | Cannot discover or relay those sites from my01 until routing/firewall/return path works. |
| Existing central VPN-based targets | Prometheus configuration lists Shams, SPC, and Dubai South private targets. | Configured targets work only when the central collector has active routes and the target exporters/probes permit access; current scrape health was not supplied. |
| Network device and endpoint names | MAC vendors and five PTR records available. | Vendor/OUI is not a hostname; most devices need DHCP, DNS, SNMP, or controller inventory to identify. |

Node Exporter can provide host CPU, memory, filesystem, interface byte/packet
counters, errors/drops, and TCP/socket metrics when installed and scrapeable.
Blackbox Exporter can provide ping success/latency and TCP/HTTP/DNS probe
results from the network location where it runs. Neither can collect a remote
host's OS metrics merely because its IP appears in traceroute or responds to
Nmap discovery.

## Corrective actions

1. **Network team — restore/confirm inter-site path.** On Fortinet, verify
   routes and policy from source `192.168.99.46` to `172.16.125.0/24`,
   `172.16.200.0/24`, and `192.168.160.0/24`, including return routes to
   `192.168.99.46`. Confirm the intended VPN/interface and whether ICMP,
   TCP/22, and the approved exporter/probe ports should be allowed.
2. **Validate after the network change.** From my01, repeat
   `ip route get <target>`, `nc -vz -w 3 <target> 22`, and the approved ping or
   probe. A successful route lookup alone is insufficient; TCP or probe
   connectivity must succeed, with a return path.
3. **Enable the public fallback for Medan.** Confirm my02's public endpoint and
   NAT mapping, then configure restricted, persistent SSH forwards for
   my01/my02 to their Node Exporter endpoints. Keep tunnel listener ports
   reachable only from the Prometheus Docker network. Confirm the tunnel
   targets become healthy before relying on the public-path dashboard.
4. **Deploy the Medan discovery relay.** Deploy Blackbox Exporter on my01,
   enable its public SSH tunnel to central Prometheus, and scrape the 92-host
   Nmap snapshot. Refresh the snapshot after approved discovery scans. For
   full host metrics, ensure each managed server runs Node Exporter (or an
   approved equivalent) and configure a reachable scrape/relay path.
5. **Build a named inventory.** Correlate IPs with Fortinet DHCP leases,
   reverse-DNS records, Aruba/device controllers, Proxmox, Synology, and
   application/device management systems. Keep discovered IP, confirmed
   hostname, owner/site, device role, and monitored ports as separate fields.
6. **Acceptance criteria.** Demonstrate (a) my01 and my02 public SSH checks,
   (b) public Node Exporter tunnels healthy, (c) the my01 Blackbox relay
   pinging the discovered Medan devices, (d) Node Exporter host and directory
   metrics for each intended Linux server, and (e) SNMP/vendor metrics for
   network devices selected for interface monitoring. Record explicit
   exceptions for devices that block ICMP or do not expose metrics.

## Evidence limitations

- Findings are based on user-provided command output and a Grafana screenshot;
  no live access to my01, Fortinet, the remote sites, or central Prometheus was
  used for this RCA.
- The pasted Nmap runs did not include a saved XML output or port/service
  inventory. The 92-host result is discovery response, not proof that all
  devices are stable, uniquely named, or approved for metrics collection.
  On the directly attached `.99/24`, Nmap can report hosts from ARP discovery;
  this does not guarantee they will answer ICMP from the Blackbox ping panel.
- No credentials, passwords, or private keys are included in this report.
