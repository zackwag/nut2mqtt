# HA UPS MQTT Bridge

A simple Python bridge that polls UPS data from
[Network UPS Tools (NUT)](https://networkupstools.org/) using `upsc` and publishes
metrics to MQTT for integration with
[Home Assistant](https://www.home-assistant.io/).

This project is intentionally lightweight and avoids custom HA integrations by
leveraging MQTT Discovery.

---

## Features

- Polls UPS metrics via `upsc`
- Publishes UPS status and sensor values to MQTT
- Auto-discovers entities in Home Assistant using MQTT Discovery
- Publishes a heartbeat sensor to prevent stale UPS states
- Supports Home Assistant entity categories (e.g. diagnostics)
- Configurable via YAML
- Designed to run as a systemd service for reliability

---

## Why the Heartbeat Exists

MQTT sensors in Home Assistant only update their `last_updated` timestamp when
the **state value changes**. For UPS data, some values (like `OL`) may remain
unchanged for hours or days.

To avoid Home Assistant showing *stale but healthy-looking data* if the bridge
stops running, this project publishes a **heartbeat sensor** on every poll
interval.

- The heartbeat value is the current epoch time
- If the bridge stops running, the heartbeat stops updating
- Home Assistant can use this to detect stale UPS data reliably

This makes the system resilient to:

- Raspberry Pi shutdowns
- Network failures
- Silent process crashes

---

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/zackwag/ha-ups-mqtt.git
cd ha-ups-mqtt
```

### 2. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

Edit the `config.yaml` file and replace each occurence of `[CHANGEME]` with the correct information.

The configuration controls:

- MQTT connection details
- UPS name used by upsc
- Which UPS metrics are published
- Optional Home Assistant metadata (icons, units, entity category)

### 4. Test run

```bash
./venv/bin/python3 ha-ups-mqtt.py
```

## Run as a System Service (Recommended)

### 1. Copy the service file

```bash
sudo cp systemd/ha-ups-mqtt.service /etc/systemd/system/
```

### 2. Adjust paths if needed

Make sure the `ExecStart` and `WorkingDirectory` in `ha-ups-mqtt.service` point to your installation directory (default: `/opt/ha-ups-mqtt`).

### 3. Reload systemd and enable service

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ha-ups-mqtt
```

### 4. Check status

```bash
systemctl status ha-ups-mqtt
```

Logs can be viewed with:

```bash
journalctl -u ups-mqtt -f
```

## Home Assistant

With MQTT Discovery enabled, entities will appear automatically.

### Entities will be named

Assuming you have set the device name to `Den UPS`:

- sensor.den_ups_battery_charge → **Den UPS Battery Charge**
- sensor.den_ups_output_voltage → **Den UPS Output Voltage**
- sensor.den_ups_status_data → **Den UPS Status Data**
- sensor.den_ups_heartbeat → **Den UPS Heartbeat**

### Diagnostics vs Sensors

This project supports Home Assistant's `entity_category` field.

Sensors marked with:

```yaml
entity_category: diagnostic
```

will appear under **Device → Diagnostics** instead of cluttering dashboards.

Recommended diagnostic entities:

- Heartbeat
- Firmware / version info
- Uptime-style sensors

### Example Sensors

- Battery Charge (%)
- Runtime Remaining (seconds)
- Input Voltage (V)
- Output Voltage (V)
- Load (%)
- Status (string)
- Heartbeat (epoch timestamp)

**Note** Derived or user-facing logic (for example: "UPS Online") is intentionally left to Home Assistant templates.

## Contributing

Pull requests are welcome! If you have improvements (new sensors, config options, Dockerfile, etc.), open an issue or PR.
