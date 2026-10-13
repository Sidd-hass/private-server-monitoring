#!/bin/bash
# Writes the trunk container (fonouc_freeswitch_trunk) state and its SIP trunk gateway
# status as Prometheus metrics (node_exporter textfile collector).
# Only the trunk container is queried; the Kazoo media FreeSWITCH (fonouc_freeswitch) is ignored.
#
# Install on every node that has the trunk container (active or standby):
#   install -m 0755 freeswitch_trunk_metrics.sh /usr/local/sbin/fs_trunk_metrics.sh
#   echo '* * * * * root /usr/local/sbin/fs_trunk_metrics.sh >/dev/null 2>&1' > /etc/cron.d/fs_trunk_metrics
# OUT_DIR must match node_exporter's --collector.textfile.directory.
# Test without writing anything: fs_trunk_metrics.sh --stdout
CONTAINER=${CONTAINER:-fonouc_freeswitch_trunk}
ESL_PORT=${ESL_PORT:-8023}
OUT_DIR=${OUT_DIR:-/var/lib/node_exporter/textfile}
OUT=$OUT_DIR/freeswitch_trunk.prom
FS() { timeout 10 docker exec "$CONTAINER" /opt/freeswitch/bin/fs_cli -P "$ESL_PORT" -x "$1" 2>/dev/null; }
# Label values must not break the exposition format.
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
# Empty sample values make node_exporter reject the whole file.
num() { case "$1" in ''|*[!0-9]*) echo 0 ;; *) echo "$1" ;; esac; }

gen() {
  local list ok=1 up="" info="" uptime="" calls="" running=0 started restarts c
  c=$(esc "$CONTAINER")
  [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ] && running=1
  started=$(docker inspect -f '{{.State.StartedAt}}' "$CONTAINER" 2>/dev/null)
  [ -n "$started" ] && started=$(date -d "$started" +%s 2>/dev/null)
  restarts=$(docker inspect -f '{{.RestartCount}}' "$CONTAINER" 2>/dev/null)

  echo "# HELP trunk_container_running 1 if the trunk container is running, else 0"
  echo "# TYPE trunk_container_running gauge"
  echo "trunk_container_running{container=\"$c\"} $running"
  echo "# HELP trunk_container_start_time_seconds Unix time the trunk container last started"
  echo "# TYPE trunk_container_start_time_seconds gauge"
  echo "trunk_container_start_time_seconds{container=\"$c\"} $(num "$started")"
  echo "# HELP trunk_container_restart_count Docker restart count of the trunk container"
  echo "# TYPE trunk_container_restart_count gauge"
  echo "trunk_container_restart_count{container=\"$c\"} $(num "$restarts")"

  if [ $running = 1 ]; then
    list=$(FS "sofia status gateway") || ok=0
    [ -z "$list" ] && ok=0
  else
    ok=0
  fi

  if [ $ok = 1 ]; then
    while read -r pg; do
      gw=${pg#*::}; prof=${pg%%::*}
      d=$(FS "sofia status gateway $gw")
      v() { echo "$d" | awk -v k="$1" '$1==k{print $2; exit}'; }
      status=$(v Status); state=$(v State); proxy=$(v Proxy)
      user=$(echo "$list" | awk -v p="$pg" '$1==p{print $2}' | sed 's/^sip://;s/@.*//')
      g=$(esc "$gw")
      s=0; [ "$status" = "UP" ] && s=1
      up+="freeswitch_gateway_up{gateway=\"$g\",profile=\"$(esc "$prof")\",number=\"$(esc "$user")\"} $s"$'\n'
      info+="freeswitch_gateway_info{gateway=\"$g\",state=\"$(esc "$state")\",proxy=\"$(esc "$proxy")\"} 1"$'\n'
      uptime+="freeswitch_gateway_uptime_seconds{gateway=\"$g\"} $(num "$(v Uptime | tr -dc 0-9)")"$'\n'
      calls+="freeswitch_gateway_calls_total{gateway=\"$g\",direction=\"in\",result=\"all\"} $(num "$(v CallsIN)")"$'\n'
      calls+="freeswitch_gateway_calls_total{gateway=\"$g\",direction=\"out\",result=\"all\"} $(num "$(v CallsOUT)")"$'\n'
      calls+="freeswitch_gateway_calls_total{gateway=\"$g\",direction=\"in\",result=\"failed\"} $(num "$(v FailedCallsIN)")"$'\n'
      calls+="freeswitch_gateway_calls_total{gateway=\"$g\",direction=\"out\",result=\"failed\"} $(num "$(v FailedCallsOUT)")"$'\n'
    done < <(echo "$list" | awk '/::/ && $1 !~ /Gateway-Name/{print $1}')
  fi

  # Each metric family must be contiguous in the text format.
  echo "# HELP freeswitch_trunk_scrape_ok 1 if fs_cli on the trunk FreeSWITCH answered"
  echo "# TYPE freeswitch_trunk_scrape_ok gauge"
  echo "freeswitch_trunk_scrape_ok $ok"
  echo "# HELP freeswitch_gateway_up 1 if gateway Status is UP, else 0"
  echo "# TYPE freeswitch_gateway_up gauge"
  printf '%s' "$up"
  echo "# HELP freeswitch_gateway_info Gateway registration state as label"
  echo "# TYPE freeswitch_gateway_info gauge"
  printf '%s' "$info"
  echo "# HELP freeswitch_gateway_uptime_seconds Gateway uptime"
  echo "# TYPE freeswitch_gateway_uptime_seconds gauge"
  printf '%s' "$uptime"
  echo "# HELP freeswitch_gateway_calls_total Calls through gateway since FS start"
  echo "# TYPE freeswitch_gateway_calls_total counter"
  printf '%s' "$calls"
}

if [ "$1" = "--stdout" ]; then gen; exit; fi
mkdir -p "$OUT_DIR"
# Hosts without the trunk container publish nothing, so they never show as a down trunk.
if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then rm -f "$OUT"; exit 0; fi
gen > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"
