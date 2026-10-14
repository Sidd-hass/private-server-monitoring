# RabbitMQ scrape targets

Add one entry per Kazoo node to `rabbitmq.json` (Prometheus reloads it within a minute):

```json
[
  {
    "targets": ["151.243.12.57:15692"],
    "labels": { "environment": "production", "cluster": "ucpin", "site": "ucpin", "server_name": "ucpin11" }
  }
]
```

On each node first enable the plugin and open 15692 to the Prometheus host only:

    rabbitmq-plugins enable rabbitmq_prometheus
    curl -s localhost:15692/metrics | grep rabbitmq_queue_messages
