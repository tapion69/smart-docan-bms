"""Offline tests: no BLE hardware, MQTT broker or Home Assistant required."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "smart_docan_bms" / "app"))

from logic import aggregate, device_id, field_spec, normalise_sample, voltage_decimals, format_runtime
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

    def test_auto_aggregate_for_one_to_four_configured_batteries(self):
        for n in range(1, 5):
            records = [
                {"slot": i, "online": True, "data": {
                    "battery_level": 50, "voltage": 52.5,
                    "current": -2, "power": -105,
                    "stored_energy_wh": 9000, "design_capacity": 300,
                }} for i in range(1, n + 1)
            ]
            g = aggregate(records, len(records))
            self.assertTrue(g["complete"])
            self.assertEqual(g["expected_count"], n)
            self.assertEqual(g["online_count"], n)
            self.assertEqual(g["active_slots"], ",".join(map(str, range(1, n + 1))))
            self.assertEqual(g["power"], -105 * n)
            self.assertEqual(g["stored_energy_wh"], 9000 * n)
            self.assertEqual(g["battery_level"], 50)

    def test_offline_slot_remains_in_expected_count_and_hides_totals(self):
        records = [
            {"slot": i, "online": i != 2,
             "data": {"battery_level": 50, "current": -1, "power": -52}}
            for i in (1, 2, 4)
        ]
        g = aggregate(records, 3)
        self.assertEqual(g["active_slots"], "1,2,4")
        self.assertEqual(g["online_count"], 2)
        self.assertEqual(g["expected_count"], 3)
        self.assertFalse(g["complete"])
        self.assertNotIn("power", g)
        self.assertNotIn("battery_level", g)

    def test_disabled_slot_not_auto_assigned_and_reenable_preserves_address(self):
        a, b, c, d = (
            "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02",
            "AA:BB:CC:DD:EE:03", "AA:BB:CC:DD:EE:04",
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "slots.json"
            assignment = SlotAssignments(path, {})
            assignment.bind_discovered([a, b, c, d])
            three = SlotAssignments(path, {"slot_4_enabled": False})
            self.assertEqual([i for i, _ in three.configured()], [1, 2, 3])
            self.assertEqual(three.disabled(), [(4, d)])
            self.assertIsNone(three.slot_for(d))
            self.assertEqual(three.bind_discovered([a, b, c, d]), {a: 1, b: 2, c: 3})
            self.assertEqual(three.slots[4], d)
            again = SlotAssignments(path, {"slot_4_enabled": True})
            self.assertEqual(again.slot_for(d), 4)

    def test_auto_discovery_respects_disabled_empty_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            slots = SlotAssignments(Path(folder) / "slots.json", {"slot_2_enabled": False})
            a, b = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"
            self.assertEqual(slots.bind_discovered([a, b]), {a: 1, b: 3})
            self.assertEqual([i for i, _ in slots.configured()], [1, 3])

    def test_new_global_discovery_counters_not_masked_when_partial(self):
        for key in ("online_count", "expected_count", "active_slots"):
            _, config = global_discovery(key)
            self.assertEqual(config["availability_topic"],
                             "smart_docan_bms/global/availability")
            self.assertEqual(config["unique_id"], f"smart_docan_bms_global_{key}")
        for key in ("battery_level", "current", "power", "stored_energy_wh"):
            _, config = global_discovery(key)
            self.assertEqual(config["availability_topic"],
                             "smart_docan_bms/global/measurements_availability")
        self.assertEqual(
            global_discovery("battery_level")[1]["unique_id"],
            "smart_docan_bms_global_soc",
        )

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
            switched = SlotAssignments(path, {"slot_2_address": b})
            self.assertEqual(switched.slot_for(b), 2)
            self.assertNotIn(1, dict(switched.configured()))
            with self.assertRaisesRegex(ValueError, "deux batteries"):
                SlotAssignments(path, {"slot_1_address": b, "slot_2_address": b})

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

    def test_voltage_precision_by_measurement_type(self):
        self.assertEqual(voltage_decimals("voltage"), 2)
        for key in ("cell_01", "cell_16", "cell_min_voltage",
                    "cell_max_voltage", "delta_cell_voltage", "voltage_spread"):
            self.assertEqual(voltage_decimals(key), 3)
        self.assertIsNone(voltage_decimals("current"))

        sample = {
            "voltage": 53.1267,
            "cell_voltages": [3.3, 3.3026] + [3.3014] * 14,
            "delta_voltage": 0.0026,
            "runtime": 3725,
        }
        data = normalise_sample(sample)
        self.assertEqual(data["voltage"], 53.13)
        self.assertEqual(data["cell_01"], 3.300)
        self.assertEqual(data["cell_02"], 3.303)
        self.assertEqual(data["cell_min_voltage"], 3.300)
        self.assertEqual(data["cell_max_voltage"], 3.303)
        self.assertEqual(data["delta_cell_voltage"], 0.003)
        self.assertEqual(data["runtime"], 3725)
        self.assertEqual(data["runtime_text"], "1 h 2 min")

    def test_autonomy_duration_minutes_hours_days_and_unknown(self):
        for secs, text in (
            (0, "0 s"), (30, "30 s"), (60, "1 min"),
            (3599, "59 min"), (3600, "1 h"), (86400, "1 j"),
            (90061, "1 j 1 h 1 min"), (3 * 86400 + 50, "3 j"),
        ):
            self.assertEqual(format_runtime(secs), text)
        for seconds in (None, False, -10, float("nan"), float("inf")):
            self.assertIsNone(format_runtime(seconds))
        self.assertNotIn("runtime_text", normalise_sample({"voltage": 53.12}))

    def test_mqtt_entity_precision_and_fixed_ids(self):
        for field, precision in (
            ("voltage", 2), ("cell_01", 3), ("cell_min_voltage", 3),
            ("cell_max_voltage", 3), ("delta_cell_voltage", 3),
        ):
            topic, cfg = battery_discovery(1, "Batterie 1", field, {})
            self.assertEqual(cfg["suggested_display_precision"], precision)
            self.assertIn(f"%.{precision}f", cfg["value_template"])
            self.assertIn("smart_docan_bms_battery_1_", cfg["unique_id"])
        for key in ("voltage", "voltage_spread"):
            topic, cfg = global_discovery(key)
            self.assertEqual(cfg["suggested_display_precision"], 3)
        topic, cfg = battery_discovery(1, "Batterie 1", "runtime_text", {})
        self.assertEqual(cfg["unique_id"], "smart_docan_bms_battery_1_runtime_text")
        self.assertNotIn("unit_of_measurement", cfg)
        topic, cfg = battery_discovery(1, "Batterie 1", "runtime", {})
        self.assertEqual(cfg["unique_id"], "smart_docan_bms_battery_1_runtime")
        self.assertEqual(cfg["unit_of_measurement"], "s")

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

if __name__ == "__main__":
    unittest.main()
