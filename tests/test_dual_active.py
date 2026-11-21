"""No-hardware tests of the opt-in external-port diagnostic."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "smart_docan_bms" / "app"))

from dual_active import probe_cycle, compare_sample
from daren_serial import read_request, checksum, length_checksum
from test_serial import REFERENCE_FRAME


class FakePort:
    def __init__(self, *, preflight=b"", reply=None):
        self.now = 0.0
        self.writes = []
        self.preflight = preflight
        self.reply = reply or (lambda request: b"")
        self.pending = b""
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    @property
    def in_waiting(self):
        return len(self.pending)

    def read(self, size):
        self.now += 0.2
        if self.preflight:
            data, self.preflight = self.preflight, b""
            return data
        data, self.pending = self.pending[:size], self.pending[size:]
        return data

    def write(self, data):
        self.writes.append(data)
        self.pending += self.reply(data)
        return len(data)

    def flush(self):
        pass

    def probe(self, addresses=(0,1,2,3)):
        return probe_cycle("/dev/fake", 9600, addresses,
                           serial_factory=lambda *a, **kw: self,
                           clock=lambda: self.now)


def frame_at(address):
    core = REFERENCE_FRAME[1:-5].decode("ascii")
    core = core[:2] + f"{address:02X}" + core[4:]
    return f"~{core}{checksum(core):04X}\r".encode("ascii")


class ActiveDiagnosticTests(unittest.TestCase):
    def test_busy_bus_never_transmits(self):
        port = FakePort(preflight=b"\x85\x01")
        result = port.probe()
        self.assertTrue(result["busy"])
        self.assertEqual(result["preflight_bytes"], 2)
        self.assertEqual(port.writes, [])
        self.assertTrue(port.closed)

    def test_only_read_requests_and_address_zero_is_decoded(self):
        port = FakePort(reply=lambda req: req + frame_at(0) if req == read_request(0) else req)
        result = port.probe()
        self.assertFalse(result["busy"])
        self.assertEqual(port.writes, [read_request(a) for a in range(4)])
        self.assertEqual(result["queries"][0]["sample"]["battery_level"], 67)
        self.assertEqual(result["queries"][0]["echoes"], 1)
        self.assertIsNone(result["queries"][1]["sample"])
        self.assertTrue(port.closed)

    def test_binary_and_wrong_address_are_not_interpreted(self):
        port = FakePort(reply=lambda req: b"\x85\x01\x02\r" + frame_at(1))
        result = port.probe((0,))
        query = result["queries"][0]
        self.assertEqual(query["non_ascii"], 3)
        self.assertGreater(query["bytes"], 3)
        self.assertIsNone(query["sample"])
        self.assertEqual(query["rejected"], 1)

    def test_existing_master_query_stops_further_transmissions(self):
        core = f"23014A84{length_checksum(2):04X}01"
        request = f"~{core}{checksum(core):04X}\r".encode("ascii")
        port = FakePort(reply=lambda req: request)
        result = port.probe()
        self.assertTrue(result["busy"])
        self.assertEqual(port.writes, [read_request(0)])
        self.assertTrue(result["queries"][0]["other_master"])

    def test_compare_keeps_same_soc_packs_ambiguous(self):
        sample = {"cell_voltages": [3.3]*16, "voltage": 52.8,
                  "battery_level": 28}
        profiles = {
            1: {"cells": (3.3,)*16, "voltage": 52.8, "soc": 23},
            3: {"cells": (3.3,)*16, "voltage": 52.8, "soc": 28},
            4: {"cells": (3.3,)*16, "voltage": 52.8, "soc": 28},
        }
        rows = compare_sample(sample, profiles)
        self.assertEqual({r["slot"] for r in rows if r["plausible"]}, {3, 4})
        sample["voltage"] = 45
        self.assertEqual(compare_sample(sample, profiles), [])


if __name__ == "__main__":
    unittest.main()
