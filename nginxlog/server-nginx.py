import glob
import os
import re
import time

import requests
from prometheus_client import start_http_server, Gauge, Counter


SERVER_NAME = os.getenv(
    "SERVER_NAME",
    "nginx-proxy-manager"
)

STUB_STATUS_URL = os.getenv(
    "STUB_STATUS_URL",
    "http://nginx-proxy-manager:8080/nginx_status"
)

LOG_DIRECTORY = os.getenv(
    "LOG_DIRECTORY",
    "/var/log/nginx"
)

SCRAPE_INTERVAL = float(
    os.getenv("SCRAPE_INTERVAL", "2")
)


# ============================================================
# NGINX SERVER-WIDE METRICS
# ============================================================

NGINX_UP = Gauge(
    "custom_nginx_up",
    "Nginx service availability (1 = UP, 0 = DOWN)",
    ["server_name"]
)

ACTIVE_CONNS = Gauge(
    "custom_nginx_active_connections",
    "Active connections count",
    ["server_name"]
)

READING_CONNS = Gauge(
    "custom_nginx_connections_reading",
    "Connections reading state",
    ["server_name"]
)

WRITING_CONNS = Gauge(
    "custom_nginx_connections_writing",
    "Connections writing state",
    ["server_name"]
)

WAITING_CONNS = Gauge(
    "custom_nginx_connections_waiting",
    "Connections waiting state",
    ["server_name"]
)

HTTP_REQUESTS_TOTAL = Counter(
    "custom_nginx_http_requests_total",
    "HTTP response status codes parsed from Nginx Proxy Manager logs",
    ["server_name", "status_code"]
)


# ============================================================
# PER-WEBSITE METRICS
# ============================================================

SITE_REQUESTS_TOTAL = Counter(
    "custom_nginx_site_requests_total",
    "HTTP requests received by each Nginx Proxy Manager website",
    [
        "server_name",
        "host",
        "status_code",
        "method",
        "upstream_status"
    ]
)

SITE_RESPONSE_BYTES_TOTAL = Counter(
    "custom_nginx_site_response_bytes_total",
    "HTTP response bytes sent by each Nginx Proxy Manager website",
    [
        "server_name",
        "host"
    ]
)


# ============================================================
# STUB STATUS
# ============================================================

def scrape_stub_status():

    try:

        response = requests.get(
            STUB_STATUS_URL,
            timeout=3
        )

        if response.status_code != 200:

            NGINX_UP.labels(
                server_name=SERVER_NAME
            ).set(0)

            return

        NGINX_UP.labels(
            server_name=SERVER_NAME
        ).set(1)

        text = response.text

        active_match = re.search(
            r"Active connections:\s+(\d+)",
            text
        )

        states_match = re.search(
            r"Reading:\s+(\d+)\s+Writing:\s+(\d+)\s+Waiting:\s+(\d+)",
            text
        )

        if active_match:

            ACTIVE_CONNS.labels(
                server_name=SERVER_NAME
            ).set(
                int(active_match.group(1))
            )

        if states_match:

            READING_CONNS.labels(
                server_name=SERVER_NAME
            ).set(
                int(states_match.group(1))
            )

            WRITING_CONNS.labels(
                server_name=SERVER_NAME
            ).set(
                int(states_match.group(2))
            )

            WAITING_CONNS.labels(
                server_name=SERVER_NAME
            ).set(
                int(states_match.group(3))
            )

    except Exception as exc:

        print(
            f"stub_status error: {exc}"
        )

        NGINX_UP.labels(
            server_name=SERVER_NAME
        ).set(0)


# ============================================================
# LOG PROCESSING
# ============================================================

