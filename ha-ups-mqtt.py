#!/usr/bin/env python3
import json
import subprocess
import time
import signal
import sys

import paho.mqtt.client as mqtt
import yaml

CONFIG_FILE = "config.yaml"


def load_config():
    """Load configuration from YAML file."""
    with open(CONFIG_FILE, "r") as f:
        return yaml.safe_load(f)


def read_ups(ups_name):
    """Call `upsc` CLI to get UPS status and return as dict of key/value pairs."""
    result = subprocess.run(["upsc", ups_name], capture_output=True, text=True)
    if result.returncode != 0:
        return {}  # UPS not reachable or command failed
    data = {}
    for line in result.stdout.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            data[key.strip()] = val.strip()
    return data


def first_value(data, *keys, default="unknown"):
    """
    Return the first key found in `data` that has a value.
    Useful for falling back on multiple possible UPS fields.
    """
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return default


def build_discovery_topic(entity_id, platform="sensor"):
    """Construct Home Assistant MQTT discovery topic for a sensor or binary_sensor."""
    return f"homeassistant/{platform}/{entity_id}/config"


def build_state_topic(base_topic, entity_id):
    """Construct MQTT state topic for publishing sensor values."""
    return f"{base_topic}/{entity_id}/state"


def make_entity_id(device_name, key):
    """
    Generate a Home Assistant entity_id by combining the device name and sensor key.
    Converts to lowercase and replaces dots/spaces with underscores.
    """
    base = device_name.lower().replace(" ", "_")
    key_clean = key.lower().replace(".", "_")
    if key_clean.startswith(base + "_"):
        key_clean = key_clean[len(base) + 1 :]
    return f"{base}_{key_clean}"


def build_payload(sensor, ups_data, device_info, availability_topic):
    """
    Build MQTT discovery payload for a sensor.
    Adds unit, icon, device_class, entity_category if present.
    Includes availability_topic for HA to mark `unavailable` if UPS goes offline.
    """
    key = sensor["key"]
    value = ups_data.get(key)
    if value is None:
        return None  # Skip sensors with no data

    entity_id = make_entity_id(device_info["name"], key)

    friendly_name = sensor["friendly_name"]
    device_name_prefix = device_info["name"]
    if friendly_name.startswith(device_name_prefix):
        friendly_name = friendly_name[len(device_name_prefix) :].strip()

    payload = {
        "name": f"{device_info['name']} {friendly_name}".strip(),
        "state_topic": build_state_topic(config["mqtt"]["base_topic"], entity_id),
        "unique_id": entity_id,
        "device": device_info,
        "availability_topic": availability_topic,  # Mark unavailable if UPS offline
    }

    if "unit" in sensor:
        payload["unit_of_measurement"] = sensor["unit"]
    if "icon" in sensor:
        payload["icon"] = sensor["icon"]
    if "device_class" in sensor:
        payload["device_class"] = sensor["device_class"]
    if "entity_category" in sensor:
        payload["entity_category"] = sensor["entity_category"]

    return payload, value


