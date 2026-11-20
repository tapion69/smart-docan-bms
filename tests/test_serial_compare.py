"""Dual BLE/reference vs passive RS485 probing unit tests (no hardware).

The RS485 v23/85 payload layout is not yet documented. These tests only
prove candidate scanning and ambiguity rejection, not actual field offsets.
"""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "smart_docan_bms" / "app"))

from serial_compare import (
    ble_cell_profiles, cell_windows, scalar_offsets, compare_payloads,
    summarize_finding,
)
from daren_serial import PassiveSerialMonitor, checksum, length_checksum


class DualSerialCompareTests(unittest.TestCase):
    @staticmethod
    def ble_record(slot, cells, soc=39.87, voltage=None, age=0, online=True):
        when = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
        if voltage is None:
            voltage = round(sum(cells), 2)
        return {
            "slot": slot,
            "online": online,
            "last_seen": when,
            "data": {
                **{f"cell_{i:02d}": v for i, v in enumerate(cells, 1)},
                "battery_level": soc,
                "voltage": voltage,
                "last_seen": when,
            },
        }

    @staticmethod
    def pattern_a():
        return tuple(3.275 + (i * 7 % 17) * .001 for i in range(16))

    @staticmethod
    def pattern_b():
        return tuple(3.300 + (i * 9 % 19) * .001 for i in range(16))

    @staticmethod
    def pack_payload(cells, offset=30, endian="big", soc=39.87):
        raw = bytearray([0xFA] * 98)
        raw[8:10] = int(round(soc * 100)).to_bytes(2, endian)
        raw[12:14] = int(round(sum(cells)*100)).to_bytes(2, endian)
        for i, value in enumerate(cells):
            raw[offset+2*i:offset+2*i+2] = int(round(value*1000)).to_bytes(2, endian)
        return bytes(raw)

    @staticmethod
    def frame(address, payload):
        core = (f"23{address:02X}4A85"
                f"{length_checksum(len(payload)*2):04X}"
                + payload.hex().upper())
        return f"~{core}{checksum(core):04X}\r".encode("ascii")

    def test_ble_profiles_require_recent_online_16_cells(self):
        cells = self.pattern_a()
        valid = self.ble_record(3, cells)
        stale = self.ble_record(2, cells, age=190)
        offline = self.ble_record(1, cells, online=False)
        incomplete = self.ble_record(4, cells)
        del incomplete["data"]["cell_09"]
        found = ble_cell_profiles([valid, stale, offline, incomplete])
        self.assertEqual(set(found), {3})
        self.assertEqual(found[3]["cells"], cells)

    def test_detect_cell_array_at_unknown_offset_or_byte_order(self):
        cells = self.pattern_a()
        for order in ("big", "little"):
            payload = self.pack_payload(cells, offset=21, endian=order)
            windows = cell_windows(payload)
            self.assertTrue(any(
                off == 21 and endian == order and all(
                    abs(x-y) < .00001 for x,y in zip(vec,cells)
                ) for off,endian,vec in windows
            ))

    def test_match_battery_identity_by_cells_not_address_or_slot_order(self):
        a, b = self.pattern_a(), self.pattern_b()
        ble = ble_cell_profiles([
            self.ble_record(1, b, soc=42.1),
            self.ble_record(3, a, soc=39.87),
        ])
        # RS485 device address 1 is BLE slot 3, not BLE slot 1.
        raw = self.pack_payload(a, offset=25, soc=39.87)
        found = compare_payloads({1: raw}, ble)
        self.assertEqual(len(found), 1)
        row = found[0]
        self.assertEqual(row["status"], "candidate")
        self.assertEqual(row["address"], 1)
        self.assertEqual(row["ble_slot"], 3)
        self.assertEqual(row["cell_offset"], 25)
        self.assertEqual(row["endian"], "big")
        self.assertIn(8, row["soc_offsets"])
        self.assertIn(12, row["pack_voltage_offsets"])
        self.assertIn("candidat BLE batterie 3", summarize_finding(row))

    def test_identical_ble_cell_patterns_are_ambiguous(self):
        a = self.pattern_a()
        ble = ble_cell_profiles([
            self.ble_record(1, a), self.ble_record(4, a),
        ])
        result = compare_payloads({2: self.pack_payload(a)}, ble)[0]
        self.assertEqual(result["status"], "ambiguous")
        self.assertIn("AMBIGUË", summarize_finding(result))

    def test_two_serial_addresses_cannot_claim_one_ble_device(self):
        a = self.pattern_a()
        ble = ble_cell_profiles([self.ble_record(4, a)])
        result = compare_payloads({
            1: self.pack_payload(a),
            2: self.pack_payload(a),
        }, ble)
        self.assertEqual([r["status"] for r in result], ["ambiguous", "ambiguous"])

    def test_random_or_outdated_payload_cannot_create_match(self):
        ble = ble_cell_profiles([self.ble_record(1, self.pattern_a())])
        self.assertEqual(
            compare_payloads({1: b"\xff" * 98}, ble)[0]["status"],
            "no_cell_pattern",
        )
        self.assertEqual(compare_payloads({1: b"\x00" * 40}, ble), [])

    def test_master_frame_capture_keeps_v23_bytes_in_memory_only(self):
        a = self.pattern_a()
        payload = self.pack_payload(a)
        reply = self.frame(7, payload)
        frames = [reply[:36], reply[36:]]
        sent = []

        class FakeRxOnly:
            def __init__(self, *args, **kwargs):
                self.fragments = frames.copy()
            def read_until(self, delimiter, size):
                return self.fragments.pop(0) if self.fragments else b""
            def write(self, arg):
                sent.append(arg)
                raise AssertionError("RS485 listening must never transmit")
            def reset_input_buffer(self):
                raise AssertionError("RX buffer must not be reset")
            def flush(self):
                raise AssertionError("RX monitor must not flush")
            def close(self):
                pass

        with patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=FakeRxOnly)}):
            monitor = PassiveSerialMonitor("/dev/ttyUSB123", 9600)
            monitor.open()
            _, stats = monitor.read_window(0.05, True)
            monitor.close()
        self.assertEqual(stats["probe_payloads"], {7: payload})
        self.assertEqual(sent, [])
        self.assertGreaterEqual(stats["frames"], 1)
        # "No recognized telemetry" may still be true; acquisition of 98 raw
        # bytes for private comparison must not imply BMS online/MQTT publish.

    def test_checksum_invalid_v23_never_used_as_candidate(self):
        payload = self.pack_payload(self.pattern_a())
        bad = self.frame(3, payload)
        bad = bad[:-5] + b"0000\r"

        class FakeRxOnly:
            def __init__(self, *args, **kwargs):
                self.parts = [bad]
            def read_until(self, delimiter, size):
                return self.parts.pop(0) if self.parts else b""
            def close(self):
                pass

        with patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=FakeRxOnly)}):
            monitor = PassiveSerialMonitor("/dev/ttyUSB123", 9600)
            monitor.open()
            _, stats = monitor.read_window(0.03, True)
            monitor.close()
        self.assertEqual(stats["probe_payloads"], {})


if __name__ == "__main__":
    unittest.main()
