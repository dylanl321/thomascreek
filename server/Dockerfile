# Thomas Creek Hub
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends wireguard-tools iproute2 iptables tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY hub ./hub
RUN pip install --no-cache-dir .

ENV HUB_DATA_DIR=/data \
    HUB_MASTER_KEY_FILE=/run/secrets/hub_master_key \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8443/tcp 51820/udp

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import ssl,urllib.request;c=ssl._create_unverified_context();urllib.request.urlopen('https://127.0.0.1:8443/v1/ping',context=c,timeout=4)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/hub/entrypoint.sh"]
