"""Home Assistant MQTT Discovery with predictable IDs for installation slots."""
from __future__ import annotations

from logic import FIELDS, device_id, field_spec
from slots import entity_key, global_entity_id, slot_device_id, slot_entity_id


def battery_discovery(slot: int, name: str, key: str, info: dict) -> tuple[str, dict] | None:
    spec = field_spec(key)
    if spec is None:
        return None
    label, unit, device_class, state_class, category, kind = spec
    uid = slot_entity_id(slot, key)
    base = f"smart_docan_bms/{slot_device_id(slot)}"
    device = {
        "identifiers": [slot_device_id(slot)],
        "name": name,
        "manufacturer": info.get("manufacturer", "Daren"),
        "model": info.get("model", "Docan Power NOON (Daren BLE)"),
    }
    if info.get("fw_version"):
        device["sw_version"] = str(info["fw_version"])
    config = {
        "name": label,
        "unique_id": uid,
        "default_entity_id": f"{kind}.{uid}",
        "device": device,
        "state_topic": f"{base}/state",
        "availability_topic": f"{base}/availability",
        "payload_available": "online",
        "payload_not_available": "offline",
        "value_template": (
            "{{ 'ON' if value_json.get('" + key + "') else 'OFF' }}"
            if kind == "binary_sensor"
            else "{{ value_json.get('" + key + "') }}"
        ),
    }
    if kind == "binary_sensor":
        config.update(payload_on="ON", payload_off="OFF")
    else:
        if unit:
            config["unit_of_measurement"] = unit
        if state_class:
            config["state_class"] = state_class
    if device_class:
        config["device_class"] = device_class
    if category:
        config["entity_category"] = category
    return (f"homeassistant/{kind}/{uid}/config", config)


GLOBAL_FIELDS = (
    ("battery_level", "SOC global estimé", "%", "battery"),
    ("voltage", "Tension moyenne", "V", "voltage"),
    ("current", "Courant total", "A", "current"),
    ("power", "Puissance totale", "W", "power"),
    ("stored_energy_wh", "Énergie stockée estimée", "Wh", "energy"),
    ("voltage_spread", "Écart tensions batteries", "V", "voltage"),
    ("online_count", "Batteries en ligne", None, None),
)


def global_discovery(key: str) -> tuple[str, dict]:
    label, unit, device_class = next(
        (lab, unit, dc) for name, lab, unit, dc in GLOBAL_FIELDS if name == key
    )
    uid = global_entity_id(key)
    config = {
        "name": label,
        "unique_id": uid,
        "default_entity_id": f"sensor.{uid}",
        "device": {
            "identifiers": ["smart_docan_bms_global"],
            "name": "Docan Power - Global",
            "manufacturer": "Smart Docan BMS",
            "model": "Synthèse des batteries en parallèle",
        },
        "state_topic": "smart_docan_bms/global/state",
        "value_template": "{{ value_json.get('" + key + "') }}",
        "availability_topic": "smart_docan_bms/global/availability",
        "payload_available": "online",
        "payload_not_available": "offline",
    }
    if unit:
        config.update(unit_of_measurement=unit, state_class="measurement")
    if device_class:
        config["device_class"] = device_class
    return f"homeassistant/sensor/{uid}/config", config


def legacy_battery_topics(address: str) -> list[str]:
    """Retained MQTT discovery config topics created before slot IDs."""
    prefix = device_id(address)
    fields = set(FIELDS)
    fields.update(f"cell_{i:02d}" for i in range(1, 33))
    fields.update(f"temp_{i:02d}" for i in range(1, 17))
    fields.add("pack_count")
    topics = set()
    for key in fields:
        if spec := field_spec(key):
            topics.add(f"homeassistant/{spec[-1]}/{prefix}_{key}/config")
    # Legacy bridge retained state/availability can also be cleared.
    topics.add(f"smart_docan_bms/{prefix}/state")
    topics.add(f"smart_docan_bms/{prefix}/availability")
    return sorted(topics)


def legacy_global_topics() -> list[str]:
    topics = [f"homeassistant/sensor/docan_aggregate_{key}/config"
              for key, *_ in GLOBAL_FIELDS]
    topics.extend([
        "smart_docan_bms/docan_aggregate/state",
        "smart_docan_bms/docan_aggregate/availability",
    ])
    return topics
