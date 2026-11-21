"""Persistent, optional serial bus address -> stable MQTT battery slot mapping.

This file does NOT touch existing /data/slots.json Bluetooth associations.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from daren_serial import parse_serial_addresses


class SerialSlots:
    def __init__(self, path: str | Path, options: dict):
        self.path = Path(path)
        self.enabled = {
            n: bool(options.get(f"slot_{n}_enabled", True))
            for n in range(1, 5)
        }
        self.names = {
            n: str(options.get(f"slot_{n}_name") or f"Batterie {n}").strip()[:64]
            for n in range(1, 5)
        }
        self.mapping: dict[int, int] = {}
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("version") != 1 or not isinstance(saved.get("slots"), dict):
                raise ValueError("serial_slots.json inconnu : affectations conservées")
            for k, v in saved["slots"].items():
                slot = int(k)
                address = int(v)
                if not (1 <= slot <= 4 and 0 <= address <= 15):
                    raise ValueError("Association RS485 enregistrée invalide")
                self.mapping[slot] = address

        # Address itself defines the canonical display order. Older persisted
        # mappings are sorted again on upgrade; no manual per-slot inputs needed.
        # An explicit bus-address list, when supplied, replaces stale saved IDs.
        explicit = str(options.get("serial_addresses") or "").strip()
        known = parse_serial_addresses(explicit) if explicit else tuple(self.mapping.values())
        self._order_by_address(known)
        self._save()

    def _order_by_address(self, addresses):
        ordered = sorted(set(addresses))[:4]
        self.mapping = {slot: address for slot, address in enumerate(ordered, 1)}

    def _save(self):
        canonical = json.dumps({
            "version": 1,
            "slots": {str(n): self.mapping[n] for n in sorted(self.mapping)},
        }, indent=2) + "\n"
        if self.path.exists() and self.path.read_text(encoding="utf-8") == canonical:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(canonical, encoding="utf-8")
        os.replace(temp, self.path)

    def slot_for(self, address: int) -> int | None:
        return next(
            (n for n, addr in self.mapping.items()
             if addr == address and self.enabled[n]), None
        )

    def bind_detected(self, address: int) -> int | None:
        """Discover physical addresses, always display in ascending address order."""
        address = int(address)
        if not 0 <= address <= 15:
            raise ValueError("Adresse RS485 invalide")
        slot = self.slot_for(address)
        if slot is not None:
            return slot
        if address in self.mapping.values():
            return None  # disabled slot, keep reserved
        if len(self.mapping) >= 4:
            return None
        self._order_by_address((*self.mapping.values(), address))
        self._save()
        return self.slot_for(address)

    def configured(self):
        return sorted((slot, address) for slot, address in self.mapping.items()
                      if self.enabled[slot])

    def inactive(self):
        return sorted(slot for slot in range(1, 5) if not self.enabled[slot])
