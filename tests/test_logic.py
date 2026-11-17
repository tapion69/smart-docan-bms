"""Offline tests: no BLE hardware, MQTT broker or Home Assistant required."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "smart_docan_bms" / "app"))

from logic import aggregate, device_id, field_spec, normalise_sample

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
        self.assertEqual(device_id("68:09:47:41:81:BE"), "docan_6809474181be")
        self.assertEqual(field_spec("cell_16")[0], "Tension cellule 16")
        self.assertIsNone(field_spec("temp_type_01"))

if __name__ == "__main__":
    unittest.main()
