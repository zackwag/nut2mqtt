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
__version__ = "1.7.1"
APP_NAME = "nut2mqtt"

# Filenames
CONFIG_FILE = "config.yaml"
LAST_VALUES_FILE = "last_values.json"
DEVICE_INFO_FILE = "device_info.json"

# UPS-reported identity fields cached to disk so a restart still shows the real
# manufacturer/model/firmware before the driver has polled them back.
DEVICE_IDENTITY_FIELDS = ("manufacturer", "model", "sw_version")

# Default Config Values
DEFAULT_CLIENT_ID = "nut2mqtt"
DEFAULT_BASE_TOPIC    = "nut2mqtt"
DEFAULT_POLL_INTERVAL = 30
DEFAULT_MAX_ATTEMPTS  = 5
DEFAULT_RETRY_DELAY   = 2

# After a switch runs its on/off instant command, wait this long before
# re-reading the UPS so the driver has polled the new status back.
DEFAULT_SETTLE_SECONDS = 0.5
DEFAULT_SWITCH_STATE_ON = ["enabled"]

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
    if config.get("commands"):
        require_config(config, "ups", "upscmd_username")
        require_config(config, "ups", "upscmd_password")
    if config.get("switches"):
        require_config(config, "ups", "upscmd_username")
        require_config(config, "ups", "upscmd_password")
        for switch in config["switches"]:
            for field in ("key", "friendly_name", "status_key", "command_on", "command_off"):
                if not switch.get(field):
                    raise ValueError(f"Switch entry is missing required field: '{field}'")
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


