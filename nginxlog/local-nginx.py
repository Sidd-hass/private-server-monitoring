import time
import re
import requests
from prometheus_client import start_http_server, Gauge, Counter

# Define Custom Prometheus Metrics
NGINX_UP = Gauge('custom_nginx_up', 'Nginx service availability (1 = UP, 0 = DOWN)')
ACTIVE_CONNS = Gauge('custom_nginx_active_connections', 'Active connections count')
READING_CONNS = Gauge('custom_nginx_connections_reading', 'Connections reading state')
WRITING_CONNS = Gauge('custom_nginx_connections_writing', 'Connections writing state')
WAITING_CONNS = Gauge('custom_nginx_connections_waiting', 'Connections waiting state')
HTTP_REQUESTS_TOTAL = Counter('custom_nginx_http_requests_total', 'HTTP response status codes parsed from log', ['status_code'])

# UPDATED: Changed host.docker.internal to localhost and /stub_status to /nginx_status
# Use host.docker.internal so the container reaches Nginx on the host
STUB_STATUS_URL = "http://host.docker.internal:8080/nginx_status"
LOG_FILE_PATH = "/var/log/nginx/access.log"

def scrape_stub_status():
    try:
        response = requests.get(STUB_STATUS_URL, timeout=3)
        if response.status_code == 200:
            NGINX_UP.set(1)
            lines = response.text.splitlines()
            
            # Parse Active connections
            active_match = re.search(r'Active connections:\s+(\d+)', lines[0])
            if active_match:
                ACTIVE_CONNS.set(int(active_match.group(1)))
                
            # Parse Reading, Writing, Waiting
            states_match = re.search(r'Reading:\s+(\d+)\s+Writing:\s+(\d+)\s+Waiting:\s+(\d+)', lines[3])
            if states_match:
                READING_CONNS.set(int(states_match.group(1)))
                WRITING_CONNS.set(int(states_match.group(2)))
                WAITING_CONNS.set(int(states_match.group(3)))
        else:
            NGINX_UP.set(0)
    except Exception:
        NGINX_UP.set(0)

def tail_access_log(file_handle):
    """Reads new log lines added to access.log and increments counters."""
    lines = file_handle.readlines()
    for line in lines:
        # Match standard HTTP status code (3-digit integer)
        match = re.search(r'"\s+(\d{3})\s+', line)
        if match:
            status = match.group(1)
            HTTP_REQUESTS_TOTAL.labels(status_code=status).inc()

if __name__ == '__main__':
    start_http_server(8000)
    print("Custom Python Nginx Metric Exporter running on port 8000...")
    
    
    try:
        log_file = open(LOG_FILE_PATH, 'r')
        log_file.seek(0, 2)  # Move cursor to end of file
    except FileNotFoundError:
        log_file = None

    while True:
        scrape_stub_status()
        if log_file:
            tail_access_log(log_file)
        time.sleep(2)