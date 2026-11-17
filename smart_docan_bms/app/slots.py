"""Stable, persisted Bluetooth -> logical BMS slot bindings.

Slots are identifiers for the *installation position*, not for BLE MAC addresses.
They survive add-on restarts and are never reassigned just because a BMS is absent.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

MAC = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")
MAX_SLOTS = 4

def clean_mac(value: str) -> str:
    value = str(value or "").strip().upper()
    if not MAC.fullmatch(value):
        raise ValueError("Adresse Bluetooth incorrecte (format AA:BB:CC:DD:EE:FF)")
    return value


def slot_device_id(slot: int) -> str:
    if not 1 <= slot <= MAX_SLOTS:
        raise ValueError("Numéro de batterie invalide")
    return f"smart_docan_bms_battery_{slot}"


def entity_key(key: str) -> str:
    return {"battery_level": "soc", "battery_health": "soh"}.get(key, key)


def slot_entity_id(slot: int, key: str) -> str:
    return f"{slot_device_id(slot)}_{entity_key(key)}"


def global_entity_id(key: str) -> str:
    return f"smart_docan_bms_global_{entity_key(key)}"


class SlotAssignments:
    """Persist assignments at /data/slots.json without storing secrets in Git."""

    def __init__(self, path: str | Path, options: dict):
        self.path = Path(path)
        self.strict = bool(options.get("strict_slots", False))
        self.names = {
            i: str(options.get(f"slot_{i}_name") or f"Batterie {i}").strip()[:64]
            for i in range(1, MAX_SLOTS + 1)
        }
        self.slots: dict[int, str] = {}
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("Le fichier slots.json a un format inconnu : aucune réaffectation automatique")
            raw = payload.get("slots", {})
            if not isinstance(raw, dict):
                raise ValueError("Fichier slots.json invalide")
            for index, mac in raw.items():
                if not str(index).isdigit() or not 1 <= int(index) <= MAX_SLOTS:
                    raise ValueError("Numéro d'emplacement invalide dans slots.json")
                self.slots[int(index)] = clean_mac(mac)

        # Settings win over persistence; duplicate addresses are forbidden.
        for i in range(1, MAX_SLOTS + 1):
            configured = str(options.get(f"slot_{i}_address") or "").strip()
            if configured:
                self.slots[i] = clean_mac(configured)
        if len(set(self.slots.values())) != len(self.slots):
            raise ValueError("Une même adresse Bluetooth est attribuée à deux batteries")
        self._save_if_changed()

    def _save_if_changed(self) -> None:
        canonical = json.dumps({
            "version": 1,
            "slots": {str(i): self.slots[i] for i in sorted(self.slots)},
        }, indent=2) + "\n"
        if self.path.exists() and self.path.read_text(encoding="utf-8") == canonical:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(canonical, encoding="utf-8")
        os.replace(tmp, self.path)

    def slot_for(self, address: str) -> int | None:
        address = clean_mac(address)
        return next((i for i, mac in self.slots.items() if mac == address), None)

    def bind_discovered(self, addresses: list[str] | set[str]) -> dict[str, int]:
        """Assign previously unseen devices only to unused slots, never shuffle."""
        normalized = sorted({clean_mac(a) for a in addresses})
        if not self.strict:
            for address in normalized:
                if address in self.slots.values():
                    continue
                free = next((i for i in range(1, MAX_SLOTS + 1) if i not in self.slots), None)
                if free is None:
                    break
                self.slots[free] = address
                self._save_if_changed()
        return {
            address: slot
            for address in normalized
            if (slot := self.slot_for(address)) is not None
        }

    def get_name(self, slot: int) -> str:
        return self.names[slot]

    def configured(self) -> list[tuple[int, str]]:
        return sorted(self.slots.items())
