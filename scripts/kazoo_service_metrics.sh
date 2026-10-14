#!/bin/bash
# Writes Docker, CouchDB, RabbitMQ, FreeSWITCH and Valkey health on a Kazoo node as
# Prometheus metrics (node_exporter textfile collector). Read-only: it only runs
# docker stats/inspect, local HTTP GETs and status commands. Nothing is restarted or changed.
#
# Install on each Kazoo node:
#   install -m 0755 kazoo_service_metrics.sh /usr/local/sbin/kazoo_service_metrics.sh
#   install -m 0600 /dev/null /etc/default/kazoo_service_metrics   # or kazoo_service_metrics.env next to the script
#   cat > /etc/cron.d/kazoo_service_metrics <<'EOF'
#   * * * * *    root /usr/local/sbin/kazoo_service_metrics.sh >/dev/null 2>&1
#   */15 * * * * root /usr/local/sbin/kazoo_service_metrics.sh --couch-sizes >/dev/null 2>&1
#   EOF
#
# Settings (environment, /etc/default/kazoo_service_metrics, or kazoo_service_metrics.env beside the script):
#   OUT_DIR          must match node_exporter's --collector.textfile.directory
#   COUCH_URL        http://user:pass@127.0.0.1:5984
#   FS_CONTAINER     media FreeSWITCH container (exact name), FS_ESL_PORT its ESL port
#   VALKEY_PASS      Valkey/Redis password, if any
#   VALKEY_CLI_ARGS  extra valkey-cli options when it does not listen on 127.0.0.1:6379, e.g. "-h 10.0.0.5 -p 6380"
#   RABBIT_CONTAINER RabbitMQ container, when the name does not contain "rabbit"
#   RABBIT_METRICS_URL rabbitmq_prometheus endpoint (default http://127.0.0.1:15692/metrics)
#   VALKEY_SENTINEL_ARGS / VALKEY_SENTINEL_PASS  Sentinel valkey-cli options (default "-p 26379") and password (default VALKEY_PASS)
#   VALKEY_CONTAINER / VALKEY_SENTINEL_CONTAINER  container names when auto-detection (first "valkey") is wrong
# The CouchDB /_system + /_stats and RabbitMQ memory breakdown sections need jq.
# Test without writing anything:  kazoo_service_metrics.sh --stdout
#                                 kazoo_service_metrics.sh --couch-sizes --stdout
# cron runs with PATH=/usr/bin:/bin; rabbitmqctl and friends live in /usr/sbin.
PATH=$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/sbin
[ -r /etc/default/kazoo_service_metrics ] && . /etc/default/kazoo_service_metrics
# A settings file next to the script wins, so a self-contained install dir works too.
[ -r "$(dirname "$(readlink -f "$0")")/kazoo_service_metrics.env" ] && . "$(dirname "$(readlink -f "$0")")/kazoo_service_metrics.env"
OUT_DIR=${OUT_DIR:-/var/lib/node_exporter/textfile}
COUCH_URL=${COUCH_URL:-http://127.0.0.1:5984}
FS_CONTAINER=${FS_CONTAINER:-fonouc_freeswitch}
FS_ESL_PORT=${FS_ESL_PORT:-8021}
COUCH_TOP_DBS=${COUCH_TOP_DBS:-20}
RABBIT_TOP_QUEUES=${RABBIT_TOP_QUEUES:-20}
RABBIT_METRICS_URL=${RABBIT_METRICS_URL:-http://127.0.0.1:15692/metrics}
VALKEY_SENTINEL_ARGS=${VALKEY_SENTINEL_ARGS:--p 26379}
VALKEY_SENTINEL_PASS=${VALKEY_SENTINEL_PASS:-$VALKEY_PASS}
# Per-service cgroup metrics: systemd services smaller than this are skipped (containers always kept).
CGROUP_MIN_MB=${CGROUP_MIN_MB:-20}
# Log analysis (per-database CouchDB activity, error counts and top error messages).
LOG_METRICS=${LOG_METRICS:-1}
STATE_DIR=${STATE_DIR:-/var/lib/kazoo_service_metrics}
LOG_MAX_BYTES=${LOG_MAX_BYTES:-50000000}
LOG_SAMPLE_BYTES=${LOG_SAMPLE_BYTES:-3000000}
COUCH_LOG=${COUCH_LOG:-/var/log/couchdb/couchdb.log}
KAZOO_LOG=${KAZOO_LOG:-/var/log/kazoo/kazoo.log}
# Newest node log: hosts renamed over time keep an old *@localhost.log next to the live one.
RABBIT_LOG=${RABBIT_LOG:-$(ls -t /var/log/rabbitmq/*@*.log 2>/dev/null | head -1)}
FS_ERROR_LOG=${FS_ERROR_LOG:-/var/log/freeswitch/kazoo-error.log}
STDOUT=0

have() { command -v "$1" >/dev/null 2>&1; }
# Label values must not break the exposition format.
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
# Empty or non-numeric sample values make node_exporter reject the whole file.
num() { case "$1" in ''|*[!0-9.eE+-]*) echo 0 ;; *) echo "$1" ;; esac; }
running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }
# First running container whose name matches the regex.
find_container() { docker ps --format '{{.Names}}' 2>/dev/null | grep -m1 -E "$1"; }
couch() { timeout 15 curl -sf "$COUCH_URL$1"; }
meta() { printf '# HELP %s %s\n# TYPE %s %s\n' "$1" "$3" "$1" "$2"; }

docker_metrics() {
  local up="" restarts="" started="" limit="" mem="" cpu="" pids="" name run rc st lim
  have docker || return
  while read -r name run rc st lim; do
    name=$(esc "${name#/}")
    up+="kazoo_container_running{container=\"$name\"} $([ "$run" = true ] && echo 1 || echo 0)"$'\n'
    restarts+="kazoo_container_restart_count{container=\"$name\"} $(num "$rc")"$'\n'
    # Never-started containers report year 0001.
    case "$st" in 0001-*) st=0 ;; *) st=$(date -d "$st" +%s 2>/dev/null) ;; esac
    started+="kazoo_container_start_time_seconds{container=\"$name\"} $(num "$st")"$'\n'
    limit+="kazoo_container_memory_limit_bytes{container=\"$name\"} $(num "$lim")"$'\n'
  done < <(docker inspect -f '{{.Name}} {{.State.Running}} {{.RestartCount}} {{.State.StartedAt}} {{.HostConfig.Memory}}' $(docker ps -aq) 2>/dev/null)

  # docker stats prints sizes like "1.2GiB / 31.3GiB"; convert to bytes.
  while IFS='|' read -r name m c p; do
    name=$(esc "$name")
    mem+="kazoo_container_memory_bytes{container=\"$name\"} $(echo "${m%% /*}" | awk '
      { v=$0; sub(/[A-Za-z]+$/, "", v); u=substr($0, length(v)+1)
        f["B"]=1; f["kB"]=1e3; f["KB"]=1e3; f["MB"]=1e6; f["GB"]=1e9; f["TB"]=1e12
        f["KiB"]=1024; f["MiB"]=1048576; f["GiB"]=1073741824; f["TiB"]=1099511627776
        printf "%.0f", v * (u in f ? f[u] : 1) }')"$'\n'
    cpu+="kazoo_container_cpu_percent{container=\"$name\"} $(num "${c%\%}")"$'\n'
    pids+="kazoo_container_pids{container=\"$name\"} $(num "$p")"$'\n'
  done < <(timeout 30 docker stats --no-stream --format '{{.Name}}|{{.MemUsage}}|{{.CPUPerc}}|{{.PIDs}}' 2>/dev/null)

  meta kazoo_container_running gauge "1 if the container is running"; printf '%s' "$up"
  meta kazoo_container_restart_count gauge "Docker restart count"; printf '%s' "$restarts"
  meta kazoo_container_start_time_seconds gauge "Unix time the container last started"; printf '%s' "$started"
  meta kazoo_container_memory_limit_bytes gauge "Container memory limit, 0 = unlimited"; printf '%s' "$limit"
  meta kazoo_container_memory_bytes gauge "Container memory usage as reported by docker stats"; printf '%s' "$mem"
  meta kazoo_container_cpu_percent gauge "Container CPU usage, 100 = one core"; printf '%s' "$cpu"
  meta kazoo_container_pids gauge "Processes/threads in the container"; printf '%s' "$pids"
}

couch_metrics() {
  local up=0 tasks
  couch / >/dev/null && up=1
  meta kazoo_couchdb_up gauge "1 if CouchDB answered on COUCH_URL"
  echo "kazoo_couchdb_up $up"
  [ $up = 1 ] || return
  tasks=$(couch /_active_tasks)
  meta kazoo_couchdb_active_tasks gauge "Running CouchDB tasks by type (compaction, indexer, replication)"
  for t in database_compaction view_compaction indexer replication; do
    echo "kazoo_couchdb_active_tasks{type=\"$t\"} $(printf '%s' "$tasks" | grep -o "\"type\":\"$t\"" | wc -l)"
  done
  have jq || return   # the detailed sections below need jq

  # Erlang VM view of this CouchDB node (needs admin).
  couch /_node/_local/_system | jq -r '
    "# HELP kazoo_couchdb_erlang_memory_bytes CouchDB Erlang VM memory by type",
    "# TYPE kazoo_couchdb_erlang_memory_bytes gauge",
    (.memory | to_entries[] | select(.key | endswith("_used") | not)
      | "kazoo_couchdb_erlang_memory_bytes{kind=\"\(.key)\"} \(.value // 0)"),
    "# HELP kazoo_couchdb_processes Erlang processes (limit in kazoo_couchdb_process_limit)",
    "# TYPE kazoo_couchdb_processes gauge", "kazoo_couchdb_processes \(.process_count // 0)",
    "# TYPE kazoo_couchdb_process_limit gauge", "kazoo_couchdb_process_limit \(.process_limit // 0)",
    "# HELP kazoo_couchdb_run_queue Erlang run queue; sustained > 0 means CPU-starved",
    "# TYPE kazoo_couchdb_run_queue gauge", "kazoo_couchdb_run_queue \(.run_queue // 0)",
    "# HELP kazoo_couchdb_os_processes couchjs/query server processes",
    "# TYPE kazoo_couchdb_os_processes gauge", "kazoo_couchdb_os_processes \(.os_proc_count // 0)",
    "# HELP kazoo_couchdb_open_files Open database/view files (couch_file processes)",
    "# TYPE kazoo_couchdb_open_files gauge", "kazoo_couchdb_open_files \(.message_queues.couch_file.count // 0)",
    "# HELP kazoo_couchdb_io_bytes_total Erlang VM network/disk port I/O",
    "# TYPE kazoo_couchdb_io_bytes_total counter",
    "kazoo_couchdb_io_bytes_total{direction=\"in\"} \(.io_input // 0)",
    "kazoo_couchdb_io_bytes_total{direction=\"out\"} \(.io_output // 0)",
    "# HELP kazoo_couchdb_message_queue Longest Erlang mailbox; growth means a component is falling behind",
    "# TYPE kazoo_couchdb_message_queue gauge",
    "kazoo_couchdb_message_queue{process=\"couch_server\"} \(.message_queues.couch_server // 0)",
    "kazoo_couchdb_message_queue{process=\"couch_file\"} \(.message_queues.couch_file.max // 0)",
    "kazoo_couchdb_message_queue{process=\"couch_db_updater\"} \(.message_queues.couch_db_updater.max // 0)",
    "kazoo_couchdb_message_queue{process=\"any\"} \([.message_queues[] | if type == "object" then .max else . end] | max // 0)"
  ' 2>/dev/null

  # Every counter, gauge and histogram from /_stats, keyed by its dotted path,
  # e.g. kazoo_couchdb_stat_total{stat="couchdb.database_reads"}.
  couch /_node/_local/_stats | jq -r '
    [paths(type == "object" and has("type") and has("value"))] as $ps
    | def name($p): $p | map(tostring) | join(".");
      "# HELP kazoo_couchdb_stat_total CouchDB /_stats counters since node start",
      "# TYPE kazoo_couchdb_stat_total counter",
      ($ps[] as $p | getpath($p) | select(.type == "counter") | "kazoo_couchdb_stat_total{stat=\"\(name($p))\"} \(.value // 0)"),
      "# HELP kazoo_couchdb_stat CouchDB /_stats gauges",
      "# TYPE kazoo_couchdb_stat gauge",
      ($ps[] as $p | getpath($p) | select(.type == "gauge") | "kazoo_couchdb_stat{stat=\"\(name($p))\"} \(.value // 0)"),
      "# HELP kazoo_couchdb_stat_recent CouchDB /_stats histograms over the last 10s (request_time is in ms)",
      "# TYPE kazoo_couchdb_stat_recent gauge",
      ($ps[] as $p | getpath($p) | select(.type == "histogram") | .value as $v
        | ( ($v.percentile // [])[] | select(.[0] == 50 or .[0] == 95 or .[0] == 99)
              | "kazoo_couchdb_stat_recent{stat=\"\(name($p))\",q=\"\(.[0] / 100)\"} \(.[1] // 0)" ),
          "kazoo_couchdb_stat_recent{stat=\"\(name($p))\",q=\"mean\"} \($v.arithmetic_mean // 0)",
          "kazoo_couchdb_stat_recent{stat=\"\(name($p))\",q=\"max\"} \($v.max // 0)")
  ' 2>/dev/null
}

# Slow on Kazoo (one request per account/MODB database), so it runs on its own cron line.
couch_size_metrics() {
  local dbs db info file active n=0 tf=0 ta=0 rows=""
  dbs=$(couch /_all_dbs | tr -d '[]"' | tr ',' '\n')
  [ -n "$dbs" ] || return
  while read -r db; do
    [ -n "$db" ] || continue
    # Kazoo database names contain '/', which must be URL-encoded.
    info=$(couch "/${db//\//%2F}")
    # CouchDB 2+/3: sizes.file/active; 1.x/BigCouch: disk_size/data_size.
    file=$(printf '%s' "$info" | grep -oE '"sizes":\{[^}]*\}' | grep -oE '"file":[0-9]+' | cut -d: -f2)
    active=$(printf '%s' "$info" | grep -oE '"sizes":\{[^}]*\}' | grep -oE '"active":[0-9]+' | cut -d: -f2)
    [ -z "$file" ] && file=$(printf '%s' "$info" | grep -oE '"disk_size":[0-9]+' | cut -d: -f2)
    [ -z "$active" ] && active=$(printf '%s' "$info" | grep -oE '"data_size":[0-9]+' | cut -d: -f2)
    file=$(num "$file"); active=$(num "$active")
    docs=$(num "$(printf '%s' "$info" | grep -oE '"doc_count":[0-9]+' | head -1 | cut -d: -f2)")
    deleted=$(num "$(printf '%s' "$info" | grep -oE '"doc_del_count":[0-9]+' | head -1 | cut -d: -f2)")
    n=$((n + 1)); tf=$((tf + file)); ta=$((ta + active))
    rows+="$file $active $docs $deleted $db"$'\n'
  done <<< "$dbs"

  meta kazoo_couchdb_databases gauge "Number of CouchDB databases"; echo "kazoo_couchdb_databases $n"
  meta kazoo_couchdb_file_bytes_sum gauge "On-disk size of all databases"; echo "kazoo_couchdb_file_bytes_sum $tf"
  meta kazoo_couchdb_active_bytes_sum gauge "Live data in all databases; file - active is reclaimable by compaction"
  echo "kazoo_couchdb_active_bytes_sum $ta"
  # Only the largest databases get their own series, to keep cardinality bounded.
  printf '%s' "$rows" | sort -rn | head -n "$COUCH_TOP_DBS" | awk '
    { gsub(/[\\"]/, "", $5); f[NR] = $1; a[NR] = $2; d[NR] = $3; x[NR] = $4; db[NR] = $5 }
    function fam(name, help, v,   i) {
      print "# HELP " name " " help; print "# TYPE " name " gauge"
      for (i = 1; i <= NR; i++) printf "%s{db=\"%s\"} %s\n", name, db[i], v[i]
    }
    END {
      fam("kazoo_couchdb_database_file_bytes", "On-disk size of the largest databases", f)
      fam("kazoo_couchdb_database_active_bytes", "Live data in the largest databases", a)
      fam("kazoo_couchdb_database_docs", "Documents in the largest databases", d)
      fam("kazoo_couchdb_database_deleted_docs", "Deleted documents (tombstones) in the largest databases", x)
    }'
}

rabbit_metrics() {
  local c="${RABBIT_CONTAINER:-$(find_container rabbit)}" up=0 q mem limit
  if have rabbitmqctl; then ctl() { timeout 30 rabbitmqctl "$@"; }; diag() { timeout 30 rabbitmq-diagnostics "$@"; }
  elif [ -n "$c" ]; then ctl() { timeout 30 docker exec "$c" rabbitmqctl "$@"; }; diag() { timeout 30 docker exec "$c" rabbitmq-diagnostics "$@"; }
  else return; fi   # no broker on this node: publish nothing rather than a false "down"
  q=$(ctl list_queues -q name messages_ready messages_unacknowledged consumers memory 2>/dev/null) && up=1
  meta kazoo_rabbitmq_up gauge "1 if rabbitmqctl answered"; echo "kazoo_rabbitmq_up $up"
  [ $up = 1 ] || return
  # Drop the header row that some versions print even with -q.
  q=$(printf '%s\n' "$q" | awk 'NF == 5 && $2 ~ /^[0-9]+$/')
  mem=$(ctl eval 'erlang:memory(total).' 2>/dev/null | tr -dc 0-9)
  limit=$(ctl eval 'vm_memory_monitor:get_memory_limit().' 2>/dev/null | tr -dc 0-9)
  meta kazoo_rabbitmq_memory_bytes gauge "Broker Erlang VM memory"; echo "kazoo_rabbitmq_memory_bytes $(num "$mem")"
  meta kazoo_rabbitmq_memory_limit_bytes gauge "Memory high watermark; publishers block above it"
  echo "kazoo_rabbitmq_memory_limit_bytes $(num "$limit")"
  printf '%s\n' "$q" | awk '
    { n++; r+=$2; u+=$3; m+=$5; if ($4 == 0 && $2 + $3 > 0) orphan++ }
    END {
      print "# HELP kazoo_rabbitmq_queues Number of queues\n# TYPE kazoo_rabbitmq_queues gauge"
      printf "kazoo_rabbitmq_queues %d\n", n
      print "# HELP kazoo_rabbitmq_messages_ready Messages waiting for a consumer, all queues\n# TYPE kazoo_rabbitmq_messages_ready gauge"
      printf "kazoo_rabbitmq_messages_ready %d\n", r
      print "# HELP kazoo_rabbitmq_messages_unacked Delivered but unacknowledged messages, all queues\n# TYPE kazoo_rabbitmq_messages_unacked gauge"
      printf "kazoo_rabbitmq_messages_unacked %d\n", u
      print "# HELP kazoo_rabbitmq_queue_memory_bytes Memory held by all queues\n# TYPE kazoo_rabbitmq_queue_memory_bytes gauge"
      printf "kazoo_rabbitmq_queue_memory_bytes %d\n", m
      print "# HELP kazoo_rabbitmq_queues_without_consumers Queues holding messages with no consumer\n# TYPE kazoo_rabbitmq_queues_without_consumers gauge"
      printf "kazoo_rabbitmq_queues_without_consumers %d\n", orphan
    }'
  meta kazoo_rabbitmq_queue_messages gauge "Ready + unacked messages in the deepest non-empty queues"
  printf '%s\n' "$q" | awk '$2 + $3 > 0 { print $2 + $3, $4, $1 }' | sort -rn | head -n "$RABBIT_TOP_QUEUES" |
    while read -r msgs cons name; do echo "kazoo_rabbitmq_queue_messages{queue=\"$(esc "$name")\",consumers=\"$cons\"} $msgs"; done

  # Who holds the connections/channels: aggregated per client host.
  ctl list_connections -q peer_host channels 2>/dev/null | awk '
    NF == 2 && $2 ~ /^[0-9]+$/ { n[$1]++; ch[$1] += $2 }
    END {
      print "# HELP kazoo_rabbitmq_peer_connections AMQP connections per client host\n# TYPE kazoo_rabbitmq_peer_connections gauge"
      for (h in n) printf "kazoo_rabbitmq_peer_connections{peer=\"%s\"} %d\n", h, n[h]
      print "# HELP kazoo_rabbitmq_peer_channels AMQP channels per client host\n# TYPE kazoo_rabbitmq_peer_channels gauge"
      for (h in n) printf "kazoo_rabbitmq_peer_channels{peer=\"%s\"} %d\n", h, ch[h]
    }'

  # Where the broker memory goes (connections, queues, mgmt db, ...): rabbitmqctl status breakdown.
  if have jq; then
    meta kazoo_rabbitmq_memory_breakdown_bytes gauge "Broker memory by category (rabbitmq-diagnostics memory_breakdown)"
    diag -q memory_breakdown --unit bytes --formatter json 2>/dev/null |
      jq -r 'to_entries[] | "kazoo_rabbitmq_memory_breakdown_bytes{category=\"\(.key)\"} \(.value.bytes // 0)"' 2>/dev/null
  fi

  # Full broker metrics (rates, churn, connections, disk, fds...) from the built-in
  # rabbitmq_prometheus plugin, when it is enabled. Read locally; no port needs opening.
  timeout 15 curl -sf "$RABBIT_METRICS_URL" 2>/dev/null | grep -E '^(# (HELP|TYPE) )?rabbitmq_'
}

freeswitch_metrics() {
  local up=0 ch calls st sessions peak
  fs() {
    if have fs_cli; then timeout 10 fs_cli -P "$FS_ESL_PORT" -x "$1"
    else timeout 10 docker exec "$FS_CONTAINER" /opt/freeswitch/bin/fs_cli -P "$FS_ESL_PORT" -x "$1"; fi 2>/dev/null
  }
  have fs_cli || running "$FS_CONTAINER" || return
  st=$(fs status) && [ -n "$st" ] && up=1
  meta kazoo_freeswitch_up gauge "1 if media FreeSWITCH answered on ESL"; echo "kazoo_freeswitch_up $up"
  [ $up = 1 ] || return
  ch=$(fs "show channels count" | grep -oE '^[0-9]+ total' | tr -dc 0-9)
  calls=$(fs "show calls count" | grep -oE '^[0-9]+ total' | tr -dc 0-9)
  # "123 session(s) since startup" / "5 session(s) - peak 40, last 5min 12"
  sessions=$(printf '%s' "$st" | grep -oE '[0-9]+ session\(s\) since startup' | tr -dc 0-9)
  peak=$(printf '%s' "$st" | grep -oE 'peak [0-9]+' | head -1 | tr -dc 0-9)
  meta kazoo_freeswitch_channels gauge "Active channels (call legs)"; echo "kazoo_freeswitch_channels $(num "$ch")"
  meta kazoo_freeswitch_calls gauge "Active bridged calls"; echo "kazoo_freeswitch_calls $(num "$calls")"
  meta kazoo_freeswitch_sessions_total counter "Sessions since FreeSWITCH started"; echo "kazoo_freeswitch_sessions_total $(num "$sessions")"
  meta kazoo_freeswitch_sessions_peak gauge "Peak concurrent sessions since start"; echo "kazoo_freeswitch_sessions_peak $(num "$peak")"
}

valkey_metrics() {
  local c info up=0 cli
  # Prefer a valkey container: other apps may run their own redis (e.g. fonouc-cx-channels-redis).
  c=${VALKEY_CONTAINER:-$(docker ps --format '{{.Names}}' 2>/dev/null | grep -v sentinel | grep -m1 valkey)}
  [ -n "$c" ] || return
  cli=valkey-cli; docker exec "$c" sh -c 'command -v valkey-cli' >/dev/null 2>&1 || cli=redis-cli
  info=$(timeout 10 docker exec ${VALKEY_PASS:+-e REDISCLI_AUTH="$VALKEY_PASS"} "$c" $cli $VALKEY_CLI_ARGS INFO everything 2>/dev/null | tr -d '\r') &&
    printf '%s' "$info" | grep -q '^used_memory:' && up=1
  v() { printf '%s' "$info" | awk -F: -v k="$1" '$1 == k { print $2; exit }'; }
  c=$(esc "$c")
  meta kazoo_valkey_up gauge "1 if Valkey answered INFO"; echo "kazoo_valkey_up{container=\"$c\"} $up"
  [ $up = 1 ] || return
  meta kazoo_valkey_memory_used_bytes gauge "used_memory"; echo "kazoo_valkey_memory_used_bytes{container=\"$c\"} $(num "$(v used_memory)")"
  meta kazoo_valkey_maxmemory_bytes gauge "maxmemory, 0 = unlimited"; echo "kazoo_valkey_maxmemory_bytes{container=\"$c\"} $(num "$(v maxmemory)")"
  meta kazoo_valkey_connected_clients gauge "connected_clients"; echo "kazoo_valkey_connected_clients{container=\"$c\"} $(num "$(v connected_clients)")"
  meta kazoo_valkey_evicted_keys_total counter "Keys evicted because of maxmemory"; echo "kazoo_valkey_evicted_keys_total{container=\"$c\"} $(num "$(v evicted_keys)")"
  meta kazoo_valkey_keys gauge "Keys in db0"; echo "kazoo_valkey_keys{container=\"$c\"} $(num "$(v db0 | grep -oE 'keys=[0-9]+' | cut -d= -f2)")"
  meta kazoo_valkey_is_master gauge "1 if this instance is the master"
  echo "kazoo_valkey_is_master{container=\"$c\"} $([ "$(v role)" = master ] && echo 1 || echo 0)"
  meta kazoo_valkey_master_link_up gauge "Replica only: 1 if the link to the master is up"
  [ "$(v role)" = slave ] && echo "kazoo_valkey_master_link_up{container=\"$c\"} $([ "$(v master_link_status)" = up ] && echo 1 || echo 0)"

  # Everything else from INFO: every numeric field, per-command stats, error counts, keyspace.
  printf '%s\n' "$info" | awk -F: -v c="$c" '
    /^cmdstat_/ {
      cmd = substr($1, 9); split($2, kv, ",")
      for (i in kv) { split(kv[i], p, "="); s[cmd, p[1]] = p[2] }
      cmds[cmd] = 1; next
    }
    /^errorstat_/ { split($2, p, "="); err[substr($1, 11)] = p[2]; next }
    /^db[0-9]+:/ {
      split($2, kv, ",")
      for (i in kv) { split(kv[i], p, "="); if (p[1] == "keys") keys[$1] = p[2]; if (p[1] == "expires") exp_[$1] = p[2] }
      next
    }
    $1 ~ /^(process_id|tcp_port|server_time_usec|lru_clock|arch_bits|executable|run_id|master_replid2?|config_file|availability_zone)$/ { next }
    $2 ~ /^-?[0-9]+(\.[0-9]+)?$/ { f[$1] = $2; next }
    # rdb_last_bgsave_status, aof_last_write_status, master_link_status, ...
    $1 ~ /_status$/ { ok[$1] = ($2 == "ok" || $2 == "up") }
    END {
      print "# HELP kazoo_valkey_status_ok 1 if the INFO *_status field is ok/up (persistence, AOF, replication)\n# TYPE kazoo_valkey_status_ok gauge"
      for (k in ok) printf "kazoo_valkey_status_ok{container=\"%s\",field=\"%s\"} %d\n", c, k, ok[k]
      print "# HELP kazoo_valkey_info Numeric INFO field (counters and gauges, see the field name)\n# TYPE kazoo_valkey_info untyped"
      for (k in f) printf "kazoo_valkey_info{container=\"%s\",field=\"%s\"} %s\n", c, k, f[k]
      print "# HELP kazoo_valkey_command_calls_total Calls per command\n# TYPE kazoo_valkey_command_calls_total counter"
      for (k in cmds) printf "kazoo_valkey_command_calls_total{container=\"%s\",cmd=\"%s\"} %s\n", c, k, s[k, "calls"] + 0
      print "# HELP kazoo_valkey_command_usec_total Time spent per command in microseconds\n# TYPE kazoo_valkey_command_usec_total counter"
      for (k in cmds) printf "kazoo_valkey_command_usec_total{container=\"%s\",cmd=\"%s\"} %s\n", c, k, s[k, "usec"] + 0
      print "# HELP kazoo_valkey_command_failed_total Rejected + failed calls per command\n# TYPE kazoo_valkey_command_failed_total counter"
      for (k in cmds) printf "kazoo_valkey_command_failed_total{container=\"%s\",cmd=\"%s\"} %s\n", c, k, s[k, "rejected_calls"] + s[k, "failed_calls"]
      print "# HELP kazoo_valkey_errors_total Error replies by error prefix\n# TYPE kazoo_valkey_errors_total counter"
      for (k in err) printf "kazoo_valkey_errors_total{container=\"%s\",error=\"%s\"} %s\n", c, k, err[k]
      print "# HELP kazoo_valkey_db_keys Keys per database\n# TYPE kazoo_valkey_db_keys gauge"
      for (k in keys) printf "kazoo_valkey_db_keys{container=\"%s\",db=\"%s\"} %s\n", c, k, keys[k]
      print "# HELP kazoo_valkey_db_expires Keys with a TTL per database\n# TYPE kazoo_valkey_db_expires gauge"
      for (k in exp_) printf "kazoo_valkey_db_expires{container=\"%s\",db=\"%s\"} %s\n", c, k, exp_[k]
    }'
}

# Sentinel's view of the master it watches: is it reachable, how many replicas and
# sentinels agree. A failover shows up as a new addr label on kazoo_valkey_sentinel_master_info.
valkey_sentinel_metrics() {
  local c out up=0 cli=valkey-cli
  c=${VALKEY_SENTINEL_CONTAINER:-$(docker ps --format '{{.Names}}' 2>/dev/null | grep sentinel | grep -m1 -E 'valkey|redis')}
  [ -n "$c" ] || return
  docker exec "$c" sh -c 'command -v valkey-cli' >/dev/null 2>&1 || cli=redis-cli
  out=$(timeout 10 docker exec ${VALKEY_SENTINEL_PASS:+-e REDISCLI_AUTH="$VALKEY_SENTINEL_PASS"} "$c" $cli $VALKEY_SENTINEL_ARGS SENTINEL masters 2>/dev/null | tr -d '\r') &&
    printf '%s' "$out" | grep -qx name && up=1
  c=$(esc "$c")
  meta kazoo_valkey_sentinel_up gauge "1 if Sentinel answered"; echo "kazoo_valkey_sentinel_up{container=\"$c\"} $up"
  [ $up = 1 ] || return
  # valkey-cli prints SENTINEL masters as alternating key/value lines; each master starts with "name".
  printf '%s\n' "$out" | awk -v c="$c" '
    NR % 2 == 1 { k = $0; next }
    { if (k == "name") { m = $0; ms[m] = 1 } v[m, k] = $0 }
    END {
      print "# HELP kazoo_valkey_sentinel_master_ok 1 if Sentinel sees the master as healthy (no s_down/o_down)\n# TYPE kazoo_valkey_sentinel_master_ok gauge"
      for (m in ms) printf "kazoo_valkey_sentinel_master_ok{container=\"%s\",master=\"%s\"} %d\n", c, m, (v[m, "flags"] == "master")
      print "# HELP kazoo_valkey_sentinel_master_info Current master address and flags\n# TYPE kazoo_valkey_sentinel_master_info gauge"
      for (m in ms) printf "kazoo_valkey_sentinel_master_info{container=\"%s\",master=\"%s\",addr=\"%s:%s\",flags=\"%s\"} 1\n", c, m, v[m, "ip"], v[m, "port"], v[m, "flags"]
      print "# HELP kazoo_valkey_sentinel_replicas Replicas Sentinel knows for the master\n# TYPE kazoo_valkey_sentinel_replicas gauge"
      for (m in ms) printf "kazoo_valkey_sentinel_replicas{container=\"%s\",master=\"%s\"} %d\n", c, m, v[m, "num-slaves"]
      print "# HELP kazoo_valkey_sentinel_sentinels Sentinels watching the master, including this one\n# TYPE kazoo_valkey_sentinel_sentinels gauge"
      for (m in ms) printf "kazoo_valkey_sentinel_sentinels{container=\"%s\",master=\"%s\"} %d\n", c, m, v[m, "num-other-sentinels"] + 1
      print "# HELP kazoo_valkey_sentinel_quorum Sentinels needed to agree on a failover\n# TYPE kazoo_valkey_sentinel_quorum gauge"
      for (m in ms) printf "kazoo_valkey_sentinel_quorum{container=\"%s\",master=\"%s\"} %d\n", c, m, v[m, "quorum"]
    }'
}

# ---------------------------------------------------------------------------
# Per-service memory, CPU, I/O and pressure from cgroup v2: every Docker container and
# every systemd service above CGROUP_MIN_MB. Pressure (PSI) is the time tasks in that
# service were stalled waiting for memory/CPU/IO, i.e. real contention, not just usage.
cgroup_metrics() {
  local root=/sys/fs/cgroup/system.slice d name svc ids
  [ -r "$root/cgroup.procs" ] || return
  ids=$(docker ps --no-trunc --format '{{.ID}} {{.Names}}' 2>/dev/null)
  for d in "$root"/*.service "$root"/docker-*.scope; do
    [ -r "$d/memory.current" ] || continue
    name=${d##*/}
    case "$name" in
      docker-*.scope) name=$(printf '%s\n' "$ids" | awk -v id="${name#docker-}" '$1 ".scope" == id { print $2 }'); [ -n "$name" ] || continue ;;
      *) [ "$(cat "$d/memory.current")" -ge $((CGROUP_MIN_MB * 1048576)) ] || continue; name=${name%.service} ;;
    esac
    case "$name" in
      *couch*) svc=couchdb ;; *rabbit*) svc=rabbitmq ;; *freeswitch*) svc=freeswitch ;;
      *kazoo_apps*|*kazoo-applications*|*ecallmgr*) svc=kazoo ;; *valkey*|*redis*) svc=valkey ;;
      *kamailio*) svc=kamailio ;; *haproxy*) svc=haproxy ;; systemd-journald|rsyslog) svc=logging ;;
      fonouc*|gorec|goportal*) svc=fonouc-apps ;; docker|containerd|node-exporter|*node_exporter*) svc=platform ;; *) svc=other ;;
    esac
    printf '%s %s %s\n' "$d" "$(esc "$name")" "$svc"
  done | while read -r d name svc; do
    l="cgroup=\"$name\",service=\"$svc\""
    awk -v l="$l" -v d="$d" '
      FILENAME ~ /memory.current$/ { print "M kazoo_cgroup_memory_bytes{" l ",type=\"total\"} " $1 }
      FILENAME ~ /memory.peak$/    { print "M kazoo_cgroup_memory_peak_bytes{" l "} " $1 }
      FILENAME ~ /memory.max$/     { print "M kazoo_cgroup_memory_max_bytes{" l "} " ($1 == "max" ? 0 : $1) }
      FILENAME ~ /memory.high$/    { print "M kazoo_cgroup_memory_high_bytes{" l "} " ($1 == "max" ? 0 : $1) }
      FILENAME ~ /memory.swap.current$/ { print "M kazoo_cgroup_swap_bytes{" l "} " $1 }
      FILENAME ~ /memory.stat$/ && $1 ~ /^(anon|file|kernel|shmem|sock|active_file|inactive_file)$/ { print "M kazoo_cgroup_memory_bytes{" l ",type=\"" $1 "\"} " $2 }
      FILENAME ~ /memory.events$/  { print "C kazoo_cgroup_memory_events_total{" l ",event=\"" $1 "\"} " $2 }
      FILENAME ~ /pressure$/ {
        r = FILENAME; sub(/.*\//, "", r); sub(/\.pressure$/, "", r)
        for (i = 2; i <= NF; i++) if ($i ~ /^total=/) { split($i, t, "="); printf "P kazoo_cgroup_pressure_seconds_total{%s,resource=\"%s\",kind=\"%s\"} %.6f\n", l, r, $1, t[2] / 1e6 }
      }
      FILENAME ~ /cpu.stat$/ && $1 == "usage_usec"     { printf "U kazoo_cgroup_cpu_seconds_total{%s} %.6f\n", l, $2 / 1e6 }
      FILENAME ~ /cpu.stat$/ && $1 == "throttled_usec" { printf "T kazoo_cgroup_cpu_throttled_seconds_total{%s} %.6f\n", l, $2 / 1e6 }
      FILENAME ~ /io.stat$/ { for (i = 2; i <= NF; i++) { split($i, kv, "="); if (kv[1] == "rbytes") rb += kv[2]; if (kv[1] == "wbytes") wb += kv[2] } }
      FILENAME ~ /pids.current$/ { print "N kazoo_cgroup_pids{" l "} " $1 }
      END { print "I kazoo_cgroup_io_bytes_total{" l ",direction=\"read\"} " rb + 0; print "I kazoo_cgroup_io_bytes_total{" l ",direction=\"write\"} " wb + 0 }
    ' $(for f in memory.current memory.peak memory.max memory.high memory.swap.current memory.stat memory.events memory.pressure cpu.pressure io.pressure cpu.stat io.stat pids.current; do [ -r "$d/$f" ] && printf "%s " "$d/$f"; done) 2>/dev/null
  done | LC_ALL=C sort -s -k2,2 | awk '
    # Group samples by family (the sort keeps each family contiguous) and add HELP/TYPE once.
    { split($2, n, "{"); if (n[1] != last) { last = n[1]
        t = ($1 == "C" || $1 == "P" || $1 == "U" || $1 == "T" || $1 == "I") ? "counter" : "gauge"
        print "# TYPE " n[1] " " t }
      print $2, $3 }'
}