def main():
    global config
    config = load_config()

    mqtt_conf = config["mqtt"]
    ups_conf = config["ups"]

    # ---- Create a slug from the UPS name/friendly_name for topic prefixes ----
    ups_name_raw = ups_conf.get("name") or ups_conf.get("friendly_name", "ups")
    ups_slug = ups_name_raw.lower().replace(" ", "_")

    # ---- Availability topics ----
    # Used by HA to mark sensors online/offline
    sensor_availability_topic = f"{mqtt_conf['base_topic']}/{ups_slug}_sensors/availability"
    binary_availability_topic = f"{mqtt_conf['base_topic']}/{ups_slug}_connected/availability"

    # ---- MQTT client setup ----
    client = mqtt.Client(
        client_id=mqtt_conf.get("client_id", ""),
        protocol=mqtt.MQTTv311,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )

    # LWT (Last Will & Testament) ensures HA marks sensors offline if script crashes
    client.will_set(
        topic=sensor_availability_topic,
        payload="offline",
        qos=1,
        retain=True,
    )

    # Optional MQTT authentication
    if mqtt_conf.get("username") and mqtt_conf.get("password"):
        client.username_pw_set(mqtt_conf["username"], mqtt_conf["password"])

    # Connect to broker and start loop
    client.connect(mqtt_conf["broker"], mqtt_conf["port"], 60)
    client.loop_start()

    # Publish initial online states
    client.publish(sensor_availability_topic, "online", retain=True)
    client.publish(binary_availability_topic, "true", retain=True)

    # ---- Graceful shutdown ----
    def handle_exit(signum, frame):
        """Publish offline states and disconnect cleanly on SIGINT/SIGTERM."""
        print("Stopping UPS MQTT bridge...")
        client.publish(sensor_availability_topic, "offline", retain=True)
        client.publish(binary_availability_topic, "false", retain=True)
        client.loop_stop()
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    last_values = {}

    # ---- Initial UPS read for device info with retry ----
    max_attempts = int(ups_conf.get("startup_max_attempts", 5))
    attempt = 0
    ups_data = {}
    while attempt < max_attempts:
        ups_data = read_ups(ups_conf["name"])
        if ups_data:
            break  # Successful read
        attempt += 1
        print(f"UPS not reachable, retrying {attempt}/{max_attempts}...")
        time.sleep(2)  # Small delay before retry
    
    if not ups_data:
        print("Warning: UPS unreachable at startup, device info will be partially unknown.")
    
    sw_version = first_value(ups_data, "driver.version")
    driver_data = ups_data.get("driver.version.data")
    if driver_data and sw_version != "unknown":
        sw_version = f"{sw_version} ({driver_data})"
    
    device_info = {
        "identifiers": [ups_conf["name"]],
        "name": ups_conf["friendly_name"],
        "manufacturer": first_value(ups_data, "device.mfr", "ups.mfr"),
        "model": first_value(ups_data, "device.model", "ups.model"),
        "sw_version": sw_version,
    }

    # ---- UPS Connected binary sensor discovery ----
    # This sensor replaces the old heartbeat and shows UPS reachability
    binary_sensor_entity_id = f"{ups_slug}_connected"  # Use slug for consistency
    binary_discovery_topic = build_discovery_topic(
        binary_sensor_entity_id, platform="binary_sensor"
    )
    binary_discovery_payload = {
        "name": f"{ups_conf['friendly_name']} Connected",
        "state_topic": binary_availability_topic,
        "payload_on": "true",
        "payload_off": "false",
        "device_class": "connectivity",
        "unique_id": binary_sensor_entity_id,
        "device": device_info,
    }
    client.publish(
        binary_discovery_topic, json.dumps(binary_discovery_payload), retain=True
    )

    # ---- Main polling loop ----
    while True:
        ups_data = read_ups(ups_conf["name"])

        if not ups_data:
            # UPS not reachable → mark sensors offline and UPS Connected false
            client.publish(sensor_availability_topic, "offline", retain=True)
            client.publish(binary_availability_topic, "false", retain=True)
        else:
            # UPS reachable → mark sensors online and UPS Connected true
            client.publish(sensor_availability_topic, "online", retain=True)
            client.publish(binary_availability_topic, "true", retain=True)

            # Publish individual UPS sensors
            for sensor in config["sensors"]:
                payload_info = build_payload(
                    sensor, ups_data, device_info, sensor_availability_topic
                )
                if not payload_info:
                    continue

                payload, value = payload_info

                # Title-case the beeper status for nicer display
                if sensor["key"] == "ups.beeper.status" and isinstance(value, str):
                    value = value.title()

                entity_id = payload["unique_id"]
                discovery_topic = build_discovery_topic(entity_id)
                state_topic = build_state_topic(config["mqtt"]["base_topic"], entity_id)

                # Publish sensor discovery (retained)
                client.publish(discovery_topic, json.dumps(payload), retain=True)

                # Publish sensor state if changed
                if last_values.get(entity_id) != value:
                    client.publish(state_topic, value, retain=True)
                    last_values[entity_id] = value

        time.sleep(ups_conf.get("poll_interval", 30))


if __name__ == "__main__":
    main()
