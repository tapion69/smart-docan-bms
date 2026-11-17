"""Pure BLE retry scheduling for missing or temporarily failed battery slots.

Do not reset Home Assistant's Bluetooth adapter or remove BlueZ devices:
other integrations can be using the same controller.
"""
from __future__ import annotations


def missing_slots(configured: list[tuple[int, str]], seen: set[str]) -> list[int]:
    """Configured logical slots not observed during a BLE discovery scan."""
    return sorted(slot for slot, address in configured if address not in seen)


def scan_interval(normal_seconds: int, missing_seconds: int, missing: bool) -> int:
    """Shorten scans only if an assigned battery remains missing."""
    return int(missing_seconds if missing else normal_seconds)


def retry_delay(failures: int, base_seconds: int = 5, maximum: int = 60) -> int:
    """Bounded BLE connection retry delay to avoid saturating BlueZ."""
    if failures <= 0:
        return 0
    return min(maximum, base_seconds * 2 ** min(failures - 1, 5))


def is_attempt_due(failures: int, last_attempt: float | None, now: float,
                   base_seconds: int = 5) -> bool:
    if failures <= 0 or last_attempt is None:
        return True
    return now - last_attempt >= retry_delay(failures, base_seconds)
