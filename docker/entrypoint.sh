#!/usr/bin/env bash
# Runs the NUT UPS driver + upsd + the nut2mqtt bridge in one container.
# Exits (so the container restarts) if upsd or the bridge stops.
#
# RUN_NUT_SERVER=0 skips the driver/upsd and runs only the bridge against an
# existing NUT server (set ups.name to "upsname@host" in config.yaml).
set -uo pipefail

RUN_NUT_SERVER="${RUN_NUT_SERVER:-1}"
UPSD_PID=""
BRIDGE_PID=""

cleanup() {
  echo "[entrypoint] shutting down..."
  [[ -n "$BRIDGE_PID" ]] && kill -TERM "$BRIDGE_PID" 2>/dev/null || true
  [[ -n "$UPSD_PID" ]] && kill -TERM "$UPSD_PID" 2>/dev/null || true
  [[ "$RUN_NUT_SERVER" == "1" ]] && upsdrvctl -u root stop 2>/dev/null || true
  exit 0
}
trap cleanup TERM INT

if [[ ! -f /data/config.yaml ]]; then
  echo "[entrypoint] ERROR: /data/config.yaml not found — mount your bridge config there." >&2
  exit 1
fi

if [[ "$RUN_NUT_SERVER" == "1" ]]; then
  if [[ ! -f /etc/nut/ups.conf ]]; then
    echo "[entrypoint] ERROR: /etc/nut/ups.conf not found — mount your NUT config at /etc/nut." >&2
    exit 1
  fi

  # Fresh runtime dir — stale *.pid files survive a container restart and make
  # upsd / upsdrvctl think a previous instance is still running.
  mkdir -p /run/nut
  rm -f /run/nut/*.pid
  chown -R root:nut /run/nut 2>/dev/null || true

  # First [section] in ups.conf is the UPS name (no awk in slim base images).
  UPS_NAME="$(sed -n 's/^[[:space:]]*\[\([^]]*\)\].*/\1/p' /etc/nut/ups.conf | head -n1)"
  [[ -n "$UPS_NAME" ]] || UPS_NAME="ups"

  # Run as root: NUT is built --with-user=nut, but a passed-through USB node is
  # typically root-owned, so a dropped-privilege driver can't open it.
  echo "[entrypoint] starting UPS driver for '$UPS_NAME'..."
  if ! upsdrvctl -u root start; then
    echo "[entrypoint] driver failed to start — check USB passthrough and nut/ups.conf" >&2
    exit 1
  fi

  # Foreground upsd as a managed child so we can track it directly rather than
  # relying on a pid file (which -F upsd does not write).
  echo "[entrypoint] starting upsd..."
  upsd -F -u root &
  UPSD_PID=$!

  ready=0
  for _ in $(seq 1 20); do
    if ! kill -0 "$UPSD_PID" 2>/dev/null; then
      echo "[entrypoint] upsd exited during startup" >&2
      exit 1
    fi
    if upsc "$UPS_NAME" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1
  done
  if [[ "$ready" == "1" ]]; then
    echo "[entrypoint] upsd is serving '$UPS_NAME'"
  else
    echo "[entrypoint] WARNING: upsd not answering for '$UPS_NAME' yet, continuing" >&2
  fi
fi

echo "[entrypoint] starting nut2mqtt..."
cd /data
python3 /app/nut2mqtt.py &
BRIDGE_PID=$!

# Exit as soon as either managed process stops; the container restart brings
# the whole stack back cleanly.
wait -n
echo "[entrypoint] a managed process exited — restarting" >&2
cleanup
