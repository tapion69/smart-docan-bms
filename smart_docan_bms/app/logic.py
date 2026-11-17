"""Pure data processing and MQTT Discovery metadata for Smart Docan BMS.

All values are read from Daren BMS telemetry via aiobmsble. No configuration
or protection parameter writes are implemented.
"""
from __future__ import annotations

import re
import math
from datetime import datetime, timezone

# field: French label, unit, device class, state class, category, kind
FIELDS = {
    "battery_level": ("Charge batterie", "%", "battery", "measurement", None, "sensor"),
    "battery_health": ("Santé batterie", "%", None, "measurement", None, "sensor"),
    "voltage": ("Tension batterie", "V", "voltage", "measurement", None, "sensor"),
    "current": ("Courant batterie", "A", "current", "measurement", None, "sensor"),
    "power": ("Puissance batterie", "W", "power", "measurement", None, "sensor"),
    "design_capacity": ("Capacité nominale", "Ah", None, "measurement", "diagnostic", "sensor"),
    "cycle_charge": ("Capacité restante", "Ah", None, "measurement", None, "sensor"),
    "cycle_capacity": ("Énergie par cycle", "Wh", "energy", "measurement", "diagnostic", "sensor"),
    "stored_energy_wh": ("Énergie stockée estimée", "Wh", "energy", "measurement", None, "sensor"),
    "cycles": ("Cycles", None, None, "total_increasing", None, "sensor"),
    "total_charge": ("Charge cumulée", "Ah", None, "total_increasing", "diagnostic", "sensor"),
    "delta_cell_voltage": ("Écart cellules", "V", "voltage", "measurement", None, "sensor"),
    "cell_min_voltage": ("Cellule minimale", "V", "voltage", "measurement", None, "sensor"),
    "cell_max_voltage": ("Cellule maximale", "V", "voltage", "measurement", None, "sensor"),
    "cell_min_index": ("Numéro cellule minimale", None, None, None, "diagnostic", "sensor"),
    "cell_max_index": ("Numéro cellule maximale", None, None, None, "diagnostic", "sensor"),
    "cell_count": ("Nombre de cellules", None, None, None, "diagnostic", "sensor"),
    "temp_count": ("Nombre de sondes", None, None, None, "diagnostic", "sensor"),
    "temperature": ("Température moyenne", "°C", "temperature", "measurement", None, "sensor"),
    "balance_current": ("Courant équilibrage", "A", "current", "measurement", "diagnostic", "sensor"),
    "heater_current": ("Courant chauffage", "A", "current", "measurement", "diagnostic", "sensor"),
    "runtime": ("Autonomie brute", "s", "duration", "measurement", "diagnostic", "sensor"),
    "runtime_text": ("Autonomie estimée", None, None, None, None, "sensor"),
    "problem_code": ("Code alarme", None, None, None, "diagnostic", "sensor"),
    "balancer": ("Équilibrage actif", None, None, None, "diagnostic", "binary_sensor"),
    "battery_charging": ("En charge", None, None, None, None, "binary_sensor"),
    "chrg_mosfet": ("MOSFET charge", None, None, None, "diagnostic", "binary_sensor"),
    "dischrg_mosfet": ("MOSFET décharge", None, None, None, "diagnostic", "binary_sensor"),
    "heater": ("Chauffage actif", None, None, None, "diagnostic", "binary_sensor"),
    "problem": ("Alarme BMS", None, "problem", None, None, "binary_sensor"),
    "rssi": ("Signal Bluetooth", "dBm", "signal_strength", "measurement", "diagnostic", "sensor"),
    "last_seen": ("Dernière lecture", None, "timestamp", None, "diagnostic", "sensor"),
}

def device_id(mac: str) -> str:
    """Stable identifier independent of scan order and friendly name."""
    return "docan_" + re.sub(r"[^0-9a-f]", "", mac.lower())

VOLTAGE_THREE_DECIMALS = {
    "delta_cell_voltage", "cell_min_voltage", "cell_max_voltage",
    "voltage_spread",
}


def voltage_decimals(key: str) -> int | None:
    """Pack voltage is 2 decimals; every other published voltage is 3."""
    if key == "voltage":
        return 2
    if key in VOLTAGE_THREE_DECIMALS or re.fullmatch(r"cell_\\d{2}", key):
        return 3
    return None


