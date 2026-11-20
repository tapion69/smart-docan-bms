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
                        cells_start: int, tolerance: float = 0.45
                        ) -> dict[tuple[int, str], float]:
    """Candidate SOC positions, deduplicated when 16-bit forms pad a byte.

    A byte sequence 00 29 00 represents 41% at byte 1 as u8, uint16
    big-endian from byte 0, or uint16 little-endian from byte 1.
    These are THREE interpretations of ONE physical byte, not evidence of
    three different SOC fields. Canonicalize padded /1 forms to u8 before
    scoring the shared schema across different BMS addresses.

    Tight tolerance (<0.5%) avoids treating integer SOC 37% and 38% as
    equivalent. This remains an inference, not a certified v23/85 layout.
    """
    if (isinstance(target, bool)
            or not isinstance(target, (int, float))
            or not math.isfinite(float(target))
            or not 0 <= target <= 100):
        return {}
    tolerance = min(float(tolerance), 0.45)
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
                if not 0 <= value <= 100 or abs(value-float(target)) > tolerance:
                    continue
                # A zero-padded uint16 integer is only the neighboring
                # one-byte value reinterpreted, and must not compete with it.
                if divisor == 1 and order == "big" and payload[offset] == 0:
                    found[(offset + 1, "u8")] = value
                elif divisor == 1 and order == "little" and payload[offset+1] == 0:
                    found[(offset, "u8")] = value
                else:
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
    """Compare cell vectors, reliable minimum-cell positions and SOC evidence.

    No association is based on BLE slot order or bus address. SOC offsets are
    learned only if a shared format explains >=2 different RS485 addresses
    with >=2 distinct SOC groups. Multiple equally plausible offsets remain
    unconfirmed. Findings are diagnostic only: no file/MQTT mutations.
    """
    findings = []
    pairs_by_address: dict[int, list[dict]] = {}

    for address, raw in sorted(payloads.items()):
        if len(raw) != 98 or not 0 <= address <= 15:
            continue
        sequences = cell_windows(raw)
        if not sequences:
            findings.append({"address": address, "status": "no_cell_pattern"})
            continue
        if not ble_profiles:
            findings.append({"address": address, "status": "missing_ble_reference"})
            continue
        by_slot = []
        for ble_slot, ref in ble_profiles.items():
            candidates = []
            for offset, endian, candidate in sequences:
                for direction, vector in (
                    ("normal", candidate), ("reverse", candidate[::-1])
                ):
                    differences = [abs(a-b) for a, b in zip(ref["cells"], vector)]
                    avg = sum(differences) * 1000 / len(differences)
                    maximum = max(differences) * 1000
                    rs_low = minimum_cell(vector)
                    ble_low = ref.get("minimum_cell") or minimum_cell(ref["cells"])
                    if rs_low[2] and ble_low[2]:
                        min_status = "match" if rs_low[0] == ble_low[0] else "conflict"
                    else:
                        min_status = "indeterminate"
                    soc_sites = possible_soc_fields(raw, ref.get("soc"), offset)
                    candidates.append({
                        "address": address, "ble_slot": ble_slot,
                        "cell_offset": offset, "endian": endian,
                        "direction": direction, "mean_mv": round(avg, 2),
                        "max_mv": round(maximum, 2),
                        "rs_min_cell": rs_low[0], "ble_min_cell": ble_low[0],
                        "rs_min_gap_mv": rs_low[1],
                        "ble_min_gap_mv": ble_low[1],
                        "min_status": min_status,
                        "ble_soc": ref.get("soc"),
                        "soc_sites": set(soc_sites),
                        "soc_offsets": sorted({p for p, _ in soc_sites})[:8],
                        "pack_voltage_offsets": scalar_offsets(
                            raw, ref.get("voltage"), 100, endian, .18,
                            (offset, offset + 32)
                        )[:8],
                    })
            if candidates:
                candidates.sort(key=lambda row: (
                    row["min_status"] == "conflict",
                    row["mean_mv"], row["max_mv"]
                ))
                by_slot.append(candidates[0])
        if by_slot:
            pairs_by_address[address] = by_slot
        else:
            findings.append({"address": address, "status": "missing_ble_reference"})

    soc_schemas = shared_soc_hypotheses(pairs_by_address, ble_profiles)
    primary_soc = None
    if soc_schemas:
        first = soc_schemas[0]
        ties = [x for x in soc_schemas[1:]
                if (x["addresses"], x["levels"]) ==
                (first["addresses"], first["levels"])]
        if not ties:
            primary_soc = (first["offset"], first["encoding"])

    for address, pairs in sorted(pairs_by_address.items()):
        ranked = []
        for candidate in pairs:
            row = dict(candidate)
            row.pop("soc_sites", None)
            site = candidate["soc_sites"]
            soc_status = (
                "match" if primary_soc in site else
                "conflict" if primary_soc is not None else
                "unconfirmed"
            )
            row["soc_status"] = soc_status
            row["soc_schema"] = (
                f"octet {primary_soc[0]} {primary_soc[1]}"
                if primary_soc is not None else None
            )
            row["soc_schema_candidates"] = [
                f"{h['offset']}:{h['encoding']}" for h in soc_schemas[:4]
            ]
            # Give a reliable matching minimum cell significant but not
            # absolute weight; minimum index can change while balancing.
            row["rank_score"] = (
                row["mean_mv"]
                + (100 if row["min_status"] == "conflict" else 0)
                + (30 if soc_status == "conflict" else 0)
                - (5 if row["min_status"] == "match" else 0)
                - (6 if soc_status == "match" else 0)
            )
            ranked.append(row)
        ranked.sort(key=lambda row: (
            row["rank_score"], row["mean_mv"], row["max_mv"], row["ble_slot"]
        ))
        best = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None
        best["other_ble_slot"] = second["ble_slot"] if second else None
        best["other_mean_mv"] = second["mean_mv"] if second else None
        close = best["mean_mv"] <= 12 and best["max_mv"] <= 28
        conflict = best["min_status"] == "conflict" or best["soc_status"] == "conflict"
        if not close or conflict:
            best["status"] = "no_close_match"
        elif second is None or second["mean_mv"] > 12 or second["max_mv"] > 28:
            best["status"] = "candidate"
        else:
            strong_extra = (
                best["min_status"] == "match" or
                best["soc_status"] == "match" or
                second["mean_mv"] - best["mean_mv"] >= 4
            )
            best["status"] = (
                "candidate"
                if strong_extra and (second["rank_score"] - best["rank_score"]) >= 4
                else "ambiguous"
            )
        findings.append(best)

    # Never assign one BLE pack to two serial devices through scoring.
    occupied: dict[int, list[dict]] = {}
    for finding in findings:
        if finding["status"] == "candidate":
            occupied.setdefault(finding["ble_slot"], []).append(finding)
    for collisions in occupied.values():
        if len(collisions) > 1:
            for finding in collisions:
                finding["status"] = "ambiguous"
    return findings


