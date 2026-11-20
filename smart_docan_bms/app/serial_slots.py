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
                if not (1 <= slot <= 4 and 1 <= address <= 15):
                    raise ValueError("Association RS485 enregistrée invalide")
                self.mapping[slot] = address

        overrides = {}
        for n in range(1, 5):
            v = str(options.get(f"serial_slot_{n}_address") or "").strip()
            if v:
                addresses = parse_serial_addresses(v)
                if len(addresses) != 1:
                    raise ValueError(f"serial_slot_{n}_address : une seule adresse RS485")
                overrides[n] = addresses[0]
        if len(set(overrides.values())) != len(overrides):
            raise ValueError("Une même adresse RS485 est affectée à plusieurs batteries")
        # Explicit slot address takes precedence over older auto assignment.
        for n, addr in list(self.mapping.items()):
            if n not in overrides and addr in overrides.values():
                del self.mapping[n]
        self.mapping.update(overrides)
        if len(self.mapping.values()) != len(set(self.mapping.values())):
            raise ValueError("Adresse RS485 affectée deux fois")
        self._save()

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
        slot = self.slot_for(address)
        if slot is not None:
            return slot
        if address in self.mapping.values():
            # Known but disabled; cannot reallocate it to a different slot.
            return None
        slot = next(
            (n for n in range(1, 5) if self.enabled[n] and n not in self.mapping),
            None,
        )
        if slot is not None:
            self.mapping[slot] = address
            self._save()
        return slot

    def configured(self):
        return sorted((slot, address) for slot, address in self.mapping.items()
                      if self.enabled[slot])

    def inactive(self):
        return sorted(slot for slot in range(1, 5) if not self.enabled[slot])
