#!/bin/sh
set -eu

textfile_dir=${NODE_TEXTFILE_DIRECTORY:-/var/lib/node_exporter/textfile_collector}
metric_file="$textfile_dir/directory_usage.prom"

install -d -m 0755 "$textfile_dir"
tmp_file=$(mktemp "$textfile_dir/.directory_usage.XXXXXX")
trap 'rm -f "$tmp_file"' EXIT HUP INT TERM

du_output=$(du -x -B1 --max-depth=1 \
  --exclude=/proc --exclude=/sys --exclude=/dev --exclude=/run \
  --exclude=/lost+found / 2>/dev/null)

{
  printf '# HELP node_directory_size_bytes Disk space used by a top-level directory on the root filesystem.\n'
  printf '# TYPE node_directory_size_bytes gauge\n'
  printf '%s\n' "$du_output" | while IFS="$(printf '\t')" read -r bytes directory; do
    [ -n "$directory" ] || continue
    [ "$directory" = '/' ] && continue
    escaped_directory=$(printf '%s' "$directory" | sed 's/\\/\\\\/g; s/"/\\"/g')
    printf 'node_directory_size_bytes{directory="%s"} %s\n' "$escaped_directory" "$bytes"
  done
} > "$tmp_file"

chmod 0644 "$tmp_file"
mv -f "$tmp_file" "$metric_file"
trap - EXIT HUP INT TERM
