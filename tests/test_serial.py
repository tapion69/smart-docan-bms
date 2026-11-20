"""No-hardware Daren RS485 tests using original documented service 42 example.

Source of sample frame: cpttinkering/daren-485 (MIT License).
"""
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "smart_docan_bms" / "app"))

from daren_serial import (
    DarenSerialError, decode_response, read_request, read_bms,
    parse_serial_addresses, validate_port,
)
from serial_slots import SerialSlots
from logic import normalise_sample, aggregate

# Documented 16S DR-JC03 19200-baud GetDeviceInfo response with checksum D582.
REFERENCE_FRAME = (
    b"~22014A00E0C6001A2C14C0100D010D010CDB0D010D010D000D020CE60CF80D020D010CDF0D020D020D000CE0010E0104010E040104010401040104000000000050011F4014F000CC000000000000000000230000000000000000000000000000000000000000000000D582\r"
)


class SerialProtocolTests(unittest.TestCase):
    def test_frame_only_read_command(self):
        self.assertEqual(read_request(1), b"~22014A42E00201FD28\r")
        for address in (0, 16, -1):
            with self.assertRaises(ValueError):
                read_request(address)

    def test_decode_real_documented_reference(self):
        sample = decode_response(REFERENCE_FRAME, 1)
        self.assertEqual(sample["battery_level"], 67.0)
        self.assertEqual(sample["voltage"], 53.12)
        self.assertEqual(len(sample["cell_voltages"]), 16)
        self.assertEqual(sample["cell_voltages"][2], 3.291)
        self.assertEqual(sample["current"], 0)
        self.assertEqual(sample["battery_health"], 80)
        self.assertEqual(sample["design_capacity"], 80)
        self.assertEqual(sample["cycle_charge"], 53.6)
        self.assertEqual(sample["cycles"], 204)
        self.assertTrue(sample["chrg_mosfet"])
        self.assertTrue(sample["dischrg_mosfet"])
        self.assertFalse(sample["problem"])
        data = normalise_sample(sample)
        self.assertEqual(data["cell_03"], 3.291)
        self.assertEqual(data["voltage"], 53.12)
        self.assertAlmostEqual(data["delta_cell_voltage"], 0.039)
        self.assertEqual(data["stored_energy_wh"], round(53.12 * 80 * .67, 1))

    def test_reject_broken_checksums_and_wrong_address(self):
        with self.assertRaises(DarenSerialError):
            decode_response(REFERENCE_FRAME.replace(b"D582", b"D583"), 1)
        with self.assertRaises(DarenSerialError):
            decode_response(REFERENCE_FRAME, 2)
        with self.assertRaises(DarenSerialError):
            decode_response(b"", 1)
        with self.assertRaises(DarenSerialError):
            decode_response(REFERENCE_FRAME[:-1], 1)

    def test_default_address_probing_and_user_supplied_device(self):
        self.assertEqual(parse_serial_addresses(""), (1, 2, 3, 4))
        self.assertEqual(parse_serial_addresses("1, 3;5,3"), (1, 3, 5))
        for x in ("0", "16", "1,99", "abc"):
            with self.assertRaises(ValueError):
                parse_serial_addresses(x)
        with self.assertRaises(ValueError):
            validate_port("")
        self.assertEqual(validate_port("/dev/serial/by-id/some-unique-device"), "/dev/serial/by-id/some-unique-device")
        with self.assertRaises(ValueError):
            validate_port("/tmp/not-a-serial-device")

    def test_fake_serial_transport_only_sends_a_get_request(self):
        writes = []
        class FakePort:
            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def reset_input_buffer(self):
                return None
            def write(self, command):
                writes.append(command)
            def flush(self):
                pass
            def read_until(self, delimiter, size):
                self_delimiter, self_size = delimiter, size
                assert self_delimiter == b"\r" and self_size == 512
                return REFERENCE_FRAME

        with patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=FakePort)}):
            sample = read_bms("/dev/ttyUSB123", 19200, 1)
        self.assertEqual(sample["battery_level"], 67)
        self.assertEqual(writes, [b"~22014A42E00201FD28\r"])

    def test_persistent_serial_slots_separate_from_ble(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "serial_slots.json"
            slots = SerialSlots(path, {})
            self.assertEqual(slots.bind_detected(3), 1)
            self.assertEqual(slots.bind_detected(1), 2)
            again = SerialSlots(path, {})
            self.assertEqual(again.slot_for(3), 1)
            self.assertEqual(again.slot_for(1), 2)
            self.assertEqual(again.bind_detected(3), 1)
            self.assertEqual(again.bind_detected(2), 3)

    def test_manual_serial_reassignment_and_disabled_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "serial_slots.json"
            initial = SerialSlots(path, {"serial_slot_2_address": "9"})
            self.assertEqual(initial.slot_for(9), 2)
            swapped = SerialSlots(path, {
                "serial_slot_4_address": "9", "slot_4_enabled": True,
                "slot_2_enabled": False,
            })
            self.assertEqual(swapped.slot_for(9), 4)
            self.assertEqual(swapped.inactive(), [2])
            with self.assertRaises(ValueError):
                SerialSlots(path, {"serial_slot_1_address": "3", "serial_slot_3_address": "3"})


if __name__ == "__main__":
    unittest.main()
