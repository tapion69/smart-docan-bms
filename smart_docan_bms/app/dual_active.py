"""Explicit, diagnostic-only polling of the dedicated external RS485 port.

Never use on RS485A/B inter-pack wiring. Check for existing traffic before
transmitting. Only the existing documented service 0x42 is sent; no setting
writes, MQTT publications, or persistent slot assignments.
"""
from __future__ import annotations

import math
import time

from daren_serial import (
    DarenSerialError, SUPPORTED_BAUDS, _extract_frames, decode_response,
    passive_frame_signature, read_request, validate_port,
)


def _observe(chunk: bytes, stats: dict) -> None:
    stats["bytes"] += len(chunk)
    stats["non_ascii"] += sum(
        value not in (10, 13) and not 32 <= value <= 126 for value in chunk
    )


def probe_cycle(port: str, baudrate: int, addresses=(0, 1, 2, 3),
                *, serial_factory=None, clock=time.monotonic) -> dict:
    """Bounded one-port transaction. A busy preflight blocks ALL requests.

    The six-second silence check is an additional precaution, not proof
    that no other master exists. Explicit dedicated-port selection is required.
    """
    validate_port(port)
    baudrate = int(baudrate)
    if baudrate not in SUPPORTED_BAUDS:
        raise ValueError("Vitesse RS485 non prévue")
    addresses = tuple(dict.fromkeys(addresses))
    requests = [(address, read_request(address)) for address in addresses]
    if serial_factory is None:
        import serial
        serial_factory = serial.Serial
    result = {"busy": False, "preflight_bytes": 0, "queries": []}
    with serial_factory(
        port, baudrate=baudrate, timeout=0.2, write_timeout=2.2,
        bytesize=8, parity="N", stopbits=1,
    ) as ser:
        deadline = clock() + 6.0
        while clock() < deadline:
            existing = ser.read(512)
            if existing:
                result["preflight_bytes"] += len(existing)
                result["busy"] = True
                return result

        for address, request in requests:
            # Do not clear the input buffer: late/unsolicited traffic matters.
            if getattr(ser, "in_waiting", 0):
                result["busy"] = True
                break
            stats = {
                "address": address, "bytes": 0, "non_ascii": 0,
                "echoes": 0, "signatures": {}, "sample": None,
                "rejected": 0, "other_master": False,
            }
            result["queries"].append(stats)
            ser.write(request)
            ser.flush()
            deadline = clock() + 2.2
            buffer = bytearray()
            while clock() < deadline and stats["bytes"] < 4096:
                chunk = ser.read(512)
                if not chunk:
                    continue
                _observe(chunk, stats)
                buffer.extend(chunk)
                for frame in _extract_frames(buffer):
                    if frame == request:
                        stats["echoes"] += 1
                        continue
                    signature = passive_frame_signature(frame)
                    signatures = stats["signatures"]
                    signatures[signature] = signatures.get(signature, 0) + 1
                    if signature.endswith("crcOK-lenOK") and (
                        "-c4A-k84-" in signature or "-c4A-k42-" in signature
                    ):
                        stats["other_master"] = True
                        result["busy"] = True
                        break
                    try:
                        sample = decode_response(frame, address)
                    except DarenSerialError:
                        stats["rejected"] += 1
                        continue
                    stats["sample"] = sample
                if stats["other_master"]:
                    break
            if result["busy"]:
                break
    return result


def compare_sample(sample: dict, profiles: dict[int, dict]) -> list[dict]:
    """Report ranked evidence, never associate a physical pack automatically."""
    cells = sample.get("cell_voltages", [])
    if len(cells) != 16 or not all(math.isfinite(v) and 2 <= v <= 4.4 for v in cells):
        return []
    voltage = sample["voltage"]
    soc = sample["battery_level"]
    if abs(sum(cells) - voltage) > 0.8:
        return []  # pack/cell consistency check before interpreting identities
    candidates = []
    for slot, reference in profiles.items():
        ref_cells = reference.get("cells", ())
        ref_soc = reference.get("soc")
        ref_voltage = reference.get("voltage")
        if len(ref_cells) != 16 or not isinstance(ref_soc, (int, float)) or not isinstance(ref_voltage, (int, float)):
            continue
        differences = [abs(a-b)*1000 for a,b in zip(cells, ref_cells)]
        row = {
            "slot": slot,
            "mean_mv": round(sum(differences)/16, 2),
            "max_mv": round(max(differences), 2),
            "soc_delta": round(abs(soc-ref_soc), 2),
            "voltage_delta": round(abs(voltage-ref_voltage), 3),
        }
        row["plausible"] = (
            row["mean_mv"] <= 12 and row["max_mv"] <= 28
            and row["soc_delta"] <= 1.25 and row["voltage_delta"] <= 0.3
        )
        candidates.append(row)
    return sorted(candidates, key=lambda row: (
        not row["plausible"], row["soc_delta"], row["mean_mv"], row["slot"]
    ))
