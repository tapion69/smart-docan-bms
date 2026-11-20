"""Bluetooth vs *passive* RS485 learning helpers for Docan/Daren.

No serial commands, MQTT writes, identity guesses by battery list order, or
persistent mappings. A serial address is matched against each BLE battery's
own 16-cell signature. Ambiguity is reported rather than auto-paired.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

CELL_MIN_V = 2.0
CELL_MAX_V = 4.4
MAX_CELL_SPREAD_V = 0.35


def ble_cell_profiles(records: list[dict], max_age_seconds: int = 180) -> dict[int, dict]:
    """Use online and recent BLE measurements; keep their stable slot IDs."""
    profiles = {}
    now = datetime.now(timezone.utc)
    for rec in records:
        if not rec.get("online"):
            continue
        data = rec.get("data") or {}
        cells = [data.get(f"cell_{n:02d}") for n in range(1, 17)]
        if not all(isinstance(x, (float, int)) and not isinstance(x, bool)
                   and math.isfinite(x) and CELL_MIN_V <= x <= CELL_MAX_V
                   for x in cells):
            continue
        when = rec.get("last_seen") or data.get("last_seen")
        try:
            ts = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
            if ts.tzinfo is None or abs((now - ts).total_seconds()) > max_age_seconds:
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        slot = rec.get("slot")
        if not isinstance(slot, int) or not 1 <= slot <= 4:
            continue
        profiles[slot] = {
            "cells": tuple(float(x) for x in cells),
            "soc": data.get("battery_level"),
            "voltage": data.get("voltage"),
            "cycles": data.get("cycles"),
            "minimum_cell": minimum_cell(tuple(float(v) for v in cells)),
        }
    return profiles


def cell_windows(payload: bytes) -> list[tuple[int, str, tuple[float, ...]]]:
    """Detect plausible 16-cell u16 millivolt arrays at any byte offset.

    Scanning is *only a hypothesis generator*. No unidentified field is
    treated as a BMS measurement or published to Home Assistant.
    """
    if len(payload) != 98:
        return []
    result = []
    for offset in range(len(payload) - 32 + 1):
        view = payload[offset:offset + 32]
        for endian in ("big", "little"):
            values = tuple(
                int.from_bytes(view[2*n:2*n+2], endian) / 1000.0
                for n in range(16)
            )
            if (all(CELL_MIN_V <= v <= CELL_MAX_V for v in values)
                    and max(values) - min(values) <= MAX_CELL_SPREAD_V):
                result.append((offset, endian, values))
    return result


def scalar_offsets(payload: bytes, target: object, divisor: float, order: str,
                   tolerance: float, exclude: tuple[int, int] | None = None) -> list[int]:
    """Find offsets with a scalar near a BLE reference, not a confirmed field."""
    if isinstance(target, bool) or not isinstance(target, (int, float)):
        return []
    if not math.isfinite(float(target)):
        return []
    found = []
    for offset in range(len(payload) - 1):
        if exclude is not None and exclude[0] <= offset < exclude[1]:
            continue
        val = int.from_bytes(payload[offset:offset+2], order) / divisor
        if abs(val - float(target)) <= tolerance:
            found.append(offset)
    return found




def minimum_cell(cells: tuple[float, ...] | list[float]) -> tuple[int, float, bool]:
    """1-based index, separation from second minimum in mV, confidence.

    A near tie (below 2 mV) must not be treated as a unique identity.
    """
    positions = sorted(range(len(cells)), key=lambda i: (cells[i], i))
    if len(positions) < 2:
        return (0, 0.0, False)
    gap = (cells[positions[1]] - cells[positions[0]]) * 1000
    return positions[0] + 1, round(gap, 2), gap >= 1.99


def possible_soc_fields(payload: bytes, target: object,
                        cells_start: int, tolerance: float = 1.2
                        ) -> dict[tuple[int, str], float]:
    """Candidate %SOC *positions*, not an authoritative v23/85 decoder.

    Explore u8 %, u16 /1, /10, /100 with both endiannesses. Exclude
    the known 16-cell span; never infer an identity from SOC alone.
    """
    if (isinstance(target, bool)
            or not isinstance(target, (int, float))
            or not math.isfinite(float(target))
            or not 0 <= target <= 100):
        return {}
    excluded = set(range(cells_start, cells_start + 32))
    found: dict[tuple[int, str], float] = {}
    for offset in range(len(payload)):
        if offset in excluded:
            continue
        if abs(payload[offset] - float(target)) <= tolerance:
            found[(offset, "u8")] = float(payload[offset])
        if offset + 1 in excluded or offset + 1 >= len(payload):
            continue
        for order in ("big", "little"):
            word = int.from_bytes(payload[offset:offset+2], order)
            for divisor in (1, 10, 100):
                value = word / divisor
                if 0 <= value <= 100 and abs(value-float(target)) <= tolerance:
                    found[(offset, f"u16_{order}_div{divisor}")] = value
    return found


def shared_soc_hypotheses(pairs_by_address: dict[int, list[dict]],
                          profiles: dict[int, dict]) -> list[dict]:
    """Require one *common offset/encoding* across ≥2 addresses AND ≥2 SOC groups.

    Such offsets are diagnostic hypotheses only. Ambiguous sites never
    disambiguate a battery or become MQTT readings.
    """
    sites: dict[tuple[int, str], list[tuple[int, int]]] = {}
    for addr, pairs in pairs_by_address.items():
        for p in pairs:
            if p["mean_mv"] > 12 or p["max_mv"] > 28 or p["min_status"] == "conflict":
                continue
            for site in p["soc_sites"]:
                sites.setdefault(site, []).append((addr, p["ble_slot"]))
    result = []
    for (offset, encoding), matches in sites.items():
        addrs = {a for a, _ in matches}
        levels = {
            round(float(profiles[slot]["soc"]), 1)
            for _, slot in matches
            if isinstance(profiles[slot].get("soc"), (int, float))
        }
        if len(addrs) < 2 or not any(
                abs(a-b) >= 3 for a in levels for b in levels):
            continue
        result.append({
            "offset": offset, "encoding": encoding,
            "addresses": len(addrs), "levels": len(levels),
        })
    return sorted(result, key=lambda h: (
        -h["addresses"], -h["levels"], h["offset"], h["encoding"]
    ))


def compare_payloads(payloads: dict[int, bytes], ble_profiles: dict[int, dict]) -> list[dict]:
    """Return candidate associations, never implicit serial-address=BLE-slot.

    Require a close 16-cell match and a meaningful score margin against other
    BLE devices. The result is **diagnostic only** and must not be persisted as
    a slot assignment or used to publish guessed telemetry.
    """
    findings: list[dict] = []
    for address, raw in sorted(payloads.items()):
        if len(raw) != 98 or not 0 <= address <= 15:
            continue
        sequences = cell_windows(raw)
        if not sequences:
            findings.append({"address": address, "status": "no_cell_pattern"})
            continue
        best_for_slot: list[dict] = []
        for ble_slot, ref in ble_profiles.items():
            cells = ref["cells"]
            all_matches = []
            for offset, endian, candidate in sequences:
                for direction, vector in (("normal", candidate), ("reverse", candidate[::-1])):
                    differences = [abs(a-b) for a,b in zip(cells, vector)]
                    mae = sum(differences) / len(differences)
                    maxdiff = max(differences)
                    all_matches.append({
                        "address": address,
                        "ble_slot": ble_slot,
                        "cell_offset": offset,
                        "endian": endian,
                        "direction": direction,
                        "mean_mv": round(mae*1000, 2),
                        "max_mv": round(maxdiff*1000, 2),
                    })
            if all_matches:
                best_for_slot.append(min(
                    all_matches, key=lambda o: (o["mean_mv"], o["max_mv"])
                ))
        if not best_for_slot:
            findings.append({"address": address, "status": "missing_ble_reference"})
            continue
        ranked = sorted(best_for_slot, key=lambda o: (o["mean_mv"], o["max_mv"]))
        best = dict(ranked[0])
        second = ranked[1] if len(ranked) > 1 else None
        # Avoid matching near-identical cell vectors across different packs.
        close = best["mean_mv"] <= 12 and best["max_mv"] <= 28
        distinct = second is None or (
            second["mean_mv"] - best["mean_mv"] >= 4
            and second["mean_mv"] >= max(1.4 * best["mean_mv"], best["mean_mv"] + 4)
        )
        best["other_ble_slot"] = second["ble_slot"] if second else None
        best["other_mean_mv"] = second["mean_mv"] if second else None
        if not close:
            best["status"] = "no_close_match"
        elif not distinct:
            best["status"] = "ambiguous"
        else:
            best["status"] = "candidate"
            ref = ble_profiles[best["ble_slot"]]
            excluded = (best["cell_offset"], best["cell_offset"]+32)
            best["soc_offsets"] = scalar_offsets(
                raw, ref.get("soc"), 100, best["endian"], 0.8, excluded
            )[:6]
            best["pack_voltage_offsets"] = scalar_offsets(
                raw, ref.get("voltage"), 100, best["endian"], 0.18, excluded
            )[:6]
        findings.append(best)

    # One Bluetooth battery can correspond to only one RS485 address.
    # Mark colliding candidates ambiguous instead of choosing by bus order.
    occupied = {}
    for finding in findings:
        if finding["status"] == "candidate":
            occupied.setdefault(finding["ble_slot"], []).append(finding)
    for values in occupied.values():
        if len(values) > 1:
            for finding in values:
                finding["status"] = "ambiguous"
    return findings


def summarize_finding(row: dict) -> str:
    """Concise metadata only; no MAC, raw telegram or personal identifiers."""
    address = row["address"]
    status = row["status"]
    if status == "no_cell_pattern":
        return f"RS485 adresse {address} : aucun motif 16 cellules mV identifié"
    if status == "missing_ble_reference":
        return f"RS485 adresse {address} : aucune référence Bluetooth récente"
    mean = row.get("mean_mv")
    other = row.get("other_mean_mv")
    if status == "no_close_match":
        return f"RS485 adresse {address} : empreinte non comparable (écart minimal {mean} mV)"
    if status == "ambiguous":
        return (
            f"RS485 adresse {address} : correspondance AMBIGUË ; "
            f"meilleure candidate BLE {row['ble_slot']} ({mean} mV), "
            f"deuxième écart {other} mV ; aucune association"
        )
    return (
        f"RS485 adresse {address} : candidat BLE batterie {row['ble_slot']} ; "
        f"cellules offset {row['cell_offset']} {row['endian']} {row['direction']} ; "
        f"écart moyen {mean} mV, max {row['max_mv']} mV ; "
        f"offsets SOC possibles {row.get('soc_offsets', [])} ; "
        f"tension pack possibles {row.get('pack_voltage_offsets', [])}"
    )
