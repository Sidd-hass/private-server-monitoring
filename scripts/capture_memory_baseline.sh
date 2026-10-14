#!/bin/bash
# Captures a memory baseline for one Kazoo/FreeSWITCH node into a timestamped file.
# Run it once while the node is quiet and again under peak call load, then compare.
#
#   install -m 0755 capture_memory_baseline.sh /usr/local/sbin/capture_memory_baseline.sh
#   capture_memory_baseline.sh calm      # -> /var/tmp/mem-baseline/<host>-calm-<time>.txt
#   capture_memory_baseline.sh peak
#
# Each component is found on the host first, then in a running container whose name
# matches. Override when names differ:
#   RABBIT_CONTAINER, FS_CONTAINER, FS_ESL_PORT, COUCH_URL (e.g. http://user:pass@127.0.0.1:5984)
# Read-only: nothing is changed on the node.
LABEL=${1:-snapshot}
OUT_DIR=${OUT_DIR:-/var/tmp/mem-baseline}
FS_CONTAINER=${FS_CONTAINER:-fonouc_freeswitch}
FS_ESL_PORT=${FS_ESL_PORT:-8021}
COUCH_URL=${COUCH_URL:-http://127.0.0.1:5984}
OUT=$OUT_DIR/$(hostname -s)-$LABEL-$(date +%Y%m%d-%H%M%S).txt

have() { command -v "$1" >/dev/null 2>&1; }
# First running container whose name matches $1.
container() { have docker && docker ps --format '{{.Names}}' 2>/dev/null | grep -m1 -i "$1"; }
section() { printf '\n===== %s =====\n' "$1"; }

RABBIT_CONTAINER=${RABBIT_CONTAINER:-$(container rabbit)}
rabbitmqctl_() {
  if have rabbitmqctl; then timeout 30 rabbitmqctl "$@"
  elif [ -n "$RABBIT_CONTAINER" ]; then timeout 30 docker exec "$RABBIT_CONTAINER" rabbitmqctl "$@"
  else echo "rabbitmqctl not found (set RABBIT_CONTAINER)"; fi
}
fs_cli_() {
  if have fs_cli; then timeout 10 fs_cli -x "$1"
  # Exact name: a substring match would also hit fonouc_freeswitch_trunk.
  elif [ "$(docker inspect -f '{{.State.Running}}' "$FS_CONTAINER" 2>/dev/null)" = "true" ]; then
    timeout 10 docker exec "$FS_CONTAINER" /opt/freeswitch/bin/fs_cli -P "$FS_ESL_PORT" -x "$1"
  else echo "fs_cli not found (set FS_CONTAINER)"; fi
}
couch() { timeout 15 curl -s "$COUCH_URL$1" || echo "CouchDB not reachable at ${COUCH_URL%%@*}... (set COUCH_URL)"; }

gen() {
  section "host"
  echo "label=$LABEL host=$(hostname -f 2>/dev/null || hostname) date=$(date -Is)"
  uptime

  section "free -m"
  free -m

  section "per-process memory (largest first)"
  if have smem; then smem -r -k | head -30
  elif have ps_mem; then ps_mem 2>/dev/null | tail -30
  else ps -eo pid,rss,vsz,comm --sort=-rss | head -30
  fi

  section "rabbitmqctl status"
  rabbitmqctl_ status
  section "rabbitmqctl list_queues (non-empty or no consumers)"
  rabbitmqctl_ list_queues -q name messages consumers memory |
    awk 'NR==1 || $2 > 0 || $3 == 0' | sort -k2,2nr | head -50

  section "CouchDB _active_tasks"
  couch /_active_tasks; echo
  section "CouchDB database sizes (bytes, largest first)"
  for db in $(couch /_all_dbs | tr -d '[]"' | tr ',' ' '); do
    # Kazoo db names contain '/', which must be URL-encoded.
    printf '%s %s\n' "$(couch "/${db//\//%2F}" | grep -o '"file":[0-9]*' | cut -d: -f2)" "$db"
  done | sort -rn | head -30
  section "CouchDB compaction config"
  couch /_node/_local/_config/compactions; echo
  couch /_node/_local/_config/compaction_daemon; echo

  section "FreeSWITCH channels"
  fs_cli_ "show channels count"

  section "docker stats"
  have docker && docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}'

  section "systemd memory limits"
  for u in freeswitch kazoo rabbitmq-server couchdb valkey redis docker; do
    systemctl list-units --all --no-legend "$u*" 2>/dev/null | awk '{print $1}' | while read -r unit; do
      echo "$unit $(systemctl show -p MemoryCurrent -p MemoryMax "$unit" | tr '\n' ' ')"
    done
  done
}

mkdir -p "$OUT_DIR"
gen > "$OUT" 2>&1
echo "$OUT"
