FROM python:3.12-slim

RUN pip install --no-cache-dir "aiohttp>=3.9,<4"

COPY gateway.py index.html /app/
WORKDIR /app

ENTRYPOINT ["python3", "-u", "/app/gateway.py"]
