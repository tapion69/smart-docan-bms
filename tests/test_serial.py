"""No-hardware Daren RS485 tests using original documented service 42 example.

Source of sample frame: cpttinkering/daren-485 (MIT License).
"""
import json
import struct
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "smart_docan_bms" / "app"))

from daren_serial import (
    DarenSerialError, decode_response, read_request, read_bms,
    parse_serial_addresses, validate_port, checksum, active_alarm_flags,
)
from serial_slots import SerialSlots
from logic import normalise_sample, aggregate
from discovery import battery_discovery

# Documented 16S DR-JC03 19200-baud GetDeviceInfo response with checksum D582.
REFERENCE_FRAME = (
    b"~22014A00E0C6001A2C14C0100D010D010CDB0D010D010D000D020CE60CF80D020D010CDF0D020D020D000CE0010E0104010E040104010401040104000000000050011F4014F000CC000000000000000000230000000000000000000000000000000000000000000000D582\r"
)


class SerialProtocolTests(unittest.TestCase):
    def test_frame_only_read_command(self):
        self.assertEqual(read_request(1), b"~22014A42E00201FD28\r")
        self.assertEqual(read_request(0), b"~22004A42E00200FD2A\r")
        for address in (16, -1):
            with self.assertRaises(ValueError):
                read_request(address)

    def test_docan_pack0_master_reference_frame_is_addressed_and_validated(self):
        # Re-address the documented service-42 reply to PACK0; retain its
        # telemetry but recompute checksum as a real address-zero reply would.
        core = REFERENCE_FRAME[1:-5].decode("ascii")
        master_core = core[:2] + "00" + core[4:]
        master_reply = (
            "~" + master_core + f"{checksum(master_core):04X}" + "\r"
        ).encode("ascii")
        data = decode_response(master_reply, 0)
        self.assertEqual(data["battery_level"], 67.0)
        self.assertEqual(len(data["cell_voltages"]), 16)
        with self.assertRaisesRegex(DarenSerialError, "autre adresse"):
            decode_response(master_reply, 1)

    def test_existing_three_serial_slaves_keep_fixed_slots_when_master_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "serial_slots.json"
            old = SerialSlots(path, {})
            self.assertEqual(old.bind_detected(1), 1)
            self.assertEqual(old.bind_detected(2), 2)
            self.assertEqual(old.bind_detected(3), 3)

            updated = SerialSlots(path, {})
            self.assertEqual(updated.bind_detected(0), 4)
            self.assertEqual(updated.slot_for(0), 4)
            self.assertEqual(updated.configured(), [(1, 1), (2, 2), (3, 3), (4, 0)])

            restart = SerialSlots(path, {})
            self.assertEqual(restart.configured(), [(1, 1), (2, 2), (3, 3), (4, 0)])
            self.assertEqual(restart.bind_detected(0), 4)

            # The MQTT aggregate now counts the four parallel packs, not three.
            sample = normalise_sample(decode_response(REFERENCE_FRAME, 1))
            records = [
                {"slot": slot, "online": True, "data": sample}
                for slot, _ in restart.configured()
            ]
            total = aggregate(records, len(records))
            self.assertTrue(total["complete"])
            self.assertEqual(total["expected_count"], 4)
            self.assertEqual(total["online_count"], 4)

    def test_explicit_master_mapping_accepts_address_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "serial_slots.json"
            slots = SerialSlots(path, {"serial_slot_1_address": "0"})
            self.assertEqual(slots.slot_for(0), 1)
            self.assertEqual(SerialSlots(path, {}).slot_for(0), 1)

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

    @staticmethod
    def with_flags(voltage=0, current=0, temperature=0, warning=0):
        """Patch the four service 42 flag words in the real example frame.

        Recalculate the original ASCII frame checksum, just as a real BMS would.
        The flags start after the cycle-count word, at binary data byte 66.
        """
        core = REFERENCE_FRAME[1:-5].decode("ascii")
        raw = bytearray.fromhex(core[12:])
        struct.pack_into(">HHHH", raw, 66, voltage, current, temperature, warning)
        updated = core[:12] + raw.hex().upper()
        return ("~" + updated + f"{checksum(updated):04X}" + "\r").encode("ascii")

    def test_raw_current_bit1_not_an_alarm_on_four_batteries(self):
        # Field observation: all four Docan packs report 0000/0002/0000/0000
        # while Bluetooth reports no BMS alarm. Upstream Daren does not
        # classify CURRENT bit 1 as a fault.
        raw_sample = decode_response(self.with_flags(current=0x0002), 1)
        self.assertFalse(raw_sample["problem"])
        self.assertEqual(raw_sample["problem_code"], "0000/0000/0000/0000")
        self.assertEqual(raw_sample["status_code_raw"], "0000/0002/0000/0000")
        normal = normalise_sample(raw_sample)
        self.assertFalse(normal["problem"])
        records = [{"slot": i, "online": True, "data": normal}
                   for i in range(1, 5)]
        global_status = aggregate(records, len(records))
        self.assertTrue(global_status["complete"])
        self.assertFalse(global_status["problem"])
        self.assertEqual(global_status["online_count"], 4)

    def test_alarm_and_raw_status_both_have_stable_mqtt_entities(self):
        alarm_topic, alarm = battery_discovery(1, "Batterie 1", "problem_code", {})
        raw_topic, raw = battery_discovery(1, "Batterie 1", "status_code_raw", {})
        self.assertEqual(alarm["unique_id"], "smart_docan_bms_battery_1_problem_code")
        self.assertEqual(raw["unique_id"], "smart_docan_bms_battery_1_status_code_raw")
        self.assertEqual(alarm_topic, "homeassistant/sensor/smart_docan_bms_battery_1_problem_code/config")
        self.assertEqual(raw_topic, "homeassistant/sensor/smart_docan_bms_battery_1_status_code_raw/config")

    def test_real_protection_bits_still_trigger_alarm(self):
        for field, mask in (
            ("voltage", 0x0002),       # cell undervoltage protection
            ("voltage", 0x2000),       # fuse blown protection
            ("current", 0x0004),       # charge overcurrent protection
            ("current", 0x0008),       # short circuit protection
            ("current", 0x0040),       # charge overcurrent warning
            ("current", 0x0080),       # discharge overcurrent warning
            ("temperature", 0x0001),   # high charge temperature protection
            ("temperature", 0x0100),   # high charge temperature warning
            ("warning", 0x0002),       # documented internal fault bit
            ("warning", 0x0080),       # low SOC warning
        ):
            with self.subTest(register=field, mask=mask):
                response = self.with_flags(**{field: mask})
                self.assertTrue(decode_response(response, 1)["problem"])
        self.assertEqual(active_alarm_flags(0, 2, 0, 0), (0, 0, 0, 0))
        self.assertEqual(active_alarm_flags(0, 0x0006, 0, 0), (0, 4, 0, 0))

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
        self.assertEqual(parse_serial_addresses(""), (0, 1, 2, 3, 4))
        self.assertEqual(parse_serial_addresses("1, 3;5,3"), (1, 3, 5))
        self.assertEqual(parse_serial_addresses("0,3,1,0"), (0, 3, 1))
        for x in ("-1", "16", "1,99", "abc"):
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

    def test_serial_ignores_echo_binary_preamble_and_fragmented_response(self):
        request = read_request(1)
        chunks = [
            bytes((255, 0)) + b"BAD\r", request,
            REFERENCE_FRAME[:17],
            REFERENCE_FRAME[17:91],
            REFERENCE_FRAME[91:],
        ]
        sent = []

        class FakeSerial:
            def __init__(self, *args, **kwargs):
                self.parts = chunks.copy()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def reset_input_buffer(self):
                pass
            def write(self, request_bytes):
                sent.append(request_bytes)
            def flush(self):
                pass
            def read_until(self, delimiter, size):
                return self.parts.pop(0) if self.parts else b""

        with patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=FakeSerial)}):
            sample = read_bms("/dev/ttyUSB123", 19200, 1, timeout=0.25)
        self.assertEqual(sample["battery_level"], 67.0)
        self.assertEqual(sent, [request], "Only one read command may be sent")

    def test_serial_echo_alone_does_not_create_false_bms(self):
        request = read_request(1)

        class FakeSerial:
            def __init__(self, *args, **kwargs):
                self.sent = False
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def reset_input_buffer(self):
                pass
            def write(self, data):
                pass
            def flush(self):
                pass
            def read_until(self, delimiter, size):
                if not self.sent:
                    self.sent = True
                    return request
                return b""

        with patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=FakeSerial)}):
            with self.assertRaisesRegex(DarenSerialError, "échos=1"):
                read_bms("/dev/ttyUSB123", 19200, 1, timeout=0.04)

    def test_serial_reports_binary_noise_without_exposing_raw_traffic(self):
        class FakeSerial:
            def __init__(self, *args, **kwargs):
                self.sent = False
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def reset_input_buffer(self):
                pass
            def write(self, data):
                pass
            def flush(self):
                pass
            def read_until(self, delimiter, size):
                if not self.sent:
                    self.sent = True
                    return bytes((128, 255, 0, 13))
                return b""

        with patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=FakeSerial)}):
            with self.assertRaisesRegex(DarenSerialError, "octets_non_ASCII=3"):
                read_bms("/dev/ttyUSB123", 19200, 1, timeout=0.04)

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
