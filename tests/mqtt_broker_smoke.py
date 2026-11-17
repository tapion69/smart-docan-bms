"""CI-only live Mosquitto smoke test with username/password authentication.

No Home Assistant, Bluetooth hardware or real battery identifiers needed.
"""
import json
import sys
import threading
import time

sys.path.insert(0, "/app")

import paho.mqtt.client as mqtt
from run import MQTTBridge, Store
from settings import mqtt_settings

opts = {
    "mqtt_mode": "manual",
    "mqtt_host": "127.0.0.1",
    "mqtt_port": 18884,
    "mqtt_username": "docan_ci",
    "mqtt_password": "ci-testing-password",
    "mqtt_tls": False,
}
configuration = mqtt_settings(opts)
store = Store()
bridge = MQTTBridge(configuration, store)

try:
    for _ in range(80):
        if store.mqtt_connected:
            break
        time.sleep(0.15)
    assert store.mqtt_connected, f"Manual MQTT failed: {store.mqtt_error}"

    received = []
    arrived = threading.Event()

    def on_message(client, userdata, message):
        if not message.payload:
            received.append((message.topic, None))  # retained legacy config deletion
        else:
            received.append((message.topic, json.loads(message.payload)))
        arrived.set()

    subscriber = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    subscriber.username_pw_set("docan_ci", "ci-testing-password")
    subscriber.on_message = on_message
    subscriber.connect("127.0.0.1", 18884, keepalive=30)
    subscriber.subscribe("homeassistant/#")
    subscriber.loop_start()
    try:
        bridge.battery({
            "slot": 1, "address": "AA:BB:CC:DD:EE:FF", "name": "Batterie 1",
            "data": {"voltage": 53.2, "battery_level": 42},
            "info": {}, "online": True,
        })
        bridge.summary({"complete": True, "voltage": 53.2, "battery_level": 42,
                        "current": 0, "power": 0, "online_count": 1})
        assert arrived.wait(10), "MQTT Discovery config was not received"
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if all(any(topic == expected for topic, _ in received) for expected in (
                "homeassistant/sensor/smart_docan_bms_battery_1_voltage/config",
                "homeassistant/sensor/smart_docan_bms_global_soc/config",
                "homeassistant/sensor/docan_aabbccddeeff_voltage/config",
            )):
                break
            time.sleep(0.1)
        configs = [item for _, item in received if isinstance(item, dict)]
        assert any(item.get("unique_id") == "smart_docan_bms_battery_1_voltage"
                   and item.get("default_entity_id") == "sensor.smart_docan_bms_battery_1_voltage"
                   for item in configs), "Fixed per-slot MQTT Discovery not received"
        assert any(item.get("unique_id") == "smart_docan_bms_global_soc"
                   for item in configs), "Fixed global MQTT Discovery not received"
        assert any(topic == "homeassistant/sensor/docan_aabbccddeeff_voltage/config"
                   and data is None for topic, data in received), "Retained legacy cleanup not published"
        print("Manual MQTT auth + fixed slot/global Discovery + MAC migration: PASS")
    finally:
        subscriber.disconnect()
        subscriber.loop_stop()
finally:
    bridge.stop()
