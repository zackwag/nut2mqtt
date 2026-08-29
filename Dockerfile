FROM python:3.12-slim

# NUT server (upsd) + USB/serial drivers + client tools (upsc/upscmd),
# plus lsusb for debugging USB passthrough.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      nut-server \
      nut-client \
      usbutils \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1

# Bridge code + deps (read-only at runtime).
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY nut2mqtt.py ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Runtime data: config.yaml (mounted) + last_values.json (persisted).
WORKDIR /data

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
