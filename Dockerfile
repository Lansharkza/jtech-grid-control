FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# python:slim has no zoneinfo database, so TZ would be ignored and the container
# would silently run on UTC — shifting every schedule by the local offset.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies are pinned inline rather than read from requirements.txt, so the
# build only needs the files you can see in the folder. requirements.txt is still
# there for running outside Docker — keep the two in sync if you change either.
RUN pip install --no-cache-dir \
      ocpp==2.1.0 \
      websockets==16.1.1 \
      fastapi==0.139.2 \
      uvicorn==0.51.0 \
      pydantic==2.13.4 \
      cryptography==46.0.6

COPY central_system.py ./
COPY simulator.py ./
COPY static ./static
COPY make-cert.sh check.py entrypoint.sh healthcheck.py ./
# Optional one-time history seed. Imported on first start (with de-duplication)
# then renamed, so it is safe to leave in place. Remove it if you don't want it.
COPY seed-sessions.jso[n] ./

RUN mkdir -p /app/data \
    && useradd --create-home --uid 1000 ocpp \
    && chown -R ocpp:ocpp /app

# Starts as root, fixes ownership of the mounted data folder, then drops to the
# ocpp user. The application itself never runs as root.
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

EXPOSE 9000 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python /app/healthcheck.py

CMD ["python", "central_system.py", "--host", "0.0.0.0", "--ocpp-port", "9000", "--api-port", "8080"]
