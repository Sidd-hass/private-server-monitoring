# Public-side Medan monitoring

The regular Medan node scrapes use the VPN-routed private addresses. This adds
an independent public reachability check for the SSH NAT endpoints and a
separate, optional SSH-tunnel path for full Node Exporter metrics when the VPN
route is unavailable.

## Inventory mapping used here

| Host | Private Node Exporter | Public SSH endpoint | Prometheus tunnel target |
| --- | --- | --- | --- |
| my01 | `192.168.99.46:9100` | `94.206.56.70:11446` | `host.docker.internal:19101` |
| my02 | `192.168.99.47:9100` | `94.206.56.71:11447` | `host.docker.internal:19102` |

The user's message included `192.168.99.746`, which is not a valid IPv4
address. This configuration uses `.46` for my01, matching the attached
inventory. Confirm this mapping before deployment if `.746` was intended to
mean a different host. The public IPs above are also taken from that inventory.

## What is collected

`blackbox_public_ssh` probes each public IP and SSH port from the central
Prometheus/Blackbox host. It reports reachability and probe duration. It does
not log in or collect host metrics. `blackbox_public_ping` sends ICMP probes to
the public IPs and records success and RTT. Some providers/firewalls block
ICMP, so a failed ping alone does not prove SSH is down; compare it with the
independent SSH TCP probe.

`medan_public_ssh_tunnel` scrapes Node Exporter through local TCP forwards
created by persistent SSH sessions from the central monitoring host to each
public SSH endpoint. Node Exporter must already be running on the corresponding
private interface and port on each server. It provides CPU, memory, filesystem,
uptime/load, interface throughput, errors/drops, and TCP/socket metrics. The
existing VPN-based scrape continues independently; dashboard queries can use
`server_name` to compare both paths.

The Node Exporter textfile collector adds `node_directory_size_bytes` for
top-level directories on the root filesystem. A low-priority systemd timer
refreshes the values hourly. It skips pseudo-filesystems and does not cross into
other mounted filesystems; per-mount usage remains available from normal Node
Exporter filesystem metrics.

`blackbox_medan_devices_public` scrapes a Blackbox Exporter running on my01
through a third SSH tunnel (`19115`). The exporter runs inside the Medan
network and pings the 92 addresses returned by the 6 October Nmap scan. Its
seed inventory is `prometheus/targets/medan_devices/nmap-2026-10-06.json`.
This is a dated snapshot, not automatic network discovery; replace the file
with a reviewed new scan inventory to include newly found devices.

Ping gives device reachability and latency, not per-device traffic counters.
Linux server counters require Node Exporter on each server. Switches, cameras,
NAS devices, and appliances generally need SNMP (preferably SNMPv3) or a
vendor-supported API and a matching exporter before Prometheus can collect
their interface utilization, errors, CPU, or memory.

## Deployment prerequisites

1. On each Medan server, install the directory collector: copy
   `medan/collect-directory-usage.sh` to
   `/usr/local/sbin/collect-directory-usage.sh` with mode `0755`, then copy
   `medan/directory-usage.service` and `medan/directory-usage.timer` to
   `/etc/systemd/system/`. Run `systemctl daemon-reload`, enable
   `directory-usage.timer`, and start `directory-usage.service` once to create
   the first sample. Deploy the updated Node Exporter Compose file so it mounts
   `/var/lib/node_exporter/textfile_collector` read-only and enables the
   textfile collector.
2. On my01, deploy Node Exporter bound to `192.168.99.46:9100` and Blackbox
   Exporter bound to `192.168.99.46:9115`. The provided compose files are
   `medan/node-exporter.compose.yml` and
   `medan/blackbox-exporter.compose.yml`; use
   `medan/node-exporter-my01.env.example` and
   `medan/blackbox-my01.env.example` as address templates. The Blackbox
   container needs the existing `NET_RAW` capability to send ICMP probes.
3. Confirm Node Exporter is bound privately on my02 at
   `192.168.99.47:9100`. The screenshot showed my02's public SSH endpoint
   down, so its public tunnel cannot work until `94.206.56.71:11447` is fixed.
4. Install `autossh` on the central Docker host. Create a dedicated SSH key,
   authorize it for the intended remote account on both hosts, and pin the
   verified host keys in `/etc/ssh/ssh_known_hosts`. Keep the private key out
   of this repository; do not use passwords from the server inventory.
5. Determine the address that `host.docker.internal` resolves to inside the
   Prometheus container. Set that exact address as `TUNNEL_BIND` in
   `medan/public-tunnels/tunnel.env.example`, then install it as
   `/etc/default/medan-public-tunnels`. The sample `172.17.0.1` is Docker's
   common host-gateway address and may differ on your host.
6. Install the three service files from `medan/public-tunnels/` into
   `/etc/systemd/system/`: my01 Node Exporter (`19101`), my02 Node Exporter
   (`19102`), and my01 Blackbox Exporter (`19115`). Install firewall rules so
   these listener ports accept traffic only from the Prometheus Docker
   network, never from the public interface.

On the central host, after installing the key, environment file, and unit
files, run `systemctl daemon-reload` and enable
`medan-my01-node-exporter.service`, `medan-my02-node-exporter.service`, and
`medan-my01-blackbox.service`. On my01 and my02, enable
`directory-usage.timer`. Use the Node Exporter Compose environment file with
the matching private address on each server; start the Blackbox Compose file
only on my01 with `BLACKBOX_LISTEN_ADDRESS=192.168.99.46:9115`.

After installing the key, verified host keys, and firewall rules, enable the
services with systemd. Prometheus already has scrape jobs for the local
forwards. Reload Prometheus after deploying the repository configuration and
copy the new dashboard/inventory files to their provisioned paths. The
`host.docker.internal` targets work only if the forward listener is bound to
the matching Docker host-gateway address.

The directory timer and the Node Exporter Compose update must be installed on
both my01 and my02 for the directory panel to show both servers. Only the
my01 SSH endpoint was reachable in the supplied dashboard screenshot.

## Device discovery and metrics boundary

The dated file-SD list records IP, available PTR hostname, scan segment, and
MAC vendor for each Nmap responder. It does not assign trustworthy names or
roles to devices without DNS/asset records, and MAC vendor does not establish a
device's function. The `Discovered Medan Devices` dashboard table shows
current ping status for the snapshot after the my01 Blackbox relay is active.
The original Nmap scan could discover local devices using ARP, so a device can
appear in the scan but fail ICMP ping if it filters echo requests.

To keep the inventory current, run an approved Nmap discovery from my01 for
the directly connected `192.168.99.0/24` and routed `10.15.228.0/24`, correlate
results with DHCP/DNS and device-management inventories, and update the file
SD list. Dynamic scanning is not scheduled by this change. For interface
traffic/errors or hardware health beyond ping, enable SNMP or the relevant
vendor API on those devices and add exporters/credentials with read-only
permissions. The other Shams, SPC, and Dubai South networks remain out of the
public-relay coverage until my01 has verified routes and firewall/return-path
access to them.

## Coverage boundary

The two public SSH endpoints provide a second collection path for my01 and
my02. Other Medan hosts, gateways, Shams, Dubai South, and SPC remain monitored
through their existing private/VPN routes. The attached inventory shows no
public SSH endpoints for those systems through this pair; public-side checks
for them require their own authorized public endpoints or another reachable
probe location. A working my01/my02 tunnel does not by itself make the VPN
independent for those private subnets.
