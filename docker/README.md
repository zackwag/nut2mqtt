# nut2mqtt

A NUT → MQTT bridge for Home Assistant. This image bundles the UPS driver, the
NUT server (`upsd`), and the bridge in one container. The UPS is expected on
**USB**.

Source & full docs: <https://github.com/zackwag/nut2mqtt>

## Tags

| Tag | Meaning |
| --- | --- |
| `latest` | Newest stable release |
| `X.Y.Z` | Exact release |
| `X.Y` | Latest patch of that minor line |

Images are multi-arch: `linux/amd64` and `linux/arm64`.

## Quick start

1. Create the NUT config directory (`nut/`) with four files —
   `nut.conf`, `ups.conf`, `upsd.conf`, `upsd.users` — see the
   [examples in the repo](https://github.com/zackwag/nut2mqtt/tree/main/nut).
   In `ups.conf`, set the `driver` for your model (`usbhid-ups` for most USB
   units) and name the section `[ups]`.
2. Create `config.yaml` from
   [`config.yaml.example`](https://github.com/zackwag/nut2mqtt/blob/main/config.yaml.example).
   Fill in the `mqtt` block and set `ups.name: "ups"`.

### docker run

```bash
docker run -d --name nut2mqtt --restart unless-stopped \
  --device /dev/bus/usb:/dev/bus/usb \
  -v "$PWD/nut:/etc/nut:ro" \
  -v "$PWD/config.yaml:/data/config.yaml:ro" \
  -v nut2mqtt-data:/data \
  zackwag/nut2mqtt:latest
```

### docker-compose.yml

```yaml
services:
  nut2mqtt:
    image: zackwag/nut2mqtt:latest
    container_name: nut2mqtt
    restart: unless-stopped
    init: true
    devices:
      - /dev/bus/usb:/dev/bus/usb
    volumes:
      - ./nut:/etc/nut:ro
      - ./config.yaml:/data/config.yaml:ro
      - nut2mqtt-data:/data
    # Expose the NUT server to the LAN (needs `LISTEN 0.0.0.0 3493` in
    # nut/upsd.conf):
    # ports:
    #   - "3493:3493"

volumes:
  nut2mqtt-data:
```

Verify:

```bash
docker exec nut2mqtt upsc ups
```

## Configuration

| Path | Purpose |
| --- | --- |
| `/etc/nut` | NUT server config (`nut.conf`, `ups.conf`, `upsd.conf`, `upsd.users`) — mount read-only |
| `/data/config.yaml` | Bridge config — mount read-only |
| `/data` | Holds `last_values.json`; use a named volume so HA sensor state survives restarts |

| Env var | Default | Purpose |
| --- | --- | --- |
| `RUN_NUT_SERVER` | `1` | Set to `0` to skip the driver + `upsd` and run only the bridge against an existing NUT server (point `ups.name` at `upsname@host`). |

## Proxmox LXC

Pass the UPS through twice — host → LXC, then LXC → container. On the Proxmox
host add to `/etc/pve/lxc/<vmid>.conf`:

```
features: nesting=1,keyctl=1
lxc.cgroup2.devices.allow: c 189:* rwm
lxc.mount.entry: /dev/bus/usb dev/bus/usb none bind,optional,create=dir
```

Then the container gets it via `--device /dev/bus/usb:/dev/bus/usb`. Full notes
in the [repo README](https://github.com/zackwag/nut2mqtt#running-inside-a-proxmox-lxc).
