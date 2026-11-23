FROM python:3.12-alpine

# Standard library only: no pip packages.
COPY exporter.py /app/exporter.py
WORKDIR /app
USER nobody

EXPOSE 9480
ENTRYPOINT ["python3", "-u", "/app/exporter.py"]