def summarize_finding(row: dict) -> str:
    """Readable decision evidence without BLE MAC or raw RS485 DATAI."""
    address, status = row["address"], row["status"]
    if status == "no_cell_pattern":
        return f"RS485 adresse {address} : aucun motif 16 cellules mV identifié"
    if status == "missing_ble_reference":
        return f"RS485 adresse {address} : aucune référence Bluetooth récente"
    low_text = (
        f" ; cellule minimale RS485 n°{row.get('rs_min_cell')}, "
        f"BLE n°{row.get('ble_min_cell')} ({row.get('min_status')}, "
        f"séparation {row.get('rs_min_gap_mv')}/{row.get('ble_min_gap_mv')} mV)"
    )
    soc_text = (
        f" ; SOC BLE {row.get('ble_soc')} % ; "
        f"SOC RS485 {row.get('soc_schema') or 'champ non confirmé'} "
        f"({row.get('soc_status')})"
    )
    if row.get("soc_schema_candidates") and not row.get("soc_schema"):
        soc_text += f" ; hypothèses SOC {','.join(row['soc_schema_candidates'])}"
    if status == "no_close_match":
        return (
            f"RS485 adresse {address} : empreinte non comparable "
            f"(écart minimal {row.get('mean_mv')} mV)"
            + low_text + soc_text
        )
    if status == "ambiguous":
        return (
            f"RS485 adresse {address} : correspondance AMBIGUË ; "
            f"meilleure candidate BLE {row['ble_slot']} ({row['mean_mv']} mV), "
            f"deuxième écart {row.get('other_mean_mv')} mV ; aucune association"
            + low_text + soc_text
        )
    return (
        f"RS485 adresse {address} : candidat BLE batterie {row['ble_slot']} ; "
        f"cellules offset {row['cell_offset']} "
        f"{row['endian']} {row['direction']} ; "
        f"écart moyen {row['mean_mv']} mV, max {row['max_mv']} mV"
        + low_text + soc_text
        + f" ; offsets SOC possibles {row.get('soc_offsets', [])} ; "
        f"tension pack possibles {row.get('pack_voltage_offsets', [])}"
    )
