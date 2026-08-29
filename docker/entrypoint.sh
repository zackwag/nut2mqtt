#!/usr/bin/env bash
# Starts the NUT UPS driver + upsd, then runs the nut2mqtt bridge, all in one
# container. If any of them dies, the script exits so the container restarts.
#
# Set RUN_NUT_SERVER=0 to skip the driver/upsd and run only the bridge against
# an existing NUT server (point ups.name at "upsname@host" in config.yaml).
set -euo pipefail

RUN_NUT_SERVER="${RUN_NUT_SERVER:-1}"
STATE_DIR=/run/nut

cleanup() {
  echo "[entrypoint] stopping..."
  [[ -n "${BRIDGE_PID:-}" ]] && kill -TERM "$BRIDGE_PID" 2>/dev/null || true
  if [[ "$RUN_NUT_SERVER" == "1" ]]; then
    upsd -c stop 2>/dev/null || true
    upsdrvctl stop 2>/dev/null || true
  fi
  wait 2>/dev/null || true
  exit 0
}
trap cleanup TERM INT

upsd_alive() {
  local pf
  for pf in /run/nut/upsd.pid /var/run/nut/upsd.pid; do
    [[ -f "$pf" ]] && kill -0 "$(cat "$pf" 2>/dev/null)" 2>/dev/null && return 0
  done
  return 1
}

if [[ ! -f /data/config.yaml ]]; then
  echo "[entrypoint] ERROR: /data/config.yaml not found — mount your bridge config there." >&2
  exit 1
fi

if [[ "$RUN_NUT_SERVER" == "1" ]]; then
  if [[ ! -f /etc/nut/ups.conf ]]; then
    echo "[entrypoint] ERROR: /etc/nut/ups.conf not found — mount your NUT config at /etc/nut." >&2
    exit 1
  fi

  mkdir -p "$STATE_DIR"
  chown -R root:nut "$STATE_DIR" 2>/dev/null || true

  # Run driver and upsd as root so USB access and the driver socket keep working
  # after a UPS reconnect (standard trade-off for NUT in a container).
  echo "[entrypoint] starting UPS driver(s)..."
  if ! upsdrvctl -u root start; then
    echo "[entrypoint] driver failed to start — check USB passthrough and nut/ups.conf" >&2
    exit 1
  fi

  echo "[entrypoint] starting upsd..."
  upsd -u root

  UPS_NAME="$(awk -F'[][]' '/^[[:space:]]*\[/{gsub(/[[:space:]]/,"",$2); print $2; exit}' /etc/nut/ups.conf 2>/dev/null || true)"
  if [[ -n "$UPS_NAME" ]]; then
    echo "[entrypoint] waiting for upsd to answer for '$UPS_NAME'..."
    for _ in $(seq 1 15); do
      upsc "$UPS_NAME" >/dev/null 2>&1 && break
      sleep 1
    done
  else
    sleep 2
  fi
fi

echo "[entrypoint] starting nut2mqtt..."
cd /data
python3 /app/nut2mqtt.py &
BRIDGE_PID=$!

# Supervise: bail out (container restarts) if the bridge or upsd goes away.
while kill -0 "$BRIDGE_PID" 2>/dev/null; do
  if [[ "$RUN_NUT_SERVER" == "1" ]] && ! upsd_alive; then
    echo "[entrypoint] upsd is gone — exiting for restart" >&2
    cleanup
  fi
  sleep 5
done

echo "[entrypoint] nut2mqtt exited — shutting down" >&2
cleanup
