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
not log in or collect host metrics.

`medan_public_ssh_tunnel` scrapes Node Exporter through local TCP forwards
created by persistent SSH sessions from the central monitoring host to each
public SSH endpoint. Node Exporter must already be running on the corresponding
private interface and port on each server. The existing VPN-based scrape
continues independently; dashboard queries can use `server_name` to compare
both paths.

## SSH tunnel prerequisites

1. The monitoring host needs a dedicated SSH key that is authorized for the
   intended account on both servers. Keep the private key outside this repo and
   restrict its file permissions. Do not use the passwords shown in the server
   inventory or commit credentials.
2. Verify and pin each server's SSH host key through a trusted channel before
   enabling unattended SSH. Do not accept an unverified host key.
3. From the monitoring host, verify that the public IP/port routes to the
   intended server and that the account can reach that server's Node Exporter
   at its private address. Public NAT loopback may not work when the monitoring
   host is inside the Medan network; validate this route from the actual host.
4. Confirm firewall policy allows the SSH tunnel process to listen on local
   ports `19101` and `19102` for the Prometheus Docker network only. Do not
   expose these Node Exporter tunnel ports to the public Internet.

## Tunnel commands

Run these under a service manager such as systemd with an `autossh` process,
using the dedicated key and a verified `known_hosts` file. Bind the local
forward to an address reachable only by the Prometheus container network, and
restrict it with the host firewall. The examples use placeholders for the
monitoring host's Docker bridge gateway address. Replace the sample gateway
with the host-gateway address resolved for `host.docker.internal` inside the
Prometheus container; do not bind the ports to a public interface.

```sh
DOCKER_BRIDGE_GATEWAY=172.18.0.1
AUTOSSH_GATETIME=0 autossh -M 0 -N \
  -o BatchMode=yes -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -o StrictHostKeyChecking=yes \
  -i /etc/ssh/monitoring-medan \
  -p 11446 \
  -L "${DOCKER_BRIDGE_GATEWAY}:19101:192.168.99.46:9100" \
  root@94.206.56.70
```

```sh
DOCKER_BRIDGE_GATEWAY=172.18.0.1
AUTOSSH_GATETIME=0 autossh -M 0 -N \
  -o BatchMode=yes -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -o StrictHostKeyChecking=yes \
  -i /etc/ssh/monitoring-medan \
  -p 11447 \
  -L "${DOCKER_BRIDGE_GATEWAY}:19102:192.168.99.47:9100" \
  root@94.206.56.71
```

Create one restartable service per command and substitute a verified local
gateway address that Prometheus can reach. Start the services only after
installing the SSH key and host keys. Then check `medan_public_ssh_tunnel`
targets in Prometheus. If the VPN is down but the public SSH route and tunnel
remain healthy, these scrapes continue to expose CPU, memory, filesystem,
interface traffic, packet errors/drops, and TCP/socket metrics from Node
Exporter.

## Coverage boundary

The two public SSH endpoints provide a second collection path for my01 and
my02. Other Medan hosts, gateways, Shams, Dubai South, and SPC remain monitored
through their existing private/VPN routes. The attached inventory shows no
public SSH endpoints for those systems through this pair; public-side checks
for them require their own authorized public endpoints or another reachable
probe location. A working my01/my02 tunnel does not by itself make the VPN
independent for those private subnets.
