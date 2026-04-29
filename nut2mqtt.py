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

# App Information
__version__ = "1.4.1"
APP_NAME = "nut2mqtt"

# Filenames
CONFIG_FILE = "config.yaml"
LAST_VALUES_FILE = "last_values.json"

# Default Config Values
DEFAULT_CLIENT_ID = "nut2mqtt"
DEFAULT_BASE_TOPIC    = "nut2mqtt"
DEFAULT_POLL_INTERVAL = 30
DEFAULT_MAX_ATTEMPTS  = 5
DEFAULT_RETRY_DELAY   = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)


def log_info(msg):
    log.info(f"{APP_NAME}: {msg}")

def log_warning(msg):
    log.warning(f"{APP_NAME}: {msg}")

def log_error(msg):
    log.error(f"{APP_NAME}: {msg}")

def log_debug(msg):
    log.debug(f"{APP_NAME}: {msg}")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    """Load and return configuration from YAML file."""
    try:
        with open(CONFIG_FILE, "r") as f:
            return validate_config(yaml.safe_load(f))
    except FileNotFoundError:
        log_error(f"Config file '{CONFIG_FILE}' not found.")
        sys.exit(1)
    except yaml.YAMLError as e:
        log_error(f"Failed to parse '{CONFIG_FILE}': {e}")
        sys.exit(1)
    except (KeyError, ValueError) as e:
        log_error(f"Invalid configuration: {e}")
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
    return config


def sanitize_slug(value):
    """Sanitize a string for safe use in MQTT topics and HA entity IDs."""
    return re.sub(r"[^a-z0-9_]", "_", value.lower().strip())


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_last_values():
    """Load persisted sensor values from disk."""
    try:
        with open(LAST_VALUES_FILE, "r") as f:
            data = json.load(f)
            log_info(f"Loaded {len(data)} persisted sensor value(s) from disk")
            return data
    except FileNotFoundError:
        log_info("No persisted sensor values found, starting fresh")
        return {}
    except json.JSONDecodeError:
        log_warning("Could not parse last_values.json, starting fresh")
        return {}


def save_last_values(last_values):
    """Persist sensor values to disk."""
    try:
        with open(LAST_VALUES_FILE, "w") as f:
            json.dump(last_values, f)
    except Exception as e:
        log_error(f"Failed to save last_values: {e}")


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
        log_error(f"Unexpected error reading UPS data: {e}")
        return {}


def first_value(data, *keys, default=None):
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
        log_warning(f"UPS not reachable, retrying {attempt}/{max_attempts}...")
        time.sleep(retry_delay)
    log_warning("UPS unreachable at startup — device info will be partially unknown.")
    return {}


