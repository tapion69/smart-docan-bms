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


def can_recover_stale_connection(
    *,
    enabled: bool,
    missed_scans: int,
    missing_since: float | None,
    last_success: float | None,
    last_cleanup: float | None,
    now: float,
    absent_seconds: int,
    cooldown_seconds: int = 3600,
) -> bool:
    """Gate a target-only BlueZ disconnect to long-missing, unreadable BMS slots.

    Do not affect a healthy BMS which is connected but no longer advertising.
    Require at least two missed scans and a long gap without successful readings.
    """
    if not enabled or missed_scans < 2 or missing_since is None:
        return False
    if now - missing_since < absent_seconds:
        return False
    if last_success is not None and now - last_success < absent_seconds:
        return False
    if last_cleanup is not None and now - last_cleanup < cooldown_seconds:
        return False
    return True
