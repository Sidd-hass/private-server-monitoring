#!/bin/bash
# Writes the TLS certificate expiry of a site as Prometheus metrics (node_exporter textfile
# collector), for sites the central blackbox exporter cannot reach. Used on Tahseel uc01: its
# URL only resolves inside the SonicWall, and its node_exporter already reaches Prometheus over
# the existing reverse SSH tunnel (172.17.0.1:19100), so no extra port is needed.
# Prometheus turns these into probe_ssl_* series (recording rules in alert.rules.yml), so the
# SSL dashboard and alerts treat the site like the blackbox-checked ones.
#
# Install on uc01:
#   install -m 0755 ssl_cert_metrics.sh /usr/local/sbin/ssl_cert_metrics.sh
#   echo '*/15 * * * * root /usr/local/sbin/ssl_cert_metrics.sh >/dev/null 2>&1' > /etc/cron.d/ssl_cert_metrics
# OUT_DIR must match node_exporter's --collector.textfile.directory.
# Test without writing anything: ssl_cert_metrics.sh --stdout
DOMAIN=${DOMAIN:-ira.ucprem.voicemeetme.com}
CONNECT=${CONNECT:-192.168.1.178:443}   # where to connect; the cert is verified against DOMAIN
OUT_DIR=${OUT_DIR:-/var/lib/node_exporter/textfile}
OUT=$OUT_DIR/ssl_cert.prom
# Label values must not break the exposition format.
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
# Empty sample values make node_exporter reject the whole file.
num() { case "$1" in ''|*[!0-9]*) echo 0 ;; *) echo "$1" ;; esac; }

gen() {
  local out pem verify ok=0 expiry="" issuer="" subject="" d
  out=$(timeout 20 openssl s_client -connect "$CONNECT" -servername "$DOMAIN" \
        -verify_hostname "$DOMAIN" </dev/null 2>/dev/null)
  pem=$(printf '%s\n' "$out" | openssl x509 2>/dev/null)
  if [ -n "$pem" ]; then
    expiry=$(date -d "$(printf '%s\n' "$pem" | openssl x509 -noout -enddate | cut -d= -f2)" +%s 2>/dev/null)
    issuer=$(printf '%s\n' "$pem" | openssl x509 -noout -issuer -nameopt RFC2253 | sed 's/^issuer=//')
    subject=$(printf '%s\n' "$pem" | openssl x509 -noout -subject -nameopt RFC2253 | sed 's/^subject=//')
    # Same pass/fail rule as blackbox: valid chain, correct name, not expired.
    verify=$(printf '%s\n' "$out" | sed -n 's/^ *Verify return code: \([0-9]*\).*/\1/p' | head -1)
    [ "$verify" = "0" ] && ok=1
  fi
  d=$(esc "$DOMAIN")

  echo "# HELP ssl_cert_check_success 1 if the TLS handshake and certificate verification succeeded"
  echo "# TYPE ssl_cert_check_success gauge"
  echo "ssl_cert_check_success{domain=\"$d\"} $ok"
  echo "# HELP ssl_cert_not_after_timestamp_seconds Unix time the certificate expires (0 if none was received)"
  echo "# TYPE ssl_cert_not_after_timestamp_seconds gauge"
  echo "ssl_cert_not_after_timestamp_seconds{domain=\"$d\"} $(num "$expiry")"
  echo "# HELP ssl_cert_info Certificate issuer and subject as labels"
  echo "# TYPE ssl_cert_info gauge"
  [ -n "$pem" ] && echo "ssl_cert_info{domain=\"$d\",issuer=\"$(esc "$issuer")\",subject=\"$(esc "$subject")\"} 1"
  echo "# HELP ssl_cert_last_check_timestamp_seconds Unix time of the last check (detects a stopped cron job)"
  echo "# TYPE ssl_cert_last_check_timestamp_seconds gauge"
  echo "ssl_cert_last_check_timestamp_seconds{domain=\"$d\"} $(date +%s)"
}

if [ "$1" = "--stdout" ]; then gen; exit; fi
mkdir -p "$OUT_DIR"
gen > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"