# Bytes appended to a log since the previous run, preceded by a "#EL <seconds>" line
# (time since the previous run, to turn counts into per-minute rates). The offset is kept
# in STATE_DIR; the first run only records the end of the file; rotation restarts at 0;
# reads are capped at LOG_MAX_BYTES. With --stdout nothing is written anywhere: the last
# LOG_SAMPLE_BYTES are analysed as if they were one minute of log.
log_new() {
  local file=$1 st="$STATE_DIR/$2.offset" size off ts now
  size=$(stat -c %s "$file" 2>/dev/null) || return
  now=$(date +%s)
  if [ "$STDOUT" = 1 ]; then echo "#EL 60"; tail -c "$LOG_SAMPLE_BYTES" "$file"; return; fi
  read -r off ts 2>/dev/null < "$st"
  mkdir -p "$STATE_DIR" && echo "$size $now" > "$st"
  case "$off$ts" in ''|*[!0-9]*) return ;; esac
  [ "$size" -lt "$off" ] && off=0
  [ $((size - off)) -gt "$LOG_MAX_BYTES" ] && off=$((size - LOG_MAX_BYTES))
  echo "#EL $((now - ts > 0 ? now - ts : 60))"
  tail -c +$((off + 1)) "$file" | head -c $((size - off))
}

