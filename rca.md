# RCA: Public-path Medan monitoring coverage gap

**Investigation date:** 6 October 2026  
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
