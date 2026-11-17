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
        received.append((message.topic, json.loads(message.payload)))
        arrived.set()

    subscriber = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    subscriber.username_pw_set("docan_ci", "ci-testing-password")
    subscriber.on_message = on_message
    subscriber.connect("127.0.0.1", 18884, keepalive=30)
    subscriber.subscribe("homeassistant/sensor/#")
    subscriber.loop_start()
    try:
        bridge.entity("AA:BB:CC:DD:EE:FF", "Battery CI", "voltage",
                      {"voltage": 53.2}, {})
        assert arrived.wait(10), "MQTT Discovery config was not received"
        assert any(item.get("unique_id") == "smart_docan_aabbccddeeff_voltage"
                   for _, item in received), f"Unexpected MQTT data: {received}"
        print("Manual MQTT username/password handshake + Discovery: PASS")
    finally:
        subscriber.disconnect()
        subscriber.loop_stop()
finally:
    bridge.stop()
