"""Offline tests: no BLE hardware, MQTT broker or Home Assistant required."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "smart_docan_bms" / "app"))

from logic import aggregate, device_id, field_spec, normalise_sample
from dashboard import render
from slots import SlotAssignments, slot_entity_id, global_entity_id
from discovery import battery_discovery, global_discovery, legacy_battery_topics, legacy_global_topics
import tempfile
from settings import mqtt_settings, remaining_poll_delay

class LogicTests(unittest.TestCase):
    def test_cells_all_available(self):
        cells = [3.311] * 16
        cells[6] = 3.314
        d = normalise_sample({"cell_voltages": cells, "battery_level": 36,
                              "voltage": 53.0, "design_capacity": 329})
        self.assertEqual(len([k for k in d if k.startswith("cell_") and k[5:].isdigit()]), 16)
        self.assertEqual(d["cell_max_index"], 7)
        self.assertEqual(d["cell_min_index"], 1)
        self.assertAlmostEqual(d["delta_cell_voltage"], .003)
        self.assertAlmostEqual(d["stored_energy_wh"], 6277.3, places=1)

    def test_temperature_sensor(self):
        class Temp:
            value = 17.3
        d = normalise_sample({"temperature": [Temp(), 18.1]})
        self.assertAlmostEqual(d["temperature"], 17.7)
        self.assertEqual(d["temp_01"], 17.3)

    def test_v033_daren_temperature_schema(self):
        class Probe:
            def __init__(self, value, name):
                self.value = value
                self.type = type("Type", (), {"name": name})()

        sample = {
            "temp_sensors": 3,
            "temp_values": [Probe(16.1, "CELL"), Probe(18.2, "CELL"),
                            Probe(21.1, "MOSFET")],
            "temperature": 17.15,
            "delta_voltage": 0.002,
        }
        out = normalise_sample(sample)
        self.assertEqual(out["temp_count"], 3)
        self.assertEqual(out["temp_01"], 16.1)
        self.assertEqual(out["temp_02"], 18.2)
        self.assertEqual(out["temp_03"], 21.1)
        self.assertEqual(out["temp_type_03"], "MOSFET")
        self.assertEqual(out["temperature"], 17.15)
        self.assertEqual(out["delta_cell_voltage"], 0.002)

    def test_parallel_weighted_aggregate(self):
        sample1 = {"battery_level": 40, "design_capacity": 200, "voltage": 52, "current": -4, "power": -208}
        sample2 = {"battery_level": 20, "design_capacity": 300, "voltage": 52.4, "current": -6, "power": -314.4}
        out = aggregate([{"online": True, "data": sample1}, {"online": True, "data": sample2}], 2)
        self.assertTrue(out["complete"])
        self.assertEqual(out["battery_level"], 28)
        self.assertEqual(out["current"], -10)
        self.assertEqual(out["power"], -522.4)
        self.assertAlmostEqual(out["voltage"], 52.2)

    def test_no_partial_sum(self):
        result = aggregate([{"online": True, "data": {"current": 5}},
                            {"online": False, "data": {"current": 10}}], 2)
        self.assertFalse(result["complete"])
        self.assertNotIn("current", result)

    def test_stable_device_identity(self):
        self.assertEqual(device_id("AA:BB:CC:DD:EE:FF"), "docan_aabbccddeeff")
        self.assertEqual(field_spec("cell_16")[0], "Tension cellule 16")
        self.assertIsNone(field_spec("temp_type_01"))

    def test_logical_ids_independent_of_mac(self):
        for mac in ("AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"):
            with tempfile.TemporaryDirectory() as folder:
                mapping = SlotAssignments(Path(folder) / "slots.json", {})
                mapping.bind_discovered([mac])
                self.assertEqual(mapping.slot_for(mac), 1)
                topic, entity = battery_discovery(1, "Batterie 1", "battery_level", {})
                self.assertEqual(topic, "homeassistant/sensor/smart_docan_bms_battery_1_soc/config")
                self.assertEqual(entity["unique_id"], "smart_docan_bms_battery_1_soc")
                self.assertEqual(entity["default_entity_id"], "sensor.smart_docan_bms_battery_1_soc")
                self.assertEqual(entity["state_topic"], "smart_docan_bms/smart_docan_bms_battery_1/state")
                self.assertNotIn(mac.replace(":", "").lower(), str(entity))

    def test_persistent_slots_not_reassigned_after_restart(self):
        a, b, c = "11:22:33:44:55:66", "22:33:44:55:66:77", "33:44:55:66:77:88"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "slots.json"
            slots = SlotAssignments(path, {})
            self.assertEqual(slots.bind_discovered([b, a]), {a: 1, b: 2})
            reload = SlotAssignments(path, {})
            self.assertEqual(reload.slot_for(b), 2)
            self.assertEqual(reload.bind_discovered([c]), {c: 3})
            self.assertEqual(reload.slot_for(a), 1)
            self.assertEqual(SlotAssignments(path, {}).slot_for(b), 2)

    def test_strict_slots_no_unassigned_devices(self):
        a, b = "11:22:33:44:55:66", "22:33:44:55:66:77"
        with tempfile.TemporaryDirectory() as folder:
            slots = SlotAssignments(Path(folder) / "slots.json",
                                    {"strict_slots": True, "slot_3_address": a})
            self.assertEqual(slots.bind_discovered([a, b]), {a: 3})
            self.assertEqual(slots.slot_for(b), None)
            self.assertEqual(slots.get_name(3), "Batterie 3")

    def test_slots_explicit_override_and_duplicate_rejected(self):
        a, b = "11:22:33:44:55:66", "22:33:44:55:66:77"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "slots.json"
            SlotAssignments(path, {"slot_1_address": a})
            updated = SlotAssignments(path, {"slot_1_address": b})
            self.assertEqual(updated.slot_for(b), 1)
            self.assertEqual(updated.slot_for(a), None)
            with self.assertRaisesRegex(ValueError, "deux batteries"):
                SlotAssignments(path, {"slot_2_address": b})

    def test_global_and_binary_unique_ids(self):
        topic, cfg = global_discovery("battery_level")
        self.assertEqual(topic, "homeassistant/sensor/smart_docan_bms_global_soc/config")
        self.assertEqual(cfg["unique_id"], "smart_docan_bms_global_soc")
        self.assertEqual(cfg["default_entity_id"], "sensor.smart_docan_bms_global_soc")
        topic2, cfg2 = battery_discovery(4, "Batterie 4", "problem", {})
        self.assertEqual(topic2, "homeassistant/binary_sensor/smart_docan_bms_battery_4_problem/config")
        self.assertEqual(cfg2["default_entity_id"], "binary_sensor.smart_docan_bms_battery_4_problem")
        self.assertEqual(cfg2["unique_id"], "smart_docan_bms_battery_4_problem")

    def test_legacy_cleanup_targets_only_old_ids(self):
        topics = legacy_battery_topics("AA:BB:CC:DD:EE:FF")
        self.assertIn("homeassistant/sensor/docan_aabbccddeeff_battery_level/config", topics)
        self.assertIn("homeassistant/sensor/docan_aabbccddeeff_cell_16/config", topics)
        self.assertIn("homeassistant/binary_sensor/docan_aabbccddeeff_problem/config", topics)
        self.assertNotIn("homeassistant/sensor/smart_docan_bms_battery_1_soc/config", topics)
        self.assertIn("homeassistant/sensor/docan_aggregate_battery_level/config", legacy_global_topics())

    def test_mqtt_manual_uses_explicit_credentials(self):
        opts = {"mqtt_mode": "manual", "mqtt_host": "core-mosquitto",
                "mqtt_port": 1883, "mqtt_username": "docan_user",
                "mqtt_password": "a-private-example", "mqtt_tls": False}
        service = {"host": "other-broker", "port": 7777, "username": "from_supervisor"}
        conf = mqtt_settings(opts, service)
        self.assertEqual(conf["host"], "core-mosquitto")
        self.assertEqual(conf["port"], 1883)
        self.assertEqual(conf["username"], "docan_user")
        self.assertEqual(conf["password"], "a-private-example")
        self.assertEqual(conf["mode"], "manual")

    def test_mqtt_auto_keeps_supervisor_credentials(self):
        opts = {"mqtt_mode": "auto", "mqtt_host": "ignored",
                "mqtt_username": "ignored", "mqtt_password": "ignored"}
        service = {"host": "core-mosquitto", "port": 1883, "username": "auto_user",
                   "password": "auto_password", "ssl": False}
        conf = mqtt_settings(opts, service)
        self.assertEqual(conf["username"], "auto_user")
        self.assertEqual(conf["password"], "auto_password")
        self.assertEqual(conf["mode"], "auto")

    def test_mqtt_manual_configuration_is_validated(self):
        with self.assertRaisesRegex(ValueError, "mqtt_host"):
            mqtt_settings({"mqtt_mode": "manual", "mqtt_host": ""})
        with self.assertRaisesRegex(ValueError, "mqtt_username"):
            mqtt_settings({"mqtt_mode": "manual", "mqtt_host": "local",
                           "mqtt_username": "", "mqtt_password": "secret"})
        with self.assertRaisesRegex(ValueError, "mqtt_port"):
            mqtt_settings({"mqtt_mode": "manual", "mqtt_host": "local", "mqtt_port": 0})
        with self.assertRaisesRegex(ValueError, "Supervisor"):
            mqtt_settings({"mqtt_mode": "auto"}, None)

    def test_poll_period_includes_the_four_bms_read_duration(self):
        self.assertEqual(remaining_poll_delay(0, 30), 30)
        self.assertEqual(remaining_poll_delay(12.0, 30), 18.0)
        self.assertEqual(remaining_poll_delay(36.0, 30), 0.0)
        self.assertEqual(remaining_poll_delay(-1.0, 30), 30.0)

    def test_dashboard_refresh_and_polling_are_independent(self):
        html = render({
            "batteries": [], "aggregate": {"expected_count": 0, "online_count": 0},
            "mqtt_connected": False, "poll_seconds": 45,
            "dashboard_refresh_seconds": 11, "cycle_duration_seconds": 8.4,
            "last_cycle_period_seconds": 45.5,
        })
        self.assertIn('http-equiv="refresh" content="11"', html)
        self.assertIn("cible 45 s", html)
        self.assertIn("8.4 s", html)
        self.assertIn("45.5 s", html)

    def test_render_offline_mqtt_without_exposing_secrets(self):
        html = render({
            "batteries": [{"name": "Batterie A", "address": "00:00:00:00:00:01",
                          "online": True, "data": {"battery_level": 35, "cell_01": 3.3},
                          "temperature_diagnostic": "Sondes annoncées: 4; données reçues: 0"}],
            "aggregate": {"online_count": 1, "expected_count": 1, "complete": True},
            "mqtt_connected": False, "mqtt_target": "core-mosquitto:1883",
            "mqtt_error": "Connexion TCP impossible", "mqtt_failures": 4,
        })
        self.assertIn("MQTT non connecté", html)
        self.assertIn("core-mosquitto:1883", html)
        self.assertIn("Connexion TCP impossible", html)
        self.assertIn("Sondes annoncées: 4", html)
        self.assertIn("Cellule 1", html)
        self.assertIn("Autres informations disponibles", html)

    def test_render_html_escapes_bms_data(self):
        html = render({
            "batteries": [{"name": "<script>alert(1)</script>", "online": False,
                           "data": {}, "temperature_diagnostic": "<bad>"}],
            "aggregate": {"online_count": 0, "expected_count": 1, "complete": False},
            "mqtt_connected": True,
        })
        self.assertNotIn("<script>", html)
        self.assertNotIn("<bad>", html)
        self.assertIn("&lt;script&gt;", html)

if __name__ == "__main__":
    unittest.main()
