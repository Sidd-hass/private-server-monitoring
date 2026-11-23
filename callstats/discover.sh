#!/bin/sh
# Read-only check of a Kazoo node before deploying callstats. Only GET requests and listings.
# update=false: never starts a view index build, even on a view that was never indexed.
# Run as root on the node:  sh discover.sh
COUCH=${COUCH:-http://127.0.0.1:5984}
RABBIT=${RABBIT:-http://127.0.0.1:15692}
month=$(date -u +%Y%m)

echo "== host"; hostname; ip -4 -o addr show | awk '{print $2, $4}'
echo "== docker"; docker --version 2>&1; docker compose version 2>&1 | head -1

echo "== couchdb ($COUCH)"
curl -s -m 5 "$COUCH/" || echo "NO ANSWER (CouchDB elsewhere or needs credentials: set COUCH=http://user:pass@host:5984)"
echo
for id in $(curl -s -m 10 "$COUCH/accounts/_all_docs" | grep -o '"id":"[0-9a-f]\{32\}"' | cut -d'"' -f4 | head -20); do
  a=$(echo "$id" | cut -c1-2); b=$(echo "$id" | cut -c3-4); r=$(echo "$id" | cut -c5-)
  db="account%2F$a%2F$b%2F$r-$month"
  docs=$(curl -s -m 10 "$COUCH/$db" | grep -o '"doc_count":[0-9]*' | cut -d: -f2)
  [ -z "$docs" ] && continue
  name=$(curl -s -m 10 "$COUCH/accounts/$id" | grep -o '"name":"[^"]*"' | head -1 | cut -d'"' -f4)
  info=$(curl -s -m 10 "$COUCH/$db/_design/cdrs/_info" | grep -o '"update_seq":[^,]*' | head -1)
  dbseq=$(curl -s -m 10 "$COUCH/$db" | grep -o '"update_seq":"[0-9]*' | cut -d'"' -f4)
  hour=$(curl -s -m 20 "$COUCH/$db/_design/cdrs/_view/crossbar_listing?stable=true&update=false&limit=0" | grep -o '"total_rows":[0-9]*')
  echo "== $id ($name)  $month docs=$docs  view $info (db seq $dbseq)  $hour"
done

echo "== rabbitmq_prometheus ($RABBIT)"
curl -s -m 10 "$RABBIT/metrics" | grep -E '^rabbitmq_(queues|connections|build_info)' | head -5 || true
curl -s -m 5 -o /dev/null -w "metrics HTTP %{http_code}\n" "$RABBIT/metrics"

echo "== firewall: node_exporter rules (copy their style for 9480)"
iptables -L INPUT -n --line-numbers 2>/dev/null | head -8
grep -n 9100 /etc/iptables/rules.v4 2>/dev/null
echo "== timezone"; date; cat /etc/timezone 2>/dev/null
