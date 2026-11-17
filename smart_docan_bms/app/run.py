"""Smart Docan BMS: BlueZ/Daren polling, MQTT Discovery and read-only dashboard."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import signal
import threading
import time
from urllib.request import Request, urlopen

from aiobmsble import BMSConfig
from aiobmsble.bms.daren_bms import BMS
from bleak import BleakScanner
import paho.mqtt.client as mqtt

from dashboard import start_dashboard
from logic import aggregate, device_id, field_spec, normalise_sample
from settings import mqtt_settings, remaining_poll_delay

LOG = logging.getLogger("smart_docan")
OPTIONS = Path("/data/options.json")
MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}$")
PREFIX = "smart_docan_bms"

def parse_addresses(text: str) -> set[str]:
    return {part.strip().upper() for part in re.split(r"[,;\s]+", text) if MAC_RE.fullmatch(part.strip())}

def parse_names(text: str) -> dict[str, str]:
    names = {}
    for item in text.split(";"):
        if "=" not in item:
            continue
        address, name = item.split("=", 1)
        if MAC_RE.fullmatch(address.strip()) and name.strip():
            names[address.strip().upper()] = name.strip()[:64]
    return names

def load_options() -> dict:
    defaults = {
        "auto_discover": True, "addresses": "", "names": "", "scan_seconds": 8,
        "poll_seconds": 30, "rescan_seconds": 180, "log_level": "info"
    }
    if OPTIONS.exists():
        defaults.update(json.loads(OPTIONS.read_text(encoding="utf-8")))
    return defaults

def mqtt_credentials() -> dict:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("Jeton Supervisor absent : fonctionnement hors Home Assistant non configuré")
    req = Request(
        "http://supervisor/services/mqtt",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urlopen(req, timeout=12) as response:
        payload = json.load(response)
    if payload.get("result") != "ok":
        raise RuntimeError("Impossible de récupérer le service MQTT auprès du Supervisor")
    data = payload.get("data", {})
    if not data.get("host"):
        raise RuntimeError("Service MQTT indisponible (installer/configurer Mosquitto)")
    return data

class Store:
    """Thread-safe status shared by MQTT worker, Bluetooth loop, and dashboard."""
    def __init__(self, poll_seconds: int = 30, dashboard_refresh_seconds: int = 20):
        self.lock = threading.RLock()
        self.poll_seconds = poll_seconds
        self.dashboard_refresh_seconds = dashboard_refresh_seconds
        self.cycle_duration_seconds = None
        self.cycle_completed_at = None
        self.last_cycle_period_seconds = None
        self._last_cycle_started_monotonic = None
        self.batteries: dict[str, dict] = {}
        self.aggregate: dict = {}
        self.mqtt_connected = False
        self.mqtt_target = "Indéterminé"
        self.mqtt_error = "Connexion en attente"
        self.mqtt_failures = 0

    def scan(self, address: str, name: str, rssi: int | None):
        with self.lock:
            rec = self.batteries.setdefault(
                address, {"address": address, "name": name, "online": False,
                          "failures": 0, "last_seen": None, "error": None, "data": {},
                          "info": {}}
            )
            rec["name"] = name
            if rssi is not None:
                rec["rssi"] = rssi

    def success(self, address: str, data: dict, info: dict, temperature_diagnostic: str = ""):
        with self.lock:
            rec = self.batteries[address]
            rec.update(data=data, online=True, failures=0, error=None,
                       last_seen=data["last_seen"],
                       temperature_diagnostic=temperature_diagnostic)
            if info:
                rec["info"] = info
            self._refresh()

    def failure(self, address: str, exc: Exception):
        with self.lock:
            rec = self.batteries[address]
            rec["failures"] += 1
            rec["error"] = type(exc).__name__ + ": " + str(exc)[:180]
            # transient BLE errors should not instantly mark everything unavailable
            if rec["failures"] >= 3:
                rec["online"] = False
            self._refresh()

    def _refresh(self):
        self.aggregate = aggregate(list(self.batteries.values()), len(self.batteries))

    def cycle_started(self) -> float:
        now = time.monotonic()
        with self.lock:
            if self._last_cycle_started_monotonic is not None:
                self.last_cycle_period_seconds = round(
                    now - self._last_cycle_started_monotonic, 2
                )
            self._last_cycle_started_monotonic = now
        return now

    def cycle_finished(self, duration_seconds: float) -> None:
        with self.lock:
            self.cycle_duration_seconds = round(duration_seconds, 2)
            self.cycle_completed_at = datetime.now(timezone.utc).isoformat()

    def snapshot(self) -> dict:
        with self.lock:
            # read-only JSON clone eliminates mutation while HTTP formats the page
            return json.loads(json.dumps({
                "batteries": sorted(self.batteries.values(), key=lambda x: x["address"]),
                "aggregate": self.aggregate,
                "mqtt_connected": self.mqtt_connected,
                "mqtt_target": self.mqtt_target,
                "mqtt_error": self.mqtt_error,
                "mqtt_failures": self.mqtt_failures,
                "poll_seconds": self.poll_seconds,
                "dashboard_refresh_seconds": self.dashboard_refresh_seconds,
                "cycle_duration_seconds": self.cycle_duration_seconds,
                "last_cycle_period_seconds": self.last_cycle_period_seconds,
                "cycle_completed_at": self.cycle_completed_at,
            }))

class MQTTBridge:
    def __init__(self, settings: dict, store: Store):
        self.store = store
        self.announced: set[str] = set()
        # Don't display or log the credentials returned by Supervisor.
        store.mqtt_target = f"{settings['host']}:{int(settings.get('port', 1883))} (TLS: {'oui' if settings.get('ssl') else 'non'}, mode {settings.get('mode', 'auto')})"
        store.mqtt_error = "Connexion initiale en cours"
        LOG.info("MQTT: connexion au broker %s", store.mqtt_target)
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="smart-docan-bms",
            protocol=mqtt.MQTTv311,
        )
        if settings.get("username"):
            self.client.username_pw_set(settings["username"], settings.get("password", ""))
        if settings.get("ssl"):
            self.client.tls_set()
        self.client.will_set(f"{PREFIX}/bridge/status", "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=2, max_delay=60)
        self.client.on_connect = self._connected
        self.client.on_connect_fail = self._connect_failed
        self.client.on_disconnect = self._disconnected
        self.client.enable_logger(LOG)
        self.client.connect_async(settings["host"], int(settings.get("port", 1883)), keepalive=60)
        self.client.loop_start()

    def _connected(self, client, userdata, flags, reason_code, properties):
        self.store.mqtt_connected = int(reason_code) == 0
        self.announced.clear()
        if self.store.mqtt_connected:
            self.store.mqtt_error = ""
            self.store.mqtt_failures = 0
            LOG.info("MQTT connecté : %s", self.store.mqtt_target)
            self.publish(f"{PREFIX}/bridge/status", "online")
        else:
            self.store.mqtt_failures += 1
            self.store.mqtt_error = f"Connexion refusée par le broker (code {reason_code})"
            LOG.warning("MQTT : %s", self.store.mqtt_error)

    def _connect_failed(self, client, userdata):
        """Called by paho loop_start when TCP/TLS connection fails."""
        self.store.mqtt_connected = False
        self.store.mqtt_failures += 1
        self.store.mqtt_error = "Connexion TCP/TLS impossible (hôte, port, réseau ou certificat)"
        if self.store.mqtt_failures <= 3 or self.store.mqtt_failures % 10 == 0:
            LOG.warning("MQTT: connexion impossible vers %s (tentative %d)",
                        self.store.mqtt_target, self.store.mqtt_failures)

    def _disconnected(self, client, userdata, disconnect_flags, reason_code, properties):
        self.store.mqtt_connected = False
        self.announced.clear()
        self.store.mqtt_error = f"Déconnecté du broker (code {reason_code})"
        LOG.warning("MQTT : %s", self.store.mqtt_error)

    def publish(self, topic: str, payload: str | dict, retain: bool = True):
        if not self.store.mqtt_connected:
            return
        msg = json.dumps(payload, ensure_ascii=False, allow_nan=False) if isinstance(payload, dict) else payload
        self.client.publish(topic, msg, qos=1, retain=retain)

    def entity(self, address: str, name: str, key: str, data: dict, info: dict):
        spec = field_spec(key)
        if not spec:
            return
        label, unit, device_class, state_class, category, kind = spec
        ident = device_id(address)
        uid = f"{ident}_{key}"
        if uid in self.announced:
            return
        base = f"{PREFIX}/{ident}"
        dev = {
            "identifiers": [ident],
            "name": name,
            "manufacturer": info.get("manufacturer", "Daren"),
            "model": info.get("model", "Docan Power NOON (Daren BLE)"),
        }
        if info.get("fw_version"):
            dev["sw_version"] = str(info["fw_version"])
        config = {
            "name": label,
            "unique_id": f"smart_{uid}",
            "device": dev,
            "state_topic": f"{base}/state",
            "availability_topic": f"{base}/availability",
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        if kind == "binary_sensor":
            config["value_template"] = (
                "{{ 'ON' if value_json.get('" + key + "') else 'OFF' }}"
            )
            config["payload_on"] = "ON"
            config["payload_off"] = "OFF"
        else:
            config["value_template"] = "{{ value_json.get('" + key + "') }}"
            if unit:
                config["unit_of_measurement"] = unit
            if state_class:
                config["state_class"] = state_class
        if device_class:
            config["device_class"] = device_class
        if category:
            config["entity_category"] = category
        self.publish(f"homeassistant/{kind}/{uid}/config", config)
        if self.store.mqtt_connected:
            self.announced.add(uid)

    def battery(self, rec: dict):
        address, name, data = rec["address"], rec["name"], rec["data"]
        base = f"{PREFIX}/{device_id(address)}"
        for key in data:
            self.entity(address, name, key, data, rec.get("info") or {})
        self.publish(f"{base}/state", data)
        self.publish(f"{base}/availability", "online" if rec["online"] else "offline")

    def summary(self, summary: dict):
        uid = "docan_aggregate"
        base = f"{PREFIX}/{uid}"
        if summary.get("complete"):
            for key, label, unit, device_class in (
                ("battery_level", "SOC global estimé", "%", "battery"),
                ("voltage", "Tension moyenne", "V", "voltage"),
                ("current", "Courant total", "A", "current"),
                ("power", "Puissance totale", "W", "power"),
                ("stored_energy_wh", "Énergie stockée estimée", "Wh", "energy"),
                ("voltage_spread", "Écart tensions batteries", "V", "voltage"),
                ("online_count", "Batteries en ligne", None, None),
            ):
                if key not in summary:
                    continue
                entity_id = f"{uid}_{key}"
                if entity_id not in self.announced:
                    cfg = {
                        "name": label, "unique_id": f"smart_{entity_id}",
                        "device": {"identifiers": [uid], "name": "Docan Power - Global",
                                   "manufacturer": "Smart Docan BMS",
                                   "model": "Synthèse des batteries en parallèle"},
                        "state_topic": f"{base}/state",
                        "value_template": "{{ value_json.get('" + key + "') }}",
                        "availability_topic": f"{base}/availability",
                        "payload_available": "online", "payload_not_available": "offline",
                    }
                    if unit:
                        cfg["unit_of_measurement"] = unit
                        cfg["state_class"] = "measurement"
                    if device_class:
                        cfg["device_class"] = device_class
                    self.publish(f"homeassistant/sensor/{entity_id}/config", cfg)
                    if self.store.mqtt_connected:
                        self.announced.add(entity_id)
            self.publish(f"{base}/state", summary)
        self.publish(f"{base}/availability", "online" if summary.get("complete") else "offline")

    def stop(self):
        self.client.publish(f"{PREFIX}/bridge/status", "offline", qos=1, retain=True)
        self.client.disconnect()
        self.client.loop_stop()

async def run() -> None:
    settings = load_options()
    logging.basicConfig(
        level=getattr(logging, str(settings["log_level"]).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    LOG.info("Smart Docan BMS 0.1.2-dev — lecture seule, Bluetooth Daren")
    wanted = parse_addresses(settings.get("addresses", ""))
    labels = parse_names(settings.get("names", ""))
    if not settings.get("auto_discover", True) and not wanted:
        LOG.warning("Découverte automatique désactivée et aucune adresse configurée")
    polling = int(settings["poll_seconds"])
    refresh = int(settings.get("dashboard_refresh_seconds", 20))
    store = Store(poll_seconds=polling, dashboard_refresh_seconds=refresh)
    start_dashboard(store.snapshot)
    bridge: MQTTBridge | None = None
    next_mqtt_attempt = 0.0
    LOG.info("Cadence cible de lecture et publication BMS: %d s; rafraîchissement écran: %d s",
             polling, refresh)
    LOG.info("Dashboard Home Assistant disponible via Ingress (port interne 1895)")
    devices: dict[str, tuple] = {}  # address -> (BLEDevice, RSSI)
    known_info: dict[str, dict] = {}
    next_scan = 0.0
    scan_seconds = int(settings["scan_seconds"])
    rescan = int(settings["rescan_seconds"])
    async def read_one(address: str, device, rssi):
        name = labels.get(address, device.name or address)
        store.scan(address, name, rssi)
        bms = BMS(ble_device=device, config=BMSConfig(keep_alive=True))
        try:
            async with bms:
                sample = await bms.async_update()
                if not sample:
                    raise ValueError("Trame BMS vide")
                if address not in known_info:
                    try:
                        info = await bms.device_info()
                        known_info[address] = dict(info)
                    except Exception as exc:
                        # Some firmware variants expose measurements but no device info.
                        LOG.info("Informations firmware indisponibles pour %s : %s", name, type(exc).__name__)
                        known_info[address] = {}
            data = normalise_sample(sample, rssi)
            temperature_format = "temp_values" if "temp_values" in sample else "temperature"
            raw_temps = sample.get(temperature_format)
            count = len(raw_temps) if isinstance(raw_temps, (list, tuple)) else 0
            scalar = sample.get("temperature") if isinstance(sample.get("temperature"), (int, float)) else None
            temp_diagnostic = (
                f"Sondes annoncées: {data.get('temp_count', '?')}; "
                f"sondes décodées: {count}; moyenne: {scalar if scalar is not None else '?'}; "
                f"source: {temperature_format}"
            )
            store.success(address, data, known_info[address], temp_diagnostic)
            if count == 0:
                LOG.info("%s : températures absentes du décodage (%s)", name, temp_diagnostic)
            LOG.info("%s : SOC %s %% ; %s V ; %s cellules",
                     name, data.get("battery_level", "?"), data.get("voltage", "?"),
                     len([k for k in data if re.fullmatch(r"cell_\d+", k)]))
        except Exception as exc:
            store.failure(address, exc)
            LOG.warning("%s : lecture impossible (%s)", name, type(exc).__name__)
        finally:
            rec = next((r for r in store.snapshot()["batteries"] if r["address"] == address), None)
            if rec and bridge is not None:
                bridge.battery(rec)

    try:
        while True:
            cycle_started = store.cycle_started()
            if bridge is None and cycle_started >= next_mqtt_attempt:
                try:
                    # In manual mode there is no Supervisor MQTT lookup.
                    service = mqtt_credentials() if settings.get("mqtt_mode", "auto") == "auto" else None
                    mqtt_config = mqtt_settings(settings, service)
                    bridge = MQTTBridge(mqtt_config, store)
                except Exception as exc:
                    # MQTT failure must never stop Bluetooth telemetry or web UI.
                    store.mqtt_connected = False
                    if isinstance(exc, ValueError):
                        store.mqtt_error = str(exc)
                    else:
                        store.mqtt_error = f"Configuration ou service MQTT indisponible ({type(exc).__name__})"
                    LOG.warning("MQTT indisponible (%s), nouvelle tentative dans 60 secondes",
                                type(exc).__name__)
                next_mqtt_attempt = time.monotonic() + 60
            if time.monotonic() >= next_scan:
                try:
                    found = await BleakScanner.discover(timeout=scan_seconds, return_adv=True)
                    count = 0
                    for device, adv in found.values():
                        name = (adv.local_name or device.name or "").strip()
                        address = device.address.upper()
                        if not MAC_RE.fullmatch(address):
                            continue
                        if (wanted and address not in wanted) or (
                            not wanted and (not settings["auto_discover"] or not name.startswith("DWF"))
                        ):
                            continue
                        # With an explicit address allow different advertisement names.
                        if not wanted and not name.startswith("DWF"):
                            continue
                        devices[address] = (device, adv.rssi)
                        store.scan(address, labels.get(address, name), adv.rssi)
                        count += 1
                    LOG.info("Bluetooth : %d BMS détecté(s) pendant cette recherche, %d connus", count, len(devices))
                except Exception as exc:
                    LOG.warning("Recherche Bluetooth indisponible (%s) : vérifier host_dbus", type(exc).__name__)
                next_scan = time.monotonic() + rescan
            for address, (device, rssi) in sorted(devices.items()):
                await read_one(address, device, rssi)
            if bridge is not None:
                bridge.summary(store.snapshot()["aggregate"])
            elapsed = time.monotonic() - cycle_started
            store.cycle_finished(elapsed)
            remaining = remaining_poll_delay(elapsed, polling)
            if remaining == 0:
                LOG.debug("Le cycle BMS (%.1f s) atteint/dépasse la période cible (%d s)", elapsed, polling)
            await asyncio.sleep(remaining)
    finally:
        if bridge is not None:
            bridge.stop()

def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        LOG.info("Arrêt de l'add-on")

if __name__ == "__main__":
    main()