def setup_device_info(ups_conf, max_attempts, retry_delay):
    """Read UPS data and build the HA device info dict."""
    ups_data = read_ups_with_retry(ups_conf["name"], max_attempts, retry_delay)

    driver_version = first_value(ups_data, "driver.version")
    driver_data    = ups_data.get("driver.version.data")

    if driver_version and driver_data:
        sw_version = f"{driver_version} ({driver_data})"
    elif driver_version:
        sw_version = driver_version
    else:
        sw_version = "unknown"

    return {
        "identifiers": [sanitize_slug(ups_conf["name"])],
        "name": ups_conf["friendly_name"],
        "manufacturer": first_value(ups_data, "device.mfr", "ups.mfr", default="unknown"),
        "model":        first_value(ups_data, "device.model", "ups.model", default="unknown"),
        "sw_version":   sw_version,
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
        log_warning(f"Unexpected disconnect (rc={reason_code}), attempting reconnect...")
        while True:
            try:
                client.reconnect()
                log_info("Reconnected to MQTT broker")
                break
            except Exception as e:
                log_error(f"Reconnect failed: {e}, retrying in 5s...")
                time.sleep(5)


def connect_mqtt(mqtt_conf, sensor_availability_topic):
    """Create, configure, and connect the MQTT client."""
    client = mqtt.Client(
        client_id=mqtt_conf.get("client_id", DEFAULT_CLIENT_ID),
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
    log_info(f"Connected to MQTT at {mqtt_conf['broker']}:{mqtt_conf['port']}")
    return client


def register_signal_handlers(client, sensor_availability_topic, binary_availability_topic):
    """Register SIGINT and SIGTERM handlers for graceful shutdown."""
    def handle_exit(signum, frame):
        log_info("Shutting down...")
        client.publish(sensor_availability_topic, "offline", retain=True)
        client.publish(binary_availability_topic, "offline", retain=True)
        client.loop_stop()
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)


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


def publish_and_build_lookup(client, sensors, device_info, base_topic,
                             sensor_availability_topic):
    """
    Single pass over sensors — publish discovery and build lookup table simultaneously.
    Returns dict of {entity_id: {"key": ..., "state_topic": ..., "beeper": bool}}
    """
    lookup = {}

    for sensor in sensors:
        result = build_sensor_discovery(sensor, device_info, base_topic, sensor_availability_topic)
        if not result:
            continue

        payload, entity_id = result
        client.publish(build_discovery_topic(entity_id), json.dumps(payload), retain=True)

        lookup[entity_id] = {
            "key": sensor["key"],
            "state_topic": payload["state_topic"],
            "beeper": sensor["key"] == "ups.beeper.status",
        }

    log_info(f"Published discovery config for {len(lookup)} sensors")
    return lookup


def publish_binary_discovery(client, ups_slug, device_info, binary_availability_topic):
    """Publish MQTT discovery config for the UPS Connected binary sensor."""
    binary_payload, binary_entity_id = build_binary_sensor_discovery(
        ups_slug, device_info["name"], binary_availability_topic, device_info
    )
    client.publish(
        build_discovery_topic(binary_entity_id, platform="binary_sensor"),
        json.dumps(binary_payload),
        retain=True
    )
    log_info("Published binary sensor discovery config")


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def process_poll(client, ups_data, sensor_lookup, last_values,
                 sensor_availability_topic, binary_availability_topic):
    """Process a single poll result — publish state changes and prune stale sensors."""
    if not ups_data:
        log_warning("UPS not reachable — marking sensors offline")
        client.publish(sensor_availability_topic, "offline", retain=True)
        client.publish(binary_availability_topic, "offline", retain=True)
        last_values.clear()
        save_last_values(last_values)
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
            save_last_values(last_values)
            changed += 1
            log_debug(f"{entity_id} = {value}")

    # Prune sensors no longer present in UPS data
    for entity_id in set(last_values.keys()) - current_entity_ids:
        del last_values[entity_id]
        save_last_values(last_values)
        log_debug(f"Pruned stale sensor {entity_id}")

    if changed > 0:
        log_info(f"Poll complete — {changed} value(s) changed")
    else:
        log_debug("Poll complete — no changes")


def poll_loop(client, ups_name, sensor_lookup,
              sensor_availability_topic, binary_availability_topic, poll_interval):
    """Main polling loop — reads UPS data and processes each result."""
    last_values = load_last_values()

    while True:
        try:
            ups_data = read_ups(ups_name)
            process_poll(
                client, ups_data, sensor_lookup, last_values,
                sensor_availability_topic, binary_availability_topic
            )
        except Exception as e:
            log_error(f"Unexpected error in polling loop: {e}")

        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log_info(f"Starting version {__version__}")
    config = load_config()

    mqtt_conf = config["mqtt"]
    ups_conf  = config["ups"]

    base_topic    = mqtt_conf.get("base_topic", DEFAULT_BASE_TOPIC)
    poll_interval = int(ups_conf.get("poll_interval", DEFAULT_POLL_INTERVAL))
    max_attempts  = int(ups_conf.get("startup_max_attempts", DEFAULT_MAX_ATTEMPTS))
    retry_delay   = int(ups_conf.get("startup_retry_delay", DEFAULT_RETRY_DELAY))

    ups_slug = sanitize_slug(ups_conf["friendly_name"])

    sensor_availability_topic = f"{base_topic}/{ups_slug}_sensors/availability"
    binary_availability_topic = f"{base_topic}/{ups_slug}_connected/availability"

    client = connect_mqtt(mqtt_conf, sensor_availability_topic)

    client.publish(sensor_availability_topic, "online", retain=True)
    client.publish(binary_availability_topic, "online", retain=True)

    register_signal_handlers(client, sensor_availability_topic, binary_availability_topic)

    device_info = setup_device_info(ups_conf, max_attempts, retry_delay)

    publish_binary_discovery(client, ups_slug, device_info, binary_availability_topic)

    sensor_lookup = publish_and_build_lookup(
        client, config["sensors"], device_info, base_topic, sensor_availability_topic
    )

    poll_loop(
        client, ups_conf["name"], sensor_lookup,
        sensor_availability_topic, binary_availability_topic, poll_interval
    )


if __name__ == "__main__":
    main()
