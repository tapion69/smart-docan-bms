"""Pure configuration and scheduling helpers (no Home Assistant dependencies)."""
from __future__ import annotations


def mqtt_settings(options: dict, supervisor_service: dict | None = None) -> dict:
    """Select supervisor auto MQTT credentials or explicit manual credentials.

    The password is never logged or returned to the browser.
    """
    mode = str(options.get("mqtt_mode", "auto")).strip().lower()
    if mode not in ("auto", "manual"):
        raise ValueError("mqtt_mode doit valoir auto ou manual")

    if mode == "manual":
        host = str(options.get("mqtt_host") or "").strip()
        if not host:
            raise ValueError("Renseigner mqtt_host en mode manuel")
        result = {
            "host": host,
            "port": options.get("mqtt_port", 1883),
            "username": str(options.get("mqtt_username") or "").strip(),
            "password": str(options.get("mqtt_password") or ""),
            "ssl": bool(options.get("mqtt_tls", False)),
            "mode": "manual",
        }
    else:
        if not supervisor_service or not supervisor_service.get("host"):
            raise ValueError("Service MQTT automatique du Supervisor indisponible")
        result = {
            "host": str(supervisor_service["host"]).strip(),
            "port": supervisor_service.get("port", 1883),
            "username": str(supervisor_service.get("username") or ""),
            "password": str(supervisor_service.get("password") or ""),
            "ssl": bool(supervisor_service.get("ssl", False)),
            "mode": "auto",
        }

    try:
        result["port"] = int(result["port"])
    except (TypeError, ValueError) as exc:
        raise ValueError("mqtt_port doit être un port TCP valide") from exc
    if not 1 <= result["port"] <= 65535:
        raise ValueError("mqtt_port doit être compris entre 1 et 65535")
    if result["password"] and not result["username"]:
        raise ValueError("Renseigner mqtt_username lorsque mqtt_password est défini")
    return result


def remaining_poll_delay(elapsed_seconds: float, poll_seconds: int) -> float:
    """Pace cycles from their start time, not after the cycle has finished."""
    return max(0.0, float(poll_seconds) - max(0.0, float(elapsed_seconds)))
