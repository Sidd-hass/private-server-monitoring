#!/bin/sh
# Prints the ACCOUNTS value for .env: accounts with call records this month whose CDR view is
# already (nearly) indexed. Read-only: update=false never starts an index build.
# Usage on the node:  sh pick_accounts.sh            (MAX_LAG: changes the view may be behind)
COUCH=${COUCH:-http://127.0.0.1:5984}
MAX_LAG=${MAX_LAG:-5000}
month=$(date -u +%Y%m)
ids=""
for id in $(curl -s -m 10 "$COUCH/accounts/_all_docs" | grep -o '"id":"[0-9a-f]\{32\}"' | cut -d'"' -f4); do
  name=$(curl -s -m 10 "$COUCH/accounts/$id" | grep -o '"name":"[^"]*"' | head -1 | cut -d'"' -f4)
  [ "$name" = "admin" ] && continue
  db="account%2F$(echo "$id" | cut -c1-2)%2F$(echo "$id" | cut -c3-4)%2F$(echo "$id" | cut -c5-)-$month"
  rows=$(curl -s -m 20 "$COUCH/$db/_design/cdrs/_view/crossbar_listing?stable=true&update=false&limit=0" | grep -o '"total_rows":[0-9]*' | cut -d: -f2)
  [ -z "$rows" ] || [ "$rows" -eq 0 ] && continue
  vseq=$(curl -s -m 10 "$COUCH/$db/_design/cdrs/_info" | grep -o '"update_seq":[0-9]*' | head -1 | cut -d: -f2)
  dseq=$(curl -s -m 10 "$COUCH/$db" | grep -o '"update_seq":"[0-9]*' | cut -d'"' -f4)
  lag=$(( ${dseq:-0} - ${vseq:-0} ))
  if [ "$lag" -gt "$MAX_LAG" ]; then echo "skip $id ($name): view $lag changes behind" >&2; continue; fi
  echo "use  $id ($name): $rows CDRs this month" >&2
  ids="${ids:+$ids,}$id"
done
echo "$ids"