# Shared awk helpers. sig() turns a message into a short signature (ids, numbers, IPs -> #,
# Erlang cookie redacted). top() prints the most frequent signatures as "E <sample>" lines;
# level counts are printed as "L <sample>" lines. logs_metrics() groups both families.
LOG_AWK='
NR == 1 && $1 == "#EL" { rate = 60 / $2; next }
function sig(s) {
  gsub(/cookie matches '"'"'[^'"'"']*'"'"'/, "cookie matches <redacted>", s)
  gsub(/<[0-9.]+>/, "<pid>", s)
  gsub(/\{[^}]*\}?/, "{...}", s)
  gsub(/[0-9a-fA-F]*[0-9][0-9a-fA-F]*/, "#", s)
  gsub(/[\\"]/, "", s); gsub(/[^ -~]/, "", s); gsub(/  +/, " ", s); sub(/^ /, "", s)
  return substr(s, 1, 110)
}
function top(svc, m,   k, n, i, j, key, cnt, t) {
  n = 0; for (k in errs) { n++; key[n] = k; cnt[n] = errs[k] }
  for (i = 1; i <= n; i++) for (j = i + 1; j <= n; j++) if (cnt[j] > cnt[i]) { t = cnt[i]; cnt[i] = cnt[j]; cnt[j] = t; t = key[i]; key[i] = key[j]; key[j] = t }
  for (i = 1; i <= n && i <= m; i++) printf "E kazoo_log_top_errors_per_min{service=\"%s\",signature=\"%s\"} %.2f\n", svc, key[i], cnt[i] * rate
}
function levels(svc,   k) { for (k in lv) if (k ~ /^[a-z]+$/) printf "L kazoo_log_lines_per_min{service=\"%s\",level=\"%s\"} %.2f\n", svc, k, lv[k] * rate }
'

# CouchDB access log: per-database requests, latency, time spent and errors, plus log
# levels and top error/warning messages. Our own requests (to :5984) are excluded.
couch_log_metrics() {
  [ -r "$COUCH_LOG" ] || return
  log_new "$COUCH_LOG" couchdb | awk -v topn="$COUCH_TOP_DBS" "$LOG_AWK"'
    function dec(p) { gsub(/%2[Ff]/, "/", p); return p }
    { l = $1; gsub(/[][]/, "", l); lv[l]++ }
    $1 == "[notice]" && $9 ~ /^(GET|PUT|POST|DELETE|HEAD|COPY)$/ && $12 == "ok" && $6 !~ /:5984$/ {
      p = $10; sub(/\?.*/, "", p); split(p, seg, "/"); db = dec(seg[2])
      if (db == "") db = "_root"; else if (db ~ /^_/) db = "_system"
      n[db]++; ms[db] += $13; if ($13 + 0 > mx[db] + 0) mx[db] = $13
      if ($11 == "404") nf[db]++; else if ($11 ~ /^4/) e4[db]++; else if ($11 ~ /^5/) e5[db]++
      meth[$9]++; code[$11]++; tot++; totms += $13
      next
    }
    $1 ~ /^\[(error|warning|critical|alert|emergency)\]$/ { m = ""; for (i = 6; i <= NF; i++) m = m " " $i; errs[sig(m)]++ }
    END {
      if (!rate) exit
      print "# HELP kazoo_couchdb_requests_per_min HTTP requests per minute (access log)"
      print "# TYPE kazoo_couchdb_requests_per_min gauge"
      printf "kazoo_couchdb_requests_per_min %.2f\n", tot * rate
      print "# TYPE kazoo_couchdb_request_ms_avg gauge"
      printf "kazoo_couchdb_request_ms_avg %.2f\n", tot ? totms / tot : 0
      print "# TYPE kazoo_couchdb_requests_by_method_per_min gauge"
      for (k in meth) printf "kazoo_couchdb_requests_by_method_per_min{method=\"%s\"} %.2f\n", k, meth[k] * rate
      print "# TYPE kazoo_couchdb_responses_by_code_per_min gauge"
      for (k in code) printf "kazoo_couchdb_responses_by_code_per_min{code=\"%s\"} %.2f\n", k, code[k] * rate
      # Busiest databases by request count, plus any database returning 5xx.
      c = 0; for (k in n) { c++; dk[c] = k; dn[c] = n[k] }
      for (i = 1; i <= c; i++) for (j = i + 1; j <= c; j++) if (dn[j] > dn[i]) { t = dn[i]; dn[i] = dn[j]; dn[j] = t; t = dk[i]; dk[i] = dk[j]; dk[j] = t }
      for (i = 1; i <= c; i++) if (i <= topn || e5[dk[i]] > 0) keep[dk[i]] = 1
      print "# HELP kazoo_couchdb_db_requests_per_min Requests per minute per database"
      print "# TYPE kazoo_couchdb_db_requests_per_min gauge"
      for (k in keep) printf "kazoo_couchdb_db_requests_per_min{db=\"%s\"} %.2f\n", k, n[k] * rate
      print "# HELP kazoo_couchdb_db_time_ms_per_min Request time spent per database in ms per minute: its share of CouchDB work"
      print "# TYPE kazoo_couchdb_db_time_ms_per_min gauge"
      for (k in keep) printf "kazoo_couchdb_db_time_ms_per_min{db=\"%s\"} %.2f\n", k, ms[k] * rate
      print "# TYPE kazoo_couchdb_db_request_ms_avg gauge"
      for (k in keep) printf "kazoo_couchdb_db_request_ms_avg{db=\"%s\"} %.2f\n", k, ms[k] / n[k]
      print "# TYPE kazoo_couchdb_db_request_ms_max gauge"
      for (k in keep) printf "kazoo_couchdb_db_request_ms_max{db=\"%s\"} %d\n", k, mx[k]
      print "# TYPE kazoo_couchdb_db_errors_per_min gauge"
      for (k in keep) {
        printf "kazoo_couchdb_db_errors_per_min{db=\"%s\",class=\"404\"} %.2f\n", k, nf[k] * rate
        printf "kazoo_couchdb_db_errors_per_min{db=\"%s\",class=\"4xx\"} %.2f\n", k, e4[k] * rate
        printf "kazoo_couchdb_db_errors_per_min{db=\"%s\",class=\"5xx\"} %.2f\n", k, e5[k] * rate
      }
      levels("couchdb"); top("couchdb", 10)
    }'
}

# Kazoo crossbar API ("GET request fulfilled in 66 ms") and failure lines from kazoo.log
# (syslog, no level field): signature = module + message.
kazoo_log_metrics() {
  [ -r "$KAZOO_LOG" ] || return
  log_new "$KAZOO_LOG" kazoo | awk "$LOG_AWK"'
    / request fulfilled in [0-9]+ ms/ {
      for (i = 2; i <= NF; i++) if ($i == "request" && $(i + 1) == "fulfilled") { m = $(i - 1); t = $(i + 3) }
      n[m]++; s[m] += t; if (t + 0 > mx[m] + 0) mx[m] = t
    }
    /2600hz/ { if ($0 ~ /(error|crash|exception|badmatch|badarg|undef|function_clause|timeout|failed|unable to|exit with reason)/) {
        lv["error"]++; m = $0; sub(/^[^|]*\|[^|]*\|/, "", m); sub(/:[0-9]+\(<[0-9.]+>\)/, "", m); errs[sig(m)]++
      } else lv["info"]++ }
    END {
      if (!rate) exit
      print "# HELP kazoo_api_requests_per_min Kazoo API requests per minute by method"
      print "# TYPE kazoo_api_requests_per_min gauge"
      for (k in n) printf "kazoo_api_requests_per_min{method=\"%s\"} %.2f\n", k, n[k] * rate
      print "# TYPE kazoo_api_request_ms_avg gauge"
      for (k in n) printf "kazoo_api_request_ms_avg{method=\"%s\"} %.2f\n", k, s[k] / n[k]
      print "# TYPE kazoo_api_request_ms_max gauge"
      for (k in n) printf "kazoo_api_request_ms_max{method=\"%s\"} %d\n", k, mx[k]
      levels("kazoo"); top("kazoo", 10)
    }'
}

rabbit_log_metrics() {
  [ -r "$RABBIT_LOG" ] || return
  log_new "$RABBIT_LOG" rabbitmq | awk "$LOG_AWK"'
    match($0, / \[[a-z]+\] /) { l = substr($0, RSTART + 2, RLENGTH - 4); lv[l]++
      if (l ~ /^(error|warning|critical)$/) { m = substr($0, RSTART + RLENGTH); errs[sig(m)]++ } }
    END { if (!rate) exit; levels("rabbitmq"); top("rabbitmq", 10) }'
}

freeswitch_log_metrics() {
  [ -r "$FS_ERROR_LOG" ] || return
  log_new "$FS_ERROR_LOG" freeswitch | awk "$LOG_AWK"'
    match($0, /\[[A-Z]+\] /) { l = tolower(substr($0, RSTART + 1, RLENGTH - 3)); lv[l]++; errs[sig(substr($0, RSTART + RLENGTH))]++ }
    END { if (!rate) exit; levels("freeswitch"); top("freeswitch", 10) }'
}

# Runs the log analysers; level counts and top errors from all of them form two shared
# families, so they are collected ("L"/"E" lines) and printed together.
logs_metrics() {
  [ "$LOG_METRICS" = 1 ] || return
  { couch_log_metrics; kazoo_log_metrics; rabbit_log_metrics; freeswitch_log_metrics; } | awk '
    $1 == "L" { sub(/^L /, ""); L[++nl] = $0; next }
    $1 == "E" { sub(/^E /, ""); E[++ne] = $0; next }
    { print }
    END {
      print "# HELP kazoo_log_lines_per_min Log lines per minute by service and level"
      print "# TYPE kazoo_log_lines_per_min gauge"
      for (i = 1; i <= nl; i++) print L[i]
      print "# HELP kazoo_log_top_errors_per_min Most frequent error/warning messages (normalised) per minute"
      print "# TYPE kazoo_log_top_errors_per_min gauge"
      for (i = 1; i <= ne; i++) print E[i]
    }'
}

gen() {
  local start=$SECONDS
  docker_metrics; cgroup_metrics; couch_metrics; rabbit_metrics; freeswitch_metrics; valkey_metrics; valkey_sentinel_metrics; logs_metrics
  meta kazoo_service_metrics_duration_seconds gauge "Time taken by the collector run"
  echo "kazoo_service_metrics_duration_seconds $((SECONDS - start))"
}

mode=gen; out=$OUT_DIR/kazoo_services.prom
if [ "$1" = "--couch-sizes" ]; then mode=couch_size_metrics; out=$OUT_DIR/kazoo_couchdb_sizes.prom; shift; fi
if [ "$1" = "--stdout" ]; then STDOUT=1; $mode; exit; fi
mkdir -p "$OUT_DIR"
# Skip the run if the previous one is still going (e.g. docker hung).
exec 9>"/run/lock/$(basename "$out").lock"
flock -n 9 || exit 0
$mode > "$out.tmp.$$" && mv "$out.tmp.$$" "$out"
