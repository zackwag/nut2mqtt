#!/usr/bin/env python3
import json
import logging
import re
import signal
import subprocess
import sys
import time

import paho.mqtt.client as mqtt
import yaml

CONFIG_FILE = "config.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    """Load and return configuration from YAML file."""
    try:
        with open(CONFIG_FILE, "r") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        log.error(f"ha-ups-mqtt: Config file '{CONFIG_FILE}' not found.")
        sys.exit(1)
    except yaml.YAMLError as e:
        log.error(f"ha-ups-mqtt: Failed to parse '{CONFIG_FILE}': {e}")
        sys.exit(1)


def require_config(cfg, *keys):
    """
    Walk a nested config dict by key path and raise if the value is missing.
    Example: require_config(cfg, "mqtt", "broker")
    """
    value = cfg
    path = ".".join(keys)
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise KeyError(f"Missing required config key: '{path}'")
        value = value[key]
    if not value:
        raise ValueError(f"Config key '{path}' must not be empty")
    return value


def validate_config(config):
    """Validate all required config keys are present and non-empty."""
    require_config(config, "mqtt", "broker")
    require_config(config, "mqtt", "username")
    require_config(config, "mqtt", "password")
    require_config(config, "ups", "name")
    require_config(config, "ups", "friendly_name")
    if not config.get("sensors"):
        raise ValueError("No sensors defined in config.yaml under 'sensors'")


def sanitize_slug(value):
    """Sanitize a string for safe use in MQTT topics and HA entity IDs."""
    return re.sub(r"[^a-z0-9_]", "_", value.lower().strip())


# ---------------------------------------------------------------------------
# UPS
# ---------------------------------------------------------------------------

def read_ups(ups_name):
    """Call `upsc` to get UPS status and return as dict of key/value pairs."""
    try:
        result = subprocess.run(["upsc", ups_name], capture_output=True, text=True)
        if result.returncode != 0:
            return {}
        data = {}
        for line in result.stdout.splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                data[key.strip()] = val.strip()
        return data
    except Exception as e:
        log.error(f"ha-ups-mqtt: Unexpected error reading UPS data: {e}")
        return {}


def first_value(data, *keys, default="unknown"):
    """Return the first key found in `data` that has a value."""
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return default


def read_ups_with_retry(ups_name, max_attempts, retry_delay):
    """Attempt to read UPS data, retrying up to max_attempts times."""
    for attempt in range(1, max_attempts + 1):
        ups_data = read_ups(ups_name)
        if ups_data:
            return ups_data
        log.warning(f"ha-ups-mqtt: UPS not reachable, retrying {attempt}/{max_attempts}...")
        time.sleep(retry_delay)
    log.warning("ha-ups-mqtt: UPS unreachable at startup — device info will be partially unknown.")
    return {}


def setup_device_info(ups_conf, max_attempts, retry_delay):
    """Read UPS data and build the HA device info dict."""
    ups_data = read_ups_with_retry(ups_conf["name"], max_attempts, retry_delay)

    sw_version = first_value(ups_data, "driver.version")
    driver_data = ups_data.get("driver.version.data")
    if driver_data and sw_version != "unknown":
        sw_version = f"{sw_version} ({driver_data})"

    return {
        "identifiers": [sanitize_slug(ups_conf["name"])],
        "name": ups_conf["friendly_name"],
        "manufacturer": first_value(ups_data, "device.mfr", "ups.mfr"),
        "model": first_value(ups_data, "device.model", "ups.model"),
        "sw_version": sw_version,
    }


# ---------------------------------------------------------------------------
# MQTT helpers
# ---------------------------------------------------------------------------

def build_discovery_topic(entity_id, platform="sensor"):
    """Construct Home Assistant MQTT discovery topic."""
    return f"homeassistant/{platform}/{entity_id}/config"


def build_state_topic(base_topic, entity_id):
    """Construct MQTT state topic for publishing sensor values."""
    return f"{base_topic}/{entity_id}/state"


def make_entity_id(device_name, key):
    """Generate a Home Assistant entity_id from device name and sensor key."""
    base = sanitize_slug(device_name)
    key_clean = sanitize_slug(key)
    if key_clean.startswith(base + "_"):
        key_clean = key_clean[len(base) + 1:]
    return f"{base}_{key_clean}"


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    """Reconnect on unexpected MQTT disconnect."""
    if reason_code != 0:
        log.warning(f"ha-ups-mqtt: Unexpected disconnect (rc={reason_code}), attempting reconnect...")
        while True:
            try:
                client.reconnect()
                log.info("ha-ups-mqtt: Reconnected to MQTT broker")
                break
            except Exception as e:
                log.error(f"ha-ups-mqtt: Reconnect failed: {e}, retrying in 5s...")
                time.sleep(5)


def connect_mqtt(mqtt_conf, sensor_availability_topic):
    """Create, configure, and connect the MQTT client."""
    client = mqtt.Client(
        client_id=mqtt_conf.get("client_id", "ha-ups-mqtt"),
        protocol=mqtt.MQTTv311,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )
    client.on_disconnect = on_disconnect
    client.will_set(
        topic=sensor_availability_topic,
        payload="offline",
        qos=1,
        retain=True,
    )
    client.username_pw_set(mqtt_conf["username"], mqtt_conf["password"])
    client.connect(mqtt_conf["broker"], mqtt_conf["port"], 60)
    client.loop_start()
    log.info(f"ha-ups-mqtt: Connected to MQTT at {mqtt_conf['broker']}:{mqtt_conf['port']}")
    return client


# ---------------------------------------------------------------------------
# Discovery payloads
# ---------------------------------------------------------------------------

def build_sensor_discovery(sensor, device_info, base_topic, availability_topic):
    """
    Build MQTT discovery payload for a sensor.
    Returns (payload_dict, entity_id) or None if sensor has no required fields.
    """
    key = sensor.get("key")
    if not key:
        return None

    entity_id = make_entity_id(device_info["name"], key)

    friendly_name = sensor["friendly_name"]
    device_name_prefix = device_info["name"]
    if friendly_name.startswith(device_name_prefix):
        friendly_name = friendly_name[len(device_name_prefix):].strip()

    payload = {
        "name": f"{device_info['name']} {friendly_name}".strip(),
        "state_topic": build_state_topic(base_topic, entity_id),
        "unique_id": entity_id,
        "device": device_info,
        "availability_topic": availability_topic,
    }

    for field in ("unit", "icon", "device_class", "entity_category"):
        if field in sensor:
            ha_field = "unit_of_measurement" if field == "unit" else field
            payload[ha_field] = sensor[field]

    return payload, entity_id


def build_binary_sensor_discovery(ups_slug, friendly_name, binary_availability_topic, device_info):
    """Build MQTT discovery payload for the UPS Connected binary sensor."""
    entity_id = f"{ups_slug}_connected"
    payload = {
        "name": f"{friendly_name} Connected",
        "state_topic": binary_availability_topic,
        "payload_on": "online",
        "payload_off": "offline",
        "device_class": "connectivity",
        "unique_id": entity_id,
        "device": device_info,
    }
    return payload, entity_id


def build_sensor_lookup(sensors, device_info, base_topic, availability_topic):
    """
    Pre-compute a lookup table of sensor metadata at startup.
    Returns dict of {entity_id: {"key": ..., "state_topic": ..., "beeper": bool}}
    """
    lookup = {}
    for sensor in sensors:
        result = build_sensor_discovery(sensor, device_info, base_topic, availability_topic)
        if not result:
            continue
        payload, entity_id = result
        lookup[entity_id] = {
            "key": sensor["key"],
            "state_topic": payload["state_topic"],
            "beeper": sensor["key"] == "ups.beeper.status",
        }
    return lookup


def publish_all_discovery(client, config, device_info, ups_slug, base_topic,
                          sensor_availability_topic, binary_availability_topic):
    """Publish MQTT discovery config for all sensors and the binary sensor."""
    binary_payload, binary_entity_id = build_binary_sensor_discovery(
        ups_slug, device_info["name"], binary_availability_topic, device_info
    )
    client.publish(
        build_discovery_topic(binary_entity_id, platform="binary_sensor"),
        json.dumps(binary_payload),
        retain=True
    )

    for sensor in config["sensors"]:
        result = build_sensor_discovery(sensor, device_info, base_topic, sensor_availability_topic)
        if result:
            payload, entity_id = result
            client.publish(build_discovery_topic(entity_id), json.dumps(payload), retain=True)

    log.info(f"ha-ups-mqtt: Published discovery config for {len(config['sensors'])} sensors")


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def process_poll(client, ups_data, sensor_lookup, last_values,
                 sensor_availability_topic, binary_availability_topic):
    """Process a single poll result — publish state changes and prune stale sensors."""
    if not ups_data:
        log.warning("ha-ups-mqtt: UPS not reachable — marking sensors offline")
        client.publish(sensor_availability_topic, "offline", retain=True)
        client.publish(binary_availability_topic, "offline", retain=True)
        last_values.clear()
        return

    client.publish(sensor_availability_topic, "online", retain=True)
    client.publish(binary_availability_topic, "online", retain=True)

    current_entity_ids = set()
    changed = 0

    for entity_id, meta in sensor_lookup.items():
        value = ups_data.get(meta["key"])
        if value is None:
            continue

        current_entity_ids.add(entity_id)

        if meta["beeper"] and isinstance(value, str):
            value = value.title()

        if last_values.get(entity_id) != value:
            client.publish(meta["state_topic"], value, retain=True)
            last_values[entity_id] = value
            changed += 1
            log.debug(f"ha-ups-mqtt: {entity_id} = {value}")

    # Prune sensors no longer present in UPS data
    for entity_id in set(last_values.keys()) - current_entity_ids:
        del last_values[entity_id]
        log.debug(f"ha-ups-mqtt: Pruned stale sensor {entity_id}")

    if changed > 0:
        log.info(f"ha-ups-mqtt: Poll complete — {changed} value(s) changed")
    else:
        log.debug("ha-ups-mqtt: Poll complete — no changes")


def poll_loop(client, ups_name, sensor_lookup,
              sensor_availability_topic, binary_availability_topic, poll_interval):
    """Main polling loop — reads UPS data and processes each result."""
    last_values = {}

    while True:
        try:
            ups_data = read_ups(ups_name)
            process_poll(
                client, ups_data, sensor_lookup, last_values,
                sensor_availability_topic, binary_availability_topic
            )
        except Exception as e:
            log.error(f"ha-ups-mqtt: Unexpected error in polling loop: {e}")

        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config = load_config()
    validate_config(config)

    mqtt_conf = config["mqtt"]
    ups_conf  = config["ups"]

    base_topic    = mqtt_conf.get("base_topic", "home/ups")
    poll_interval = int(ups_conf.get("poll_interval", 30))
    max_attempts  = int(ups_conf.get("startup_max_attempts", 5))
    retry_delay   = int(ups_conf.get("startup_retry_delay", 2))

    ups_slug = sanitize_slug(ups_conf["friendly_name"])

    sensor_availability_topic = f"{base_topic}/{ups_slug}_sensors/availability"
    binary_availability_topic = f"{base_topic}/{ups_slug}_connected/availability"

    client = connect_mqtt(mqtt_conf, sensor_availability_topic)

    client.publish(sensor_availability_topic, "online", retain=True)
    client.publish(binary_availability_topic, "online", retain=True)

    def handle_exit(signum, frame):
        log.info("ha-ups-mqtt: Shutting down...")
        client.publish(sensor_availability_topic, "offline", retain=True)
        client.publish(binary_availability_topic, "offline", retain=True)
        client.loop_stop()
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    device_info = setup_device_info(ups_conf, max_attempts, retry_delay)

    publish_all_discovery(
        client, config, device_info, ups_slug,
        base_topic, sensor_availability_topic, binary_availability_topic
    )

    sensor_lookup = build_sensor_lookup(
        config["sensors"], device_info, base_topic, sensor_availability_topic
    )

    poll_loop(
        client, ups_conf["name"], sensor_lookup,
        sensor_availability_topic, binary_availability_topic, poll_interval
    )


if __name__ == "__main__":
    main()