def load_device_info_cache():
    """Load the last known-good UPS identity fields, keyed by UPS id."""
    try:
        with open(DEVICE_INFO_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        log_warning("Could not parse device_info.json, ignoring cache")
        return {}


def save_device_info_cache(cache):
    """Persist known-good UPS identity fields to disk."""
    try:
        with open(DEVICE_INFO_FILE, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        log_error(f"Failed to save device_info cache: {e}")


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


def run_upscmd(ups_name, command, username, password):
    """Execute a NUT instant command via `upscmd`. Returns True on success."""
    try:
        result = subprocess.run(
            ["upscmd", "-u", username, "-p", password, ups_name, command],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            log_error(f"upscmd '{command}' failed: {result.stderr.strip()}")
            return False
        log_info(f"upscmd '{command}' executed successfully")
        return True
    except Exception as e:
        log_error(f"Unexpected error running upscmd '{command}': {e}")
        return False


def log_available_upscmds(ups_name):
    """Run `upscmd -l` and log the instant commands the UPS/driver supports."""
    try:
        result = subprocess.run(["upscmd", "-l", ups_name], capture_output=True, text=True)
        if result.returncode != 0:
            log_warning(f"Could not list UPS instant commands: {result.stderr.strip()}")
            return
        commands = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        log_info(f"UPS supports {len(commands)} instant command(s): {', '.join(commands)}")
    except Exception as e:
        log_error(f"Unexpected error listing UPS instant commands: {e}")


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


def _identity_from_ups_data(ups_data):
    """Pull manufacturer/model/sw_version out of raw UPS data (values or None)."""
    driver_version = first_value(ups_data, "driver.version")
    driver_data    = ups_data.get("driver.version.data")
    if driver_version and driver_data:
        sw_version = f"{driver_version} ({driver_data})"
    else:
        sw_version = driver_version or None

    return {
        "manufacturer": first_value(ups_data, "device.mfr", "ups.mfr"),
        "model":        first_value(ups_data, "device.model", "ups.model"),
        "sw_version":   sw_version,
    }


def setup_device_info(ups_conf, max_attempts, retry_delay):
    """
    Build the HA device info dict from UPS data, falling back to the last
    known-good values cached on disk when the UPS has not reported its identity
    yet (common for a poll or two after a restart).
    """
    ups_id = sanitize_slug(ups_conf["name"])
    ups_data = read_ups_with_retry(ups_conf["name"], max_attempts, retry_delay)

    cache = load_device_info_cache()
    cached = cache.get(ups_id, {})

    # No cache to lean on — give the driver a few extra polls to report its
    # identity before we publish "unknown" into retained discovery.
    if not cached.get("manufacturer") and not cached.get("model"):
        for _ in range(max_attempts):
            identity = _identity_from_ups_data(ups_data)
            if identity["manufacturer"] or identity["model"]:
                break
            time.sleep(retry_delay)
            ups_data = read_ups(ups_conf["name"]) or ups_data

    fresh = _identity_from_ups_data(ups_data)

    resolved = {}
    used_cache = False
    for field in DEVICE_IDENTITY_FIELDS:
        if fresh.get(field):
            resolved[field] = fresh[field]
        elif cached.get(field):
            resolved[field] = cached[field]
            used_cache = True
        else:
            resolved[field] = "unknown"

    known_good = {f: v for f, v in resolved.items() if v != "unknown"}
    if known_good and known_good != {f: cached.get(f) for f in known_good}:
        cache[ups_id] = {**cached, **known_good}
        save_device_info_cache(cache)

    if used_cache:
        log_info("Using cached UPS identity for fields the UPS has not reported yet")

    return {
        "identifiers": [ups_id],
        "name": ups_conf["friendly_name"],
        **resolved,
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


def build_command_topic(base_topic, entity_id):
    """Construct MQTT command topic for receiving button presses."""
    return f"{base_topic}/{entity_id}/set"


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


def build_command_discovery(command, device_info, base_topic, availability_topic):
    """
    Build MQTT discovery payload for a button entity representing a NUT instant command.
    Returns (payload_dict, entity_id) or None if command has no required fields.
    """
    key = command.get("key")
    if not key:
        return None

    entity_id = make_entity_id(device_info["name"], key)

    friendly_name = command["friendly_name"]
    device_name_prefix = device_info["name"]
    if friendly_name.startswith(device_name_prefix):
        friendly_name = friendly_name[len(device_name_prefix):].strip()

    payload = {
        "name": f"{device_info['name']} {friendly_name}".strip(),
        "command_topic": build_command_topic(base_topic, entity_id),
        "payload_press": "PRESS",
        "unique_id": entity_id,
        "device": device_info,
        "availability_topic": availability_topic,
    }

    for field in ("icon", "entity_category"):
        if field in command:
            payload[field] = command[field]

    return payload, entity_id


def switch_state_on_values(switch):
    """Return the set of (lowercased) status values that mean the switch is ON."""
    values = switch.get("state_on") or DEFAULT_SWITCH_STATE_ON
    if isinstance(values, str):
        values = [values]
    return {str(v).strip().lower() for v in values}


def switch_state_from_status(raw, state_on_values):
    """Map a raw NUT status string to the HA switch payload 'ON' or 'OFF'."""
    return "ON" if str(raw).strip().lower() in state_on_values else "OFF"


def build_switch_discovery(switch, device_info, base_topic, availability_topic):
    """
    Build MQTT discovery payload for a switch entity backed by a NUT status
    variable plus a pair of on/off instant commands.
    Returns (payload_dict, entity_id) or None if required fields are missing.
    """
    key = switch.get("key")
    if not key or not switch.get("status_key"):
        return None
    if not switch.get("command_on") or not switch.get("command_off"):
        return None

    entity_id = make_entity_id(device_info["name"], key)

    friendly_name = switch["friendly_name"]
    device_name_prefix = device_info["name"]
    if friendly_name.startswith(device_name_prefix):
        friendly_name = friendly_name[len(device_name_prefix):].strip()

    payload = {
        "name": f"{device_info['name']} {friendly_name}".strip(),
        "state_topic": build_state_topic(base_topic, entity_id),
        "command_topic": build_command_topic(base_topic, entity_id),
        "payload_on": "ON",
        "payload_off": "OFF",
        "state_on": "ON",
        "state_off": "OFF",
        "optimistic": bool(switch.get("optimistic", False)),
        "unique_id": entity_id,
        "device": device_info,
        "availability_topic": availability_topic,
    }

    for field in ("icon", "entity_category"):
        if field in switch:
            payload[field] = switch[field]

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


def publish_command_discovery_and_build_lookup(client, commands, device_info, base_topic,
                                               sensor_availability_topic):
    """
    Publish MQTT discovery config for each configured NUT instant command as a
    Home Assistant button entity. Returns dict of {command_topic: nut_command_key}.
    """
    lookup = {}

    for command in commands:
        result = build_command_discovery(command, device_info, base_topic, sensor_availability_topic)
        if not result:
            continue

        payload, entity_id = result
        client.publish(build_discovery_topic(entity_id, platform="button"), json.dumps(payload), retain=True)
        lookup[payload["command_topic"]] = command["key"]

    log_info(f"Published discovery config for {len(lookup)} command(s)")
    return lookup


def publish_switch_discovery_and_build_lookups(client, switches, device_info, base_topic,
                                               sensor_availability_topic):
    """
    Publish MQTT discovery config for each configured switch as a Home Assistant
    switch entity. Returns (command_lookup, state_lookup):
      command_lookup: {command_topic: {"on", "off", "status_key", "state_topic",
                                       "state_on"}} — used by the message handler
      state_lookup:   {entity_id: {"status_key", "state_topic", "state_on"}} —
                      used by the poll loop to publish real state
    """
    command_lookup = {}
    state_lookup = {}

    for switch in switches:
        result = build_switch_discovery(switch, device_info, base_topic, sensor_availability_topic)
        if not result:
            continue

        payload, entity_id = result
        client.publish(
            build_discovery_topic(entity_id, platform="switch"),
            json.dumps(payload), retain=True
        )

        meta = {
            "status_key": switch["status_key"],
            "state_topic": payload["state_topic"],
            "state_on": switch_state_on_values(switch),
        }
        state_lookup[entity_id] = meta
        command_lookup[payload["command_topic"]] = {
            **meta,
            "on": switch["command_on"],
            "off": switch["command_off"],
        }

    log_info(f"Published discovery config for {len(state_lookup)} switch(es)")
    return command_lookup, state_lookup


def make_on_message(ups_name, command_lookup, switch_lookup, username, password,
                    sensor_lookup, switch_state_lookup, last_values,
                    sensor_availability_topic, binary_availability_topic,
                    settle_seconds):
    """
    Build an MQTT on_message handler for button command topics and switch
    command topics.

    After a successful command, waits for the driver to settle and then runs a
    full poll so every sensor and switch reflects the new UPS state immediately.
    """
    def _poll_now(client):
        time.sleep(settle_seconds)
        ups_data = read_ups(ups_name)
        process_poll(client, ups_data, sensor_lookup, switch_state_lookup,
                     last_values, sensor_availability_topic,
                     binary_availability_topic)

    def on_message(client, userdata, msg):
        command_key = command_lookup.get(msg.topic)
        if command_key is not None:
            log_info(f"Received command '{command_key}' via MQTT")
            if run_upscmd(ups_name, command_key, username, password):
                _poll_now(client)
            return

        meta = switch_lookup.get(msg.topic)
        if meta is None:
            return

        payload = msg.payload.decode(errors="ignore").strip().upper()
        if payload == "ON":
            nut_command = meta["on"]
        elif payload == "OFF":
            nut_command = meta["off"]
        else:
            return

        log_info(f"Received switch '{payload}' -> '{nut_command}' via MQTT")
        if run_upscmd(ups_name, nut_command, username, password):
            _poll_now(client)
    return on_message


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def process_poll(client, ups_data, sensor_lookup, switch_lookup, last_values,
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

    for entity_id, meta in switch_lookup.items():
        raw = ups_data.get(meta["status_key"])
        if raw is None:
            continue

        current_entity_ids.add(entity_id)
        state = switch_state_from_status(raw, meta["state_on"])

        if last_values.get(entity_id) != state:
            client.publish(meta["state_topic"], state, retain=True)
            last_values[entity_id] = state
            save_last_values(last_values)
            changed += 1
            log_debug(f"{entity_id} = {state}")

    # Prune sensors no longer present in UPS data
    for entity_id in set(last_values.keys()) - current_entity_ids:
        del last_values[entity_id]
        save_last_values(last_values)
        log_debug(f"Pruned stale sensor {entity_id}")

    if changed > 0:
        log_info(f"Poll complete — {changed} value(s) changed")
    else:
        log_debug("Poll complete — no changes")


def poll_loop(client, ups_name, sensor_lookup, switch_lookup, last_values,
              sensor_availability_topic, binary_availability_topic, poll_interval):
    """Main polling loop — reads UPS data and processes each result."""
    while True:
        try:
            ups_data = read_ups(ups_name)
            process_poll(
                client, ups_data, sensor_lookup, switch_lookup, last_values,
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

    base_topic      = mqtt_conf.get("base_topic", DEFAULT_BASE_TOPIC)
    poll_interval   = int(ups_conf.get("poll_interval", DEFAULT_POLL_INTERVAL))
    max_attempts    = int(ups_conf.get("startup_max_attempts", DEFAULT_MAX_ATTEMPTS))
    retry_delay     = int(ups_conf.get("startup_retry_delay", DEFAULT_RETRY_DELAY))
    settle_seconds  = float(ups_conf.get("settle_seconds", DEFAULT_SETTLE_SECONDS))

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

    commands_conf = config.get("commands")
    switches_conf = config.get("switches")

    command_lookup = {}
    switch_command_lookup = {}
    switch_state_lookup = {}

    if commands_conf or switches_conf:
        log_available_upscmds(ups_conf["name"])

    if commands_conf:
        command_lookup = publish_command_discovery_and_build_lookup(
            client, commands_conf, device_info, base_topic, sensor_availability_topic
        )

    if switches_conf:
        switch_command_lookup, switch_state_lookup = publish_switch_discovery_and_build_lookups(
            client, switches_conf, device_info, base_topic, sensor_availability_topic
        )

    last_values = load_last_values()

    if command_lookup or switch_command_lookup:
        client.on_message = make_on_message(
            ups_conf["name"], command_lookup, switch_command_lookup,
            ups_conf["upscmd_username"], ups_conf["upscmd_password"],
            sensor_lookup, switch_state_lookup, last_values,
            sensor_availability_topic, binary_availability_topic,
            settle_seconds,
        )
        for topic in (*command_lookup, *switch_command_lookup):
            client.subscribe(topic)
        log_info(
            f"Subscribed to {len(command_lookup) + len(switch_command_lookup)} "
            "command/switch topic(s)"
        )

    poll_loop(
        client, ups_conf["name"], sensor_lookup, switch_state_lookup, last_values,
        sensor_availability_topic, binary_availability_topic, poll_interval
    )


if __name__ == "__main__":
    main()
