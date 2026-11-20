"""Smart Docan BMS: headless BlueZ/Daren polling and MQTT Discovery."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from urllib.request import Request, urlopen

from aiobmsble import BMSConfig
from aiobmsble.bms.daren_bms import BMS
from bleak import BleakScanner
from bleak_retry_connector import close_stale_connections_by_address
import paho.mqtt.client as mqtt

from logic import aggregate, normalise_sample
from discovery import (
    GLOBAL_FIELDS, battery_discovery, global_discovery,
    legacy_battery_topics, legacy_global_topics,
)
from settings import mqtt_settings, remaining_poll_delay
from slots import SlotAssignments, MAX_SLOTS, slot_device_id
from recovery import (
    can_recover_stale_connection, is_attempt_due, missing_slots, scan_interval,
)
from daren_serial import (
    read_bms as read_serial_bms, parse_serial_addresses, validate_port,
    PassiveSerialMonitor,
)
from serial_slots import SerialSlots
from serial_compare import ble_cell_profiles, compare_payloads, summarize_finding

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
        "transport": "bluetooth",
        "serial_port": "",
        "serial_passive": False,
        "serial_baudrate": 19200,
        "serial_addresses": "",
        "auto_discover": True, "addresses": "", "names": "", "scan_seconds": 8,
        "poll_seconds": 30, "rescan_seconds": 180,
        "missing_rescan_seconds": 30,
        "recover_stale_connections": False,
        "stale_connection_after_seconds": 180,
        "log_level": "info",
        "strict_slots": False, "migrate_legacy_entities": True,
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
    """Thread-safe status shared by MQTT worker and Bluetooth loop."""
    def __init__(self, poll_seconds: int = 30):
        self.lock = threading.RLock()
        self.poll_seconds = poll_seconds
        self.cycle_duration_seconds = None
        self.cycle_completed_at = None
        self.last_cycle_period_seconds = None
        self._last_cycle_started_monotonic = None
        self.batteries: dict[str, dict] = {}
        # Publish explicit "0 configured" until first DWF discovery instead
        # of leaving the global MQTT device in an undefined startup state.
        self.aggregate: dict = aggregate([], 0)
        self.mqtt_connected = False
        self.mqtt_target = "Indéterminé"
        self.mqtt_error = "Connexion en attente"
        self.mqtt_failures = 0

    def register_slot(self, slot: int, address: str, name: str) -> None:
        """Create offline placeholders for configured slots (never report 3/3 for 4 expected)."""
        with self.lock:
            rec = self.batteries.setdefault(address, {
                "address": address, "name": name, "slot": slot, "online": False,
                "failures": 0, "last_seen": None, "error": None, "data": {},
                "info": {},
            })
            rec.update(slot=slot, name=name)
            self._refresh()

    def scan(self, address: str, name: str, rssi: int | None, slot: int | None = None):
        with self.lock:
            if slot is None and address not in self.batteries:
                return
            self.register_slot(slot or self.batteries[address]["slot"], address, name)
            if rssi is not None:
                self.batteries[address]["rssi"] = rssi

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
                "batteries": sorted(self.batteries.values(), key=lambda x: x.get("slot", 99)),
                "aggregate": self.aggregate,
                "mqtt_connected": self.mqtt_connected,
                "mqtt_target": self.mqtt_target,
                "mqtt_error": self.mqtt_error,
                "mqtt_failures": self.mqtt_failures,
                "poll_seconds": self.poll_seconds,
                "cycle_duration_seconds": self.cycle_duration_seconds,
                "last_cycle_period_seconds": self.last_cycle_period_seconds,
                "cycle_completed_at": self.cycle_completed_at,
            }))

class MQTTBridge:
    def __init__(self, settings: dict, store: Store, migrate_legacy: bool = True):
        self.store = store
        self.announced: set[str] = set()
        self.migrate_legacy = migrate_legacy
        self.legacy_cleaned: set[str] = set()
        self.old_global_cleaned = False
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
        # Paho MQTT v2 passes a ReasonCode object, not an int.
        # int(ReasonCode) raises TypeError and previously killed the MQTT worker.
        self.store.mqtt_connected = getattr(reason_code, "value", reason_code) == 0
        self.announced.clear()
        self.legacy_cleaned.clear()
        self.old_global_cleaned = False
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

    def entity(self, slot: int, name: str, key: str, info: dict) -> None:
        result = battery_discovery(slot, name, key, info)
        if not result:
            return
        topic, config = result
        uid = config["unique_id"]
        if uid not in self.announced:
            self.publish(topic, config)
            if self.store.mqtt_connected:
                self.announced.add(uid)

    def battery(self, rec: dict) -> None:
        """Publish only data for a persistent logical slot."""
        slot = rec.get("slot")
        if not slot:
            return
        address, name, data = rec["address"], rec["name"], rec.get("data") or {}
        base = f"{PREFIX}/{slot_device_id(slot)}"
        for key in data:
            self.entity(slot, name, key, rec.get("info") or {})
        if data:
            self.publish(f"{base}/state", data)
        self.publish(f"{base}/availability", "online" if rec["online"] else "offline")
        # Delete retained MAC-based discovery *after* new fixed-slot entities exist.
        if self.store.mqtt_connected and data and self.migrate_legacy and address not in self.legacy_cleaned:
            for topic in legacy_battery_topics(address):
                self.publish(topic, "")
            self.legacy_cleaned.add(address)
            LOG.info("%s : migration MQTT des anciens ID Bluetooth effectuée", name)

    def summary(self, summary: dict) -> None:
        base = f"{PREFIX}/global"
        # Republish discovery after MQTT reconnect, even if not all BMS are
        # reachable. Status sensors must keep working during an outage.
        for key, *_ in GLOBAL_FIELDS:
            topic, config = global_discovery(key)
            uid = config["unique_id"]
            if uid not in self.announced:
                self.publish(topic, config)
                if self.store.mqtt_connected:
                    self.announced.add(uid)
        self.publish(f"{base}/state", summary)
        self.publish(f"{base}/availability", "online")
        self.publish(
            f"{base}/measurements_availability",
            "online" if summary.get("complete") else "offline",
        )
        if self.store.mqtt_connected and self.migrate_legacy and not self.old_global_cleaned:
            for topic in legacy_global_topics():
                self.publish(topic, "")
            self.old_global_cleaned = True
            LOG.info("Migration MQTT des anciennes entités globales effectuée")

    def stop(self):
        self.client.publish(f"{PREFIX}/bridge/status", "offline", qos=1, retain=True)
        self.client.disconnect()
        self.client.loop_stop()

async def run_serial_passive(settings: dict) -> None:
    """Listen to BMS Master traffic without transmitting ANY RS485 requests.

    Existing MQTT battery slots and the global aggregate are reused. Listening
    does not guarantee that the Master itself advertises its own measurements.
    """
    polling = int(settings["poll_seconds"])
    port = str(settings.get("serial_port") or "").strip()
    baudrate = int(settings.get("serial_baudrate", 9600))
    addresses_text = str(settings.get("serial_addresses") or "").strip()
    requested = set(parse_serial_addresses(addresses_text)) if addresses_text else None
    serial_slots = SerialSlots(Path("/data/serial_slots.json"), settings)
    store = Store(poll_seconds=polling)
    for slot, address in serial_slots.configured():
        store.register_slot(slot, f"serial:{address:02d}", serial_slots.names[slot])
    bridge: MQTTBridge | None = None
    monitor: PassiveSerialMonitor | None = None
    next_mqtt_attempt = 0.0
    latest: dict[int, float] = {}
    last_report = 0.0
    stale_after = max(90, 3 * polling)
    passive_totals = {k: 0 for k in ("bytes", "frames", "responses", "other_frames", "rejected")}
    passive_signatures: dict[str, int] = {}
    passive_layouts: dict[str, int] = {}
    passive_reject_reasons: dict[str, int] = {}
    LOG.info(
        "RS485 ÉCOUTE PASSIVE : aucune requête série envoyée ; vitesse %d ; "
        "adresses reçues %s ; délai hors ligne %d s",
        baudrate, addresses_text if addresses_text else "toutes les adresses 0..15",
        stale_after,
    )
    if not port:
        LOG.warning(
            "serial_port vide en écoute passive : renseigner le chemin "
            "/dev/serial/by-id/... dans la configuration"
        )
    else:
        port = validate_port(port)
        LOG.info("Port d'écoute RS485 : %s", port)

    try:
        while True:
            cycle_started = store.cycle_started()
            if bridge is None and cycle_started >= next_mqtt_attempt:
                try:
                    service = mqtt_credentials() if settings.get("mqtt_mode", "auto") == "auto" else None
                    config = mqtt_settings(settings, service)
                    bridge = MQTTBridge(config, store, migrate_legacy=False)
                except Exception as exc:
                    store.mqtt_connected = False
                    store.mqtt_error = (
                        f"Service MQTT indisponible ({type(exc).__name__})"
                    )
                    LOG.warning(
                        "MQTT indisponible (%s), nouvelle tentative sous 60 s",
                        type(exc).__name__,
                    )
                next_mqtt_attempt = time.monotonic() + 60

            if port:
                if monitor is None:
                    try:
                        monitor = PassiveSerialMonitor(port, baudrate)
                        await asyncio.to_thread(monitor.open)
                        LOG.info(
                            "Écoute passive : port ouvert en réception seule, "
                            "aucune trame de commande transmise"
                        )
                    except Exception as exc:
                        monitor = None
                        if time.monotonic() - last_report > 30:
                            LOG.warning(
                                "Écoute passive : impossible d'ouvrir le port (%s : %s)",
                                type(exc).__name__, str(exc)[:140],
                            )
                            last_report = time.monotonic()
                if monitor is not None:
                    try:
                        records, stats = await asyncio.to_thread(
                            monitor.read_window, 4.0
                        )
                        for key in passive_totals:
                            passive_totals[key] += stats[key]
                        for key, count in stats.get("signatures", {}).items():
                            passive_signatures[key] = passive_signatures.get(key, 0) + count
                        for key, count in stats.get("layouts", {}).items():
                            passive_layouts[key] = passive_layouts.get(key, 0) + count
                        for key, count in stats.get("reject_reasons", {}).items():
                            passive_reject_reasons[key] = passive_reject_reasons.get(key, 0) + count
                        for address, sample in sorted(records.items()):
                            if requested is not None and address not in requested:
                                continue
                            slot = serial_slots.slot_for(address)
                            if slot is None and address in serial_slots.mapping.values():
                                continue  # disabled slot, keep saved assignment
                            if slot is None:
                                slot = serial_slots.bind_detected(address)
                                if slot is None:
                                    continue
                                LOG.info(
                                    "Écoute passive : adresse RS485 %d liée à Batterie %d",
                                    address, slot,
                                )
                            ident = f"serial:{address:02d}"
                            store.register_slot(slot, ident, serial_slots.names[slot])
                            was_offline = not store.batteries[ident]["online"]
                            data = normalise_sample(sample)
                            store.success(
                                ident, data,
                                {"manufacturer": "Daren", "model": "DR-1363 RS485 passif"},
                            )
                            latest[address] = time.monotonic()
                            if was_offline:
                                LOG.info(
                                    "Batterie %d RS485 : réponse valide observée "
                                    "passivement à l'adresse %d",
                                    slot, address,
                                )
                            if bridge is not None:
                                bridge.battery(store.batteries[ident])

                        now = time.monotonic()
                        for slot, address in serial_slots.configured():
                            ident = f"serial:{address:02d}"
                            rec = store.batteries.get(ident)
                            if rec is not None and rec["online"] and (
                                now - latest.get(address, 0) > stale_after
                            ):
                                # No valid service-42 telemetry for a long period
                                # despite an open bus. Do not keep stale MQTT data.
                                with store.lock:
                                    rec["online"] = False
                                    rec["error"] = (
                                        f"Aucune trame RS485 passive valide depuis "
                                        f"plus de {stale_after} s"
                                    )
                                    store._refresh()
                                LOG.warning(
                                    "Batterie %d RS485 : télémétrie passive périmée "
                                    "(adresse %d)",
                                    slot, address,
                                )
                                if bridge is not None:
                                    bridge.battery(rec)

                        if now - last_report >= 30:
                            # Aggregate across all received 4 s windows, not
                            # just the last one. Header metadata only: no raw
                            # DATAI, IDs, SOC, secrets or serial telegrams.
                            ranked = sorted(
                                passive_signatures.items(),
                                key=lambda pair: (-pair[1], pair[0]),
                            )
                            preview = ", ".join(
                                f"{signature} x{count}"
                                for signature, count in ranked[:8]
                            ) or "aucune trame complète"
                            if len(ranked) > 8:
                                preview += f" ; {len(ranked) - 8} autres types"
                            LOG.info(
                                "RS485 passif (~30 s) : %d octets ; %d trames ; "
                                "%d réponses télémétriques valides ; "
                                "%d autres trames ; %d trames invalides ; "
                                "batteries %d/%d en ligne",
                                passive_totals["bytes"], passive_totals["frames"],
                                passive_totals["responses"], passive_totals["other_frames"],
                                passive_totals["rejected"], store.aggregate["online_count"],
                                store.aggregate["expected_count"],
                            )
                            LOG.info(
                                "RS485 diagnostic passif (entêtes uniquement) : %s",
                                preview,
                            )
                            layout_text = ", ".join(
                                f"{kind} x{count}"
                                for kind, count in sorted(passive_layouts.items())
                            ) or "aucune disposition v23/85 validée"
                            LOG.info(
                                "RS485 décodage passif validé : %s",
                                layout_text,
                            )
                            if passive_reject_reasons:
                                errors = ", ".join(
                                    f"{reason} x{count}"
                                    for reason, count in sorted(
                                        passive_reject_reasons.items(),
                                        key=lambda item: (-item[1], item[0]),
                                    )[:5]
                                )
                                LOG.info(
                                    "RS485 diagnostics de décodage refusé : %s",
                                    errors,
                                )
                            passive_totals = {key: 0 for key in passive_totals}
                            passive_signatures.clear()
                            passive_layouts.clear()
                            passive_reject_reasons.clear()
                            last_report = now
                    except Exception as exc:
                        LOG.warning(
                            "Écoute RS485 interrompue (%s : %s) ; réouverture du port",
                            type(exc).__name__, str(exc)[:140],
                        )
                        try:
                            await asyncio.to_thread(monitor.close)
                        except Exception:
                            pass
                        monitor = None

            if bridge is not None:
                active = {slot for slot, _ in serial_slots.configured()}
                for slot in range(1, 5):
                    if slot not in active:
                        bridge.publish(
                            f"{PREFIX}/{slot_device_id(slot)}/availability", "offline"
                        )
                for record in store.snapshot()["batteries"]:
                    if not record["online"]:
                        bridge.battery(record)
                bridge.summary(store.snapshot()["aggregate"])
            elapsed = time.monotonic() - cycle_started
            store.cycle_finished(elapsed)
            if not port or monitor is None:
                await asyncio.sleep(4.0)
            else:
                await asyncio.sleep(0)
    finally:
        if monitor is not None:
            await asyncio.to_thread(monitor.close)
        if bridge is not None:
            bridge.stop()


async def run_serial(settings: dict) -> None:
    """RS485 bus transport; either existing polling or strictly passive mode."""
    if bool(settings.get("serial_passive", False)):
        await run_serial_passive(settings)
        return

    """Read-only serial RS485 loop, with fixed MQTT identities and no BLE polling.

    The RS485 protocol is a best-effort pilot for Daren DR-JC03. A real BMS
    may speak a different protocol; failed reads must never modify the BMS.
    """
    polling = int(settings["poll_seconds"])
    port = str(settings.get("serial_port") or "").strip()
    baudrate = int(settings.get("serial_baudrate", 9600))
    address_list = parse_serial_addresses(settings.get("serial_addresses", ""))
    explicit_serial_addresses = bool(str(settings.get("serial_addresses") or "").strip())
    serial_slots = SerialSlots(Path("/data/serial_slots.json"), settings)
    store = Store(poll_seconds=polling)
    for slot, address in serial_slots.configured():
        store.register_slot(slot, f"serial:{address:02d}", serial_slots.names[slot])
    bridge: MQTTBridge | None = None
    next_mqtt_attempt = 0.0
    failed: dict[int, int] = {}
    discovery_complete_logged = False
    LOG.info(
        "Mode Serial RS485 (lecture seule) : vitesse %d, adresses à rechercher %s",
        baudrate, ",".join(map(str, address_list)),
    )
    if not port:
        LOG.warning(
            "Mode Serial choisi, mais serial_port est vide. "
            "Renseignez votre /dev/serial/by-id/... dans la configuration"
        )
    else:
        # This checks only syntax; OS access and protocol are checked at runtime.
        port = validate_port(port)
        LOG.info("Port série configuré : %s", port)

    try:
        while True:
            started = store.cycle_started()
            if bridge is None and started >= next_mqtt_attempt:
                try:
                    service = mqtt_credentials() if settings.get("mqtt_mode", "auto") == "auto" else None
                    conf = mqtt_settings(settings, service)
                    # Never run the old MAC/BLE migration on serial bus ids.
                    bridge = MQTTBridge(conf, store, migrate_legacy=False)
                except Exception as exc:
                    store.mqtt_connected = False
                    store.mqtt_error = f"Service MQTT indisponible ({type(exc).__name__})"
                    LOG.warning("MQTT indisponible (%s), nouvel essai dans 60 s", type(exc).__name__)
                next_mqtt_attempt = time.monotonic() + 60

            if port:
                # Stop probing unassigned default addresses once every enabled
                # logical slot is associated. This avoids querying absent
                # PACK0 on a four-pack installation using only 1,2,3,4.
                # Explicit serial_addresses always take precedence.
                assigned = serial_slots.configured()
                enabled_count = sum(bool(v) for v in serial_slots.enabled.values())
                fully_assigned = bool(enabled_count) and len(assigned) == enabled_count
                if fully_assigned and not explicit_serial_addresses:
                    targets = sorted(address for _, address in assigned)
                    if not discovery_complete_logged:
                        LOG.info(
                            "RS485 : %d/%d emplacements associés, recherche "
                            "des autres adresses suspendue (adresses actives : %s)",
                            len(assigned), enabled_count,
                            ",".join(str(addr) for addr in targets),
                        )
                        discovery_complete_logged = True
                else:
                    # Include all saved BMS addresses even if outside 0..4.
                    targets = sorted(set(address_list) | {addr for _, addr in assigned})
                for address in targets:
                    slot = serial_slots.slot_for(address)
                    if slot is None and address in serial_slots.mapping.values():
                        continue  # disabled slot: do not read or reassign
                    try:
                        sample = await asyncio.to_thread(
                            read_serial_bms, port, baudrate, address
                        )
                        if slot is None:
                            slot = serial_slots.bind_detected(address)
                            if slot is None:
                                continue  # four logical slots already occupied
                            LOG.info(
                                "RS485 : BMS adresse %d associé à Batterie %d (persistant)",
                                address, slot,
                            )
                        ident = f"serial:{address:02d}"
                        store.register_slot(slot, ident, serial_slots.names[slot])
                        data = normalise_sample(sample)
                        was_offline = not store.batteries[ident].get("online")
                        store.success(ident, data, {"manufacturer": "Daren", "model": "DR-1363 RS485"})
                        failed[address] = 0
                        if was_offline:
                            LOG.info("Batterie %d : communication RS485 établie (adresse %d)", slot, address)
                        LOG.info(
                            "Batterie %d RS485 : SOC %s %% ; %s V ; %d cellules",
                            slot, data.get("battery_level"), data.get("voltage"),
                            len([k for k in data if re.fullmatch(r"cell_\d{2}", k)])
                        )
                        if bridge is not None:
                            bridge.battery(store.batteries[ident])
                    except Exception as exc:
                        failed[address] = failed.get(address, 0) + 1
                        if slot is not None:
                            ident = f"serial:{address:02d}"
                            store.failure(ident, exc)
                            if bridge is not None:
                                bridge.battery(store.batteries[ident])
                        if failed[address] <= 2 or failed[address] % 10 == 0:
                            LOG.warning(
                                "RS485 adresse %d : lecture impossible (%s : %s). "
                                "Vérifier port, adresse, baudrate, A/B et protocole",
                                address, type(exc).__name__, str(exc)[:160],
                            )

            if bridge is not None:
                for slot in range(1, 5):
                    if slot not in {s for s, _ in serial_slots.configured()}:
                        bridge.publish(
                            f"{PREFIX}/{slot_device_id(slot)}/availability", "offline"
                        )
                snapshot = store.snapshot()
                for rec in snapshot["batteries"]:
                    if not rec["online"]:
                        bridge.battery(rec)
                bridge.summary(snapshot["aggregate"])
            elapsed = time.monotonic() - started
            store.cycle_finished(elapsed)
            await asyncio.sleep(remaining_poll_delay(elapsed, polling))
    finally:
        if bridge is not None:
            bridge.stop()


async def run_dual_serial_comparison(settings: dict, ble_store: Store) -> None:
    """Concurrent BLE reference + *strictly receive-only* serial investigation.

    BLE remains the ONLY source of MQTT readings and the dashboard. This
    optional task never polls RS485, publishes raw payloads or changes stored
    serial/Bluetooth slot associations.
    """
    port = str(settings.get("serial_port") or "").strip()
    if not port:
        LOG.warning(
            "Mode dual : port RS485 absent. Renseigner serial_port ; "
            "le Bluetooth et MQTT fonctionnent néanmoins normalement"
        )
        return
    port = validate_port(port)
    baudrate = int(settings.get("serial_baudrate", 9600))
    allowed_text = str(settings.get("serial_addresses") or "").strip()
    allowed = set(parse_serial_addresses(allowed_text)) if allowed_text else None
    monitor: PassiveSerialMonitor | None = None
    pending: dict[int, tuple[bytes, float]] = {}
    vote: dict[int, tuple[int, int]] = {}
    previous_report = time.monotonic() - 30
    total_telegrams = 0
    total_v23 = 0

    LOG.info(
        "MODE DUAL BLE + RS485 PASSIF : Bluetooth = seule source MQTT ; "
        "RS485 en réception uniquement, aucune commande envoyée ; "
        "port %s ; vitesse %d",
        port, baudrate,
    )
    try:
        while True:
            try:
                if monitor is None:
                    monitor = PassiveSerialMonitor(port, baudrate)
                    await asyncio.to_thread(monitor.open)
                    LOG.info("Dual : port RS485 ouvert en écoute passive (zéro polling)")
                _, stats = await asyncio.to_thread(monitor.read_window, 4.0, True)
                now = time.monotonic()
                total_telegrams += stats["frames"]
                for addr, payload in stats["probe_payloads"].items():
                    if allowed is None or addr in allowed:
                        pending[addr] = (payload, now)
                        total_v23 += 1
                # Do not compare a stale serial sample with a current BLE value.
                recent = {
                    addr: payload
                    for addr, (payload, received_at) in pending.items()
                    if now - received_at <= 90
                }
                pending = {
                    addr: (payload, received_at)
                    for addr, (payload, received_at) in pending.items()
                    if now - received_at <= 90
                }
                if now - previous_report >= 30:
                    profiles = ble_cell_profiles(ble_store.snapshot()["batteries"])
                    findings = compare_payloads(recent, profiles)
                    LOG.info(
                        "Dual RS485/BLE (~30 s) : trames RS485 %d ; réponses "
                        "v23/85 captées %d ; références BLE %d ; adresses "
                        "RS485 %d ; aucun polling RS485",
                        total_telegrams, total_v23, len(profiles), len(recent),
                    )
                    for finding in findings:
                        addr = finding["address"]
                        LOG.info("Dual diagnostic : %s", summarize_finding(finding))
                        if finding["status"] == "candidate":
                            ble_slot = finding["ble_slot"]
                            old_slot, hits = vote.get(addr, (None, 0))
                            hits = hits + 1 if old_slot == ble_slot else 1
                            vote[addr] = (ble_slot, hits)
                            if hits >= 2:
                                LOG.info(
                                    "Dual correspondance PROBABLE (non enregistrée) : "
                                    "RS485 adresse %d ↔ Batterie Bluetooth %d "
                                    "(comparaison répétée %d fois) ; "
                                    "à confirmer sur matériel",
                                    addr, ble_slot, hits,
                                )
                        else:
                            vote.pop(addr, None)
                    if not findings:
                        LOG.info(
                            "Dual diagnostic : aucune empreinte RS485 à comparer. "
                            "Le décodage v23/85 reste non validé ; "
                            "les mesures Bluetooth restent seules publiées"
                        )
                    total_telegrams = 0
                    total_v23 = 0
                    previous_report = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning(
                    "Dual écoute RS485 interrompue (%s : %s) ; Bluetooth inchangé",
                    type(exc).__name__, str(exc)[:120],
                )
                if monitor is not None:
                    try:
                        await asyncio.to_thread(monitor.close)
                    except Exception:
                        pass
                    monitor = None
                await asyncio.sleep(5)
    finally:
        if monitor is not None:
            await asyncio.to_thread(monitor.close)


async def run() -> None:
    settings = load_options()
    logging.basicConfig(
        level=getattr(logging, str(settings["log_level"]).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    LOG.info("Smart Docan BMS 0.1.16-dev — comparateur Bluetooth + RS485 passif")
    transport = str(settings.get("transport", "bluetooth")).strip().lower()
    if transport == "serial":
        await run_serial(settings)
        return
    if transport not in ("bluetooth", "dual"):
        raise ValueError("Transport inconnu : bluetooth, serial ou dual")
    wanted = parse_addresses(settings.get("addresses", ""))
    slots = SlotAssignments(Path("/data/slots.json"), settings)
    if not settings.get("auto_discover", True) and not wanted and not slots.configured():
        LOG.warning("Aucun slot Bluetooth n'est configuré, découverte automatique désactivée")
    for slot, address in slots.configured():
        LOG.info("Emplacement actif : batterie %d", slot)
    for slot, address in slots.disabled():
        LOG.info("Emplacement désactivé : batterie %d (association conservée)", slot)
    polling = int(settings["poll_seconds"])
    store = Store(poll_seconds=polling)
    for slot, address in slots.configured():
        store.register_slot(slot, address, slots.get_name(slot))
    bridge: MQTTBridge | None = None
    next_mqtt_attempt = 0.0
    dual_task: asyncio.Task | None = None
    if transport == "dual":
        dual_task = asyncio.create_task(
            run_dual_serial_comparison(settings, store),
            name="docan-rs485-passive-comparison",
        )
    LOG.info("Cadence cible de lecture et publication BMS: %d s", polling)
    devices: dict[str, tuple] = {}  # address -> (BLEDevice, RSSI)
    known_info: dict[str, dict] = {}
    next_scan = 0.0
    scan_seconds = int(settings["scan_seconds"])
    rescan = int(settings["rescan_seconds"])
    missing_rescan = int(settings.get("missing_rescan_seconds", 30))
    recover_stale = bool(settings.get("recover_stale_connections", False))
    stale_after = int(settings.get("stale_connection_after_seconds", 180))
    last_attempts: dict[str, float] = {}
    last_success_at: dict[str, float] = {}
    missing_since: dict[int, float] = {}
    missed_scans: dict[int, int] = {}
    last_cleanup_at: dict[int, float] = {}
    previous_missing_slots: set[int] = set()
    LOG.info("Recherche BLE normale %d s, recherche si batterie absente %d s",
             rescan, missing_rescan)
    LOG.info(
        "Déconnexion BlueZ ciblée : %s ; absence minimale %d s ; "
        "limite 1 tentative par batterie et par heure",
        "activée" if recover_stale else "désactivée",
        stale_after,
    )

    async def read_one(address: str, device, rssi):
        slot = slots.slot_for(address)
        if slot is None:
            return
        name = slots.get_name(slot)
        store.scan(address, name, rssi, slot)
        bms = BMS(ble_device=device, config=BMSConfig(keep_alive=True))
        try:
            # A stalled BMS must not block all other packs indefinitely.
            async with asyncio.timeout(30):
                async with bms:
                    sample = await bms.async_update()
                    if not sample:
                        raise ValueError("Trame BMS vide")
                    if address not in known_info:
                        try:
                            info = await bms.device_info()
                            known_info[address] = dict(info)
                        except Exception as exc:
                            # Firmware info may be unavailable although readings work.
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
            previously_seen = bool(store.batteries[address].get("last_seen"))
            was_offline = not store.batteries[address].get("online")
            store.success(address, data, known_info[address], temp_diagnostic)
            if was_offline and previously_seen:
                LOG.info("%s : communication Bluetooth rétablie automatiquement", name)
            if count == 0:
                LOG.info("%s : températures absentes du décodage (%s)", name, temp_diagnostic)
            LOG.info("%s : SOC %s %% ; %s V ; %s cellules",
                     name, data.get("battery_level", "?"), data.get("voltage", "?"),
                     len([k for k in data if re.fullmatch(r"cell_\d+", k)]))
            return True
        except Exception as exc:
            store.failure(address, exc)
            LOG.warning("%s : lecture impossible (%s)", name, type(exc).__name__)
            return False
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
                    bridge = MQTTBridge(
                        mqtt_config, store,
                        migrate_legacy=bool(settings.get("migrate_legacy_entities", True)),
                    )
                except Exception as exc:
                    # MQTT failure must never stop Bluetooth telemetry.
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
                    eligible: dict[str, tuple] = {}
                    reserved_addresses = {address for _, address in slots.configured()}
                    for device, adv in found.values():
                        name = (adv.local_name or device.name or "").strip()
                        address = device.address.upper()
                        if not MAC_RE.fullmatch(address):
                            continue
                        if address in reserved_addresses:
                            pass
                        elif wanted:
                            if address not in wanted:
                                continue
                        elif not settings.get("auto_discover", True) or not name.startswith("DWF"):
                            continue
                        eligible[address] = (device, adv.rssi)

                    assigned = slots.bind_discovered(set(eligible))
                    for address, slot in assigned.items():
                        device, rssi = eligible[address]
                        store.scan(address, slots.get_name(slot), rssi, slot)
                        # Replace old BlueZ object with a fresh discovered device.
                        devices[address] = (device, rssi)

                    missing = set(missing_slots(slots.configured(), set(assigned)))
                    now_scan = time.monotonic()
                    for slot in missing:
                        missing_since.setdefault(slot, now_scan)
                        missed_scans[slot] = missed_scans.get(slot, 0) + 1
                    for slot in set(missed_scans) - missing:
                        missing_since.pop(slot, None)
                        missed_scans.pop(slot, None)
                    newly_missing = missing - previous_missing_slots
                    recovered = previous_missing_slots - missing
                    for slot in sorted(newly_missing):
                        LOG.warning(
                            "Batterie %d absente du scan BLE : son emplacement reste réservé ; "
                            "nouvelle recherche dans %d s", slot, missing_rescan
                        )
                    for slot in sorted(recovered):
                        LOG.info("Batterie %d de nouveau visible lors du scan Bluetooth", slot)
                    previous_missing_slots = missing
                    interval = scan_interval(rescan, missing_rescan, bool(missing))
                    next_scan = time.monotonic() + interval
                    LOG.info(
                        "Bluetooth : %d/%d batteries associées vues ; prochaine recherche dans %d s",
                        len(assigned), len(slots.configured()), interval
                    )
                except Exception as exc:
                    LOG.warning(
                        "Recherche Bluetooth indisponible (%s) : nouvel essai dans %d s",
                        type(exc).__name__, missing_rescan
                    )
                    next_scan = time.monotonic() + missing_rescan

            # Read every known BMS sequentially; pause only the failing BMS.
            # A missing BMS must not block healthy batteries 2-4.
            for address, (device, rssi) in sorted(
                list(devices.items()), key=lambda p: slots.slot_for(p[0]) or 99
            ):
                rec = store.batteries.get(address, {})
                failures = rec.get("failures", 0)
                if not is_attempt_due(failures, last_attempts.get(address),
                                      time.monotonic()):
                    continue
                ok = await read_one(address, device, rssi)
                if ok:
                    last_attempts.pop(address, None)
                    last_success_at[address] = time.monotonic()
                else:
                    last_attempts[address] = time.monotonic()
                    failures = store.batteries[address]["failures"]
                    if failures >= 3:
                        # The BLEDevice's BlueZ path might be stale. Force a fresh
                        # advertisement before connecting again; do not reset hci0.
                        devices.pop(address, None)
                        next_scan = min(next_scan, time.monotonic() + 3)
                        LOG.warning(
                            "Batterie %d : %d erreurs Bluetooth ; abandon de la référence "
                            "périmée et recherche accélérée (sans redémarrer le Bluetooth)",
                            slots.slot_for(address), failures
                        )
            # Opt-in recovery for a BlueZ connection that is stuck while the
            # assigned battery no longer advertises and cannot be read.
            # This affects only that BLE address, never hci0 or healthy slots.
            if recover_stale:
                for slot, address in slots.configured():
                    now = time.monotonic()
                    if not can_recover_stale_connection(
                        enabled=True,
                        missed_scans=missed_scans.get(slot, 0),
                        missing_since=missing_since.get(slot),
                        last_success=last_success_at.get(address),
                        last_cleanup=last_cleanup_at.get(slot),
                        now=now,
                        absent_seconds=stale_after,
                    ):
                        continue
                    last_cleanup_at[slot] = now  # also rate-limit failures
                    LOG.warning(
                        "Batterie %d : toujours absente du scan et sans lecture "
                        "depuis %d s, tentative de déconnexion BlueZ ciblée",
                        slot, stale_after,
                    )
                    try:
                        async with asyncio.timeout(12):
                            await close_stale_connections_by_address(address)
                        LOG.info(
                            "Batterie %d : demande de nettoyage BlueZ terminée ; "
                            "recherche du BMS relancée", slot
                        )
                    except Exception as exc:
                        LOG.warning(
                            "Batterie %d : déconnexion ciblée refusée/échouée (%s). "
                            "Aucune modification des permissions ou de hci0",
                            slot, type(exc).__name__,
                        )
                    # Don't reuse a potentially stale BLEDevice instance.
                    devices.pop(address, None)
                    next_scan = min(next_scan, time.monotonic() + 2)

            if bridge is not None:
                # Previously discovered MQTT entities must not remain online if
                # their slot was explicitly disabled. Do not delete unique IDs.
                for disabled_slot, _ in slots.disabled():
                    bridge.publish(
                        f"{PREFIX}/{slot_device_id(disabled_slot)}/availability",
                        "offline",
                    )
                snapshot = store.snapshot()
                # Publish offline availability even for currently missing assigned slots.
                for rec in snapshot["batteries"]:
                    if not rec["online"]:
                        bridge.battery(rec)
                bridge.summary(snapshot["aggregate"])
            elapsed = time.monotonic() - cycle_started
            store.cycle_finished(elapsed)
            remaining = remaining_poll_delay(elapsed, polling)
            if remaining == 0:
                LOG.debug("Le cycle BMS (%.1f s) atteint/dépasse la période cible (%d s)", elapsed, polling)
            await asyncio.sleep(remaining)
    finally:
        if dual_task is not None:
            dual_task.cancel()
            try:
                await dual_task
            except asyncio.CancelledError:
                pass
        if bridge is not None:
            bridge.stop()

def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        LOG.info("Arrêt de l'add-on")

if __name__ == "__main__":
    main()