def format_runtime(seconds: object) -> str | None:
    """French duration: days/hours/minutes, and seconds only under one minute."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    remaining = int(seconds)
    if remaining < 60:
        return f"{remaining} s"
    minutes = remaining // 60
    days, rem_minutes = divmod(minutes, 1440)
    hours, mins = divmod(rem_minutes, 60)
    parts = []
    if days:
        parts.append(f"{days} j")
    if hours:
        parts.append(f"{hours} h")
    if mins or not parts:
        parts.append(f"{mins} min")
    return " ".join(parts)


def normalise_sample(sample: dict, rssi: int | None = None) -> dict:
    """Keep supported raw fields and expose every returned cell/temperature."""
    data: dict = {}
    for key, value in sample.items():
        if key in ("cell_voltages", "temperature", "temp_values", "problems", "packs"):
            continue
        # aiobmsble 0.33.0: temp_sensors/temp_values/delta_voltage.
        # Later versions: temp_count/temperature/delta_cell_voltage.
        if key == "temp_sensors":
            data["temp_count"] = value
            continue
        if key == "delta_voltage":
            data["delta_cell_voltage"] = value
            continue
        if isinstance(value, (str, bool, int, float)) or value is None:
            data[key] = value
        elif hasattr(value, "value"):  # Enum/int enum
            data[key] = value.value

    cells = sample.get("cell_voltages", []) or []
    if cells:
        for pos, voltage in enumerate(cells, 1):
            data[f"cell_{pos:02d}"] = round(float(voltage), 3)
        data["cell_min_voltage"] = min(cells)
        data["cell_max_voltage"] = max(cells)
        data["cell_min_index"] = cells.index(min(cells)) + 1
        data["cell_max_index"] = cells.index(max(cells)) + 1
        data.setdefault("delta_cell_voltage", round(max(cells) - min(cells), 3))

    # aiobmsble 0.33.0 also publishes a numeric average in "temperature".
    avg_temp = sample.get("temperature")
    if isinstance(avg_temp, (float, int)) and not isinstance(avg_temp, bool):
        data["temperature"] = float(avg_temp)
    temps = sample.get("temp_values", sample.get("temperature", [])) or []
    if isinstance(temps, (list, tuple)) and temps:
        for pos, temp in enumerate(temps, 1):
            data[f"temp_{pos:02d}"] = float(getattr(temp, "value", temp))
            sensor_type = getattr(getattr(temp, "type", None), "name", "GENERIC")
            data[f"temp_type_{pos:02d}"] = str(sensor_type)
        # Preserve the library's average when supplied; calculate only if absent.
        data.setdefault("temperature", round(
            sum(data[f"temp_{i:02d}"] for i in range(1, len(temps) + 1)) / len(temps), 2
        ))

    if "problems" in sample:
        data["problems"] = [str(p) for p in sample["problems"]]
    if "packs" in sample:
        data["pack_count"] = len(sample["packs"])
    if rssi is not None:
        data["rssi"] = rssi

    # Preserve numeric states for automations, but apply a consistent precision
    # before MQTT publication (no fabricated accuracy beyond the BMS values).
    for key, value in list(data.items()):
        digits = voltage_decimals(key)
        if digits is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(value):
                data[key] = round(float(value), digits)

    if "voltage" in data and "battery_level" in data and "design_capacity" in data:
        data["stored_energy_wh"] = round(
            data["voltage"] * data["design_capacity"] * data["battery_level"] / 100, 1
        )
    if "runtime" in data:
        text = format_runtime(data["runtime"])
        if text is not None:
            data["runtime_text"] = text
    data["last_seen"] = datetime.now(timezone.utc).isoformat()
    return data

def field_spec(key: str):
    """Field metadata; None for unknown/unsupported fields."""
    if key in FIELDS:
        return FIELDS[key]
    if re.fullmatch(r"cell_\d{2}", key):
        return (f"Tension cellule {int(key[-2:])}", "V", "voltage", "measurement", None, "sensor")
    if re.fullmatch(r"temp_\d{2}", key):
        return (f"Température sonde {int(key[-2:])}", "°C", "temperature", "measurement", None, "sensor")
    if key in ("pack_count",):
        return ("Nombre de packs", None, None, None, "diagnostic", "sensor")
    return None

def aggregate(records: list[dict], expected: int) -> dict:
    """Aggregate only when every registered battery is currently fresh.

    Current/power sums are only meaningful for batteries in parallel. Voltages
    are averaged only under the same declared topology. Missing telemetry is
    never silently interpreted as zero.
    """
    valid = [r for r in records if r.get("online") and r.get("data")]
    count = len(valid)
    result = {"online_count": count, "expected_count": expected, "complete": bool(expected) and count == expected}
    if not result["complete"]:
        return result
    values = [r["data"] for r in valid]
    def all_values(key):
        return [x[key] for x in values] if all(isinstance(x.get(key), (int, float)) for x in values) else []
    soc = all_values("battery_level")
    capacities = all_values("design_capacity")
    if soc:
        if capacities and sum(capacities):
            result["battery_level"] = round(sum(s * c for s, c in zip(soc, capacities)) / sum(capacities), 1)
        else:
            result["battery_level"] = round(sum(soc) / len(soc), 1)
    for field in ("current", "power", "stored_energy_wh"):
        numbers = all_values(field)
        if numbers:
            result[field] = round(sum(numbers), 2)
    volts = all_values("voltage")
    if volts:
        result["voltage"] = round(sum(volts) / len(volts), 3)
        result["voltage_spread"] = round(max(volts) - min(volts), 3)
    result["problem"] = any(bool(x.get("problem")) for x in values)
    result["last_seen"] = datetime.now(timezone.utc).isoformat()
    return result
