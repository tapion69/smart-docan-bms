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
            try:
                payload = json.loads(message.payload)
            except json.JSONDecodeError:
                payload = message.payload.decode()
            received.append((message.topic, payload))
        arrived.set()

    subscriber = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    subscriber.username_pw_set("docan_ci", "ci-testing-password")
    subscriber.on_message = on_message
    subscriber.connect("127.0.0.1", 18884, keepalive=30)
    subscriber.subscribe("homeassistant/#")
    subscriber.subscribe("smart_docan_bms/global/#")
    subscriber.loop_start()
    try:
        bridge.battery({
            "slot": 1, "address": "AA:BB:CC:DD:EE:FF", "name": "Batterie 1",
            "data": {"voltage": 53.2, "battery_level": 42},
            "info": {}, "online": True,
        })
        bridge.summary({"complete": True, "voltage": 53.2, "battery_level": 42,
                        "current": 0, "power": 0, "online_count": 1,
                        "expected_count": 1, "active_slots": "1"})
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
        assert any(item.get("unique_id") == "smart_docan_bms_global_expected_count"
                   for item in configs), "Expected count discovery missing"
        assert any(item.get("unique_id") == "smart_docan_bms_global_active_slots"
                   for item in configs), "Active slot discovery missing"

        # A disconnected BMS must suppress only aggregate values while
        # preserving the fleet count/slot status on MQTT.
        bridge.summary({"complete": False, "online_count": 2,
                        "expected_count": 3, "active_slots": "1,2,3"})
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if all(any(topic == topic_name and data == target for topic, data in received)
                   for topic_name, target in (
                       ("smart_docan_bms/global/measurements_availability", "offline"),
                       ("smart_docan_bms/global/availability", "online"),
                   )) and any(
                       topic == "smart_docan_bms/global/state"
                       and isinstance(data, dict)
                       and data.get("expected_count") == 3
                       and data.get("online_count") == 2
                       and data.get("active_slots") == "1,2,3"
                       for topic, data in received
                   ):
                break
            time.sleep(0.1)
        assert any(topic == "smart_docan_bms/global/measurements_availability"
                   and data == "offline" for topic, data in received), (
            "Partial-fleet measurements were not masked"
        )
        assert any(topic == "smart_docan_bms/global/state" and isinstance(data, dict)
                   and data.get("online_count") == 2 and data.get("expected_count") == 3
                   for topic, data in received), "Offline fleet counts were not published"
        print("Manual MQTT auth + fixed Discovery + dynamic fleet counts + missing-pack safety: PASS")
    finally:
        subscriber.disconnect()
        subscriber.loop_stop()
finally:
    bridge.stop()