def process_log_file(path, state):

    try:

        stat = os.stat(path)

        inode = stat.st_ino
        size = stat.st_size

        # First time seeing this file
        if path not in state:

            file_handle = open(
                path,
                "r",
                encoding="utf-8",
                errors="replace"
            )

            # Start at end of current log.
            # We only count new requests after exporter starts.
            file_handle.seek(0, 2)

            state[path] = {
                "handle": file_handle,
                "inode": inode
            }

            return

        entry = state[path]

        # Detect log rotation
        if entry["inode"] != inode:

            try:
                entry["handle"].close()

            except Exception:
                pass

            file_handle = open(
                path,
                "r",
                encoding="utf-8",
                errors="replace"
            )

            state[path] = {
                "handle": file_handle,
                "inode": inode
            }

            return

        file_handle = entry["handle"]

        current_position = file_handle.tell()

        # Handle truncation
        if size < current_position:

            file_handle.seek(0)

        for line in file_handle:

            parse_log_line(line)

    except FileNotFoundError:

        pass

    except Exception as exc:

        print(
            f"log processing error [{path}]: {exc}"
        )


# ============================================================
# LOG PARSER
# ============================================================

def parse_log_line(line):

    """
    Expected NPM access log format:

    [time]
    upstream_cache_status
    upstream_status
    status
    request_method
    scheme
    host
    request_uri
    [Client ...]
    [Length ...]
    [Gzip ...]
    [Sent-to ...]
    ...
    """

    match = re.search(
        r'''
        \]\s+
        (\S+)\s+                         # upstream cache status
        (\S+)\s+                         # upstream status
        (\d{3})\s+-\s+                   # final HTTP status
        (\S+)\s+                         # HTTP method
        (\S+)\s+                         # scheme
        (\S+)\s+                         # host
        "([^"]*)"\s+                     # request URI
        \[Client\s+([^\]]*)\]\s+
        \[Length\s+([0-9]+)\]\s+
        \[Gzip\s+([^\]]*)\]\s+
        \[Sent-to\s+([^\]]*)\]
        ''',
        line,
        re.VERBOSE
    )

    if not match:

        return

    (
        upstream_cache_status,
        upstream_status,
        status_code,
        method,
        scheme,
        host,
        request_uri,
        client,
        response_bytes,
        gzip_ratio,
        sent_to
    ) = match.groups()

    # Normalize values

    host = host.strip()

    method = method.upper().strip()

    if not host:

        return

    if upstream_status == "-":

        upstream_status = "none"

    response_bytes = int(
        response_bytes
    )

    # ========================================================
    # EXISTING SERVER-WIDE COUNTER
    # ========================================================

    HTTP_REQUESTS_TOTAL.labels(
        server_name=SERVER_NAME,
        status_code=status_code
    ).inc()


    # ========================================================
    # PER-WEBSITE REQUEST COUNTER
    # ========================================================

    SITE_REQUESTS_TOTAL.labels(
        server_name=SERVER_NAME,
        host=host,
        status_code=status_code,
        method=method,
        upstream_status=upstream_status
    ).inc()


    # ========================================================
    # PER-WEBSITE BANDWIDTH
    # ========================================================

    SITE_RESPONSE_BYTES_TOTAL.labels(
        server_name=SERVER_NAME,
        host=host
    ).inc(
        response_bytes
    )


# ============================================================
# PROCESS ALL NPM PROXY LOGS
# ============================================================

def process_all_logs(state):

    pattern = os.path.join(
        LOG_DIRECTORY,
        "proxy-host-*_access.log"
    )

    files = glob.glob(pattern)

    for path in files:

        process_log_file(
            path,
            state
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print(
        f"Starting custom Nginx exporter "
        f"for {SERVER_NAME}"
    )

    print(
        f"Stub status URL: {STUB_STATUS_URL}"
    )

    print(
        f"Log directory: {LOG_DIRECTORY}"
    )

    start_http_server(8000)

    file_state = {}

    while True:

        scrape_stub_status()

        process_all_logs(
            file_state
        )

        time.sleep(
            SCRAPE_INTERVAL
        )