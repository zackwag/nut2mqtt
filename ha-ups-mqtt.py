#!/usr/bin/env python3
import json
import subprocess
import time

import paho.mqtt.client as mqtt
import yaml

CONFIG_FILE = "config.yaml"

def load_config():
    with open(CONFIG_FILE, "r") as f:
        return yaml.safe_load(f)

def read_ups(ups_name):
    """Call upsc and return a dict of key/value pairs."""
    result = subprocess.run(["upsc", ups_name], capture_output=True, text=True)
    if result.returncode != 0:
        return {}
    data = {}
    for line in result.stdout.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            data[key.strip()] = val.strip()
    return data

def build_discovery_topic(entity_id):
    return f"homeassistant/sensor/{entity_id}/config"

def build_state_topic(base_topic, entity_id):
    return f"{base_topic}/{entity_id}/state"

def make_entity_id(device_name, key):
    base = device_name.lower().replace(" ", "_")
    key_clean = key.lower().replace(".", "_")

    if key_clean.startswith(base + "_"):
        key_clean = key_clean[len(base) + 1 :]

    return f"{base}_{key_clean}"

def build_payload(sensor, ups_data, device_info):
    key = sensor["key"]
    value = ups_data.get(key)
    if value is None:
        return None

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
    }

    if "unit" in sensor:
        payload["unit_of_measurement"] = sensor["unit"]
    if "icon" in sensor:
        payload["icon"] = sensor["icon"]
    if "device_class" in sensor:
        payload["device_class"] = sensor["device_class"]

    return payload, value

def main():
    global config
    config = load_config()

    mqtt_conf = config["mqtt"]
    ups_conf = config["ups"]

    ups_data = read_ups(ups_conf["name"])

    device_info = {
        "identifiers": [ups_conf["name"]],
        "name": ups_conf["friendly_name"],
        "model": ups_data.get("ups.model", "unknown"),
        "manufacturer": ups_data.get("ups.mfr", "unknown"),
        "sw_version": ups_conf.get("sw_version", "nut-upsc-bridge-1"),
    }

    client = mqtt.Client(
        client_id=mqtt_conf.get("client_id", ""),
        protocol=mqtt.MQTTv311,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )

    if mqtt_conf.get("username") and mqtt_conf.get("password"):
        client.username_pw_set(mqtt_conf["username"], mqtt_conf["password"])

    client.connect(mqtt_conf["broker"], mqtt_conf["port"], 60)
    client.loop_start()

    last_values = {}

    # ---- Heartbeat setup (static) ----
    heartbeat_entity_id = "den_ups_heartbeat"
    heartbeat_discovery_topic = build_discovery_topic(heartbeat_entity_id)
    heartbeat_state_topic = build_state_topic(
        mqtt_conf["base_topic"], heartbeat_entity_id
    )

    heartbeat_discovery_payload = {
        "name": f"{device_info['name']} Heartbeat",
        "state_topic": heartbeat_state_topic,
        "unique_id": heartbeat_entity_id,
        "device": device_info,
        "icon": "mdi:heart-pulse",
    }

    # Publish heartbeat discovery once (retained)
    client.publish(
        heartbeat_discovery_topic,
        json.dumps(heartbeat_discovery_payload),
        retain=True,
    )

    while True:
        ups_data = read_ups(ups_conf["name"])

        # Publish UPS sensors
        for sensor in config["sensors"]:
            payload_info = build_payload(sensor, ups_data, device_info)
            if not payload_info:
                continue

            payload, value = payload_info
            entity_id = payload["unique_id"]

            discovery_topic = build_discovery_topic(entity_id)
            state_topic = build_state_topic(
                mqtt_conf["base_topic"], entity_id
            )

            client.publish(discovery_topic, json.dumps(payload), retain=True)

            if last_values.get(entity_id) != value:
                client.publish(state_topic, value, retain=True)
                last_values[entity_id] = value

        # Publish heartbeat every loop (epoch seconds)
        heartbeat_value = str(int(time.time()))
        client.publish(
            heartbeat_state_topic,
            heartbeat_value,
            retain=True,
        )

        time.sleep(ups_conf.get("poll_interval", 30))

if __name__ == "__main__":
    main()
