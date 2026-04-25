# NUT to MQTT Bridge

[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)
[![Backend](https://img.shields.io/badge/backend-Python%203-3776AB?style=flat-square&logo=python)](https://www.python.org/)
[![NUT](https://img.shields.io/badge/requires-NUT-orange?style=flat-square)](https://networkupstools.org/)

A lightweight Python bridge that polls UPS data from
[Network UPS Tools (NUT)](https://networkupstools.org/) using `upsc` and publishes
metrics to MQTT for integration with [Home Assistant](https://www.home-assistant.io/).

This project is intentionally lightweight and avoids custom HA integrations by
leveraging MQTT Discovery.

---

## Features

- Polls UPS metrics via `upsc`
- Publishes UPS status and sensor values to MQTT
- Auto-discovers entities in Home Assistant via MQTT Discovery
- Publishes a **UPS Connected** binary sensor using MQTT LWT for reliable offline detection
- Supports Home Assistant entity categories (e.g. diagnostics)
- Configurable via YAML
- Designed to run as a systemd service for reliability

---

## Why LWT Instead of a Heartbeat

MQTT sensors in Home Assistant only update their `last_updated` timestamp when
the **state value changes**. For UPS data, some values (like `OL`) may remain
unchanged for hours or days, making it hard to know if the bridge is still running.

This project uses MQTT **Last Will and Testament (LWT)** to solve this cleanly.

- When the bridge starts, it publishes `online` to the availability topic
- When the bridge stops gracefully or crashes, the broker automatically publishes `offline`
- Home Assistant marks all sensors as `unavailable` when the availability topic goes `offline`
- A **UPS Connected** binary sensor reflects live reachability of the UPS itself

This makes the system resilient to:

- Raspberry Pi shutdowns
- Network failures
- Silent process crashes

---

## Requirements

- [Network UPS Tools (NUT)](https://networkupstools.org/) installed and configured
- `upsc` accessible on the system path
- MQTT broker (e.g. [Mosquitto](https://mosquitto.org/))
- Home Assistant with MQTT integration enabled
- Python 3.8+

---

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/zackwag/nut2mqtt.git
cd nut2mqtt
```

### 2. Install dependencies

```bash
sudo pip3 install -r requirements.txt --break-system-packages
```

### 3. Configure

Copy or edit `config.yaml` and replace each occurrence of `[CHANGEME]` with the correct information.

```bash
cp config.yaml.example config.yaml
```

The configuration controls:

- MQTT connection details
- UPS name used by `upsc`
- Which UPS metrics are published
- Optional Home Assistant metadata (icons, units, entity category)

### 4. Test run

```bash
python3 nut2mqtt.py
```

---

## Configuration Reference

### `mqtt`

| Key | Required | Default | Description |
|---|---|---|---|
| `broker` | ✅ | — | MQTT broker IP address |
| `port` | ❌ | `1883` | MQTT broker port |
| `username` | ✅ | — | MQTT broker username |
| `password` | ✅ | — | MQTT broker password |
| `base_topic` | ❌ | `nut2mqtt` | Base MQTT topic prefix |
| `client_id` | ❌ | `nut2mqtt` | MQTT client identifier |

### `ups`

| Key | Required | Default | Description |
|---|---|---|---|
| `name` | ✅ | — | UPS name as configured in NUT (used with `upsc`) |
| `friendly_name` | ✅ | — | Display name used in Home Assistant |
| `poll_interval` | ❌ | `30` | Seconds between polls |
| `startup_max_attempts` | ❌ | `5` | Retry attempts if UPS unreachable at startup |
| `startup_retry_delay` | ❌ | `2` | Seconds between startup retries |

### `sensors`

Each sensor entry supports the following fields:

| Key | Required | Description |
|---|---|---|
| `key` | ✅ | NUT variable name (e.g. `battery.charge`) |
| `friendly_name` | ✅ | Display name in Home Assistant |
| `unit` | ❌ | Unit of measurement (e.g. `%`, `V`, `s`) |
| `icon` | ❌ | MDI icon (e.g. `mdi:battery`) |
| `device_class` | ❌ | Home Assistant device class (e.g. `battery`, `voltage`) |
| `entity_category` | ❌ | Set to `diagnostic` to move sensor to Diagnostics section |

---

## Run as a System Service (Recommended)

### 1. Create a dedicated system user

```bash
sudo useradd --system --no-create-home --group nogroup ups
```

### 2. Copy files to install directory

```bash
sudo mkdir -p /opt/nut2mqtt
sudo cp -r . /opt/nut2mqtt
sudo chown -R ups:nogroup /opt/nut2mqtt
```

### 3. Install dependencies

```bash
sudo pip3 install -r /opt/nut2mqtt/requirements.txt --break-system-packages
```

### 4. Copy the service file

```bash
sudo cp systemd/nut2mqtt.service /etc/systemd/system/
```

### 5. Reload systemd and enable service

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nut2mqtt
```

### 6. Check status

```bash
systemctl status nut2mqtt
```

### 7. View logs

```bash
journalctl -u nut2mqtt -f
```

---

## Home Assistant

With MQTT Discovery enabled, entities appear automatically under a **UPS** device
in Settings → Devices & Services → MQTT.

### Example Entity Names

Assuming `friendly_name` is set to `Den UPS`:

| Entity ID | Name |
|---|---|
| `sensor.den_ups_battery_charge` | Den UPS Battery Charge |
| `sensor.den_ups_input_voltage` | Den UPS Input Voltage |
| `sensor.den_ups_output_voltage` | Den UPS Output Voltage |
| `sensor.den_ups_status_data` | Den UPS Status Data |
| `binary_sensor.den_ups_connected` | Den UPS Connected |

### Diagnostics vs Sensors

Sensors marked with `entity_category: diagnostic` appear under **Device → Diagnostics**
instead of cluttering dashboards. Recommended diagnostic entities include firmware info,
runtime thresholds, and delay timers.

### Example Sensors

- Battery Charge (%)
- Battery Runtime (s)
- Input / Output Voltage (V)
- UPS Load (%)
- UPS Status
- UPS Connected (binary)

> **Note:** Derived or user-facing logic (for example: "UPS Online") is intentionally
> left to Home Assistant templates.

---

## Contributing

Pull requests are welcome! If you have improvements — new sensors, config options, bug fixes —
open an issue or PR.

## License

MIT
