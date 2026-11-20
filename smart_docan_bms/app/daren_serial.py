"""Read-only Daren DR-1363 / YD-T1363.1 serial RS485 pilot.

Based on the documented service 0x42 framing in cpttinkering/daren-485
(MIT). Not all Daren/Docan firmwares use the same RS485 protocol.

Only service 0x42 (GetDeviceInfo) is ever requested. This file exposes no
configuration, switch, EEPROM or firmware commands.
"""
from __future__ import annotations

from pathlib import Path
import re
import struct
import time

ASCII_HEX = re.compile(r"^[0-9A-Fa-f]+$")
SUPPORTED_BAUDS = {9600, 19200}
DEFAULT_SCAN_IDS = (0, 1, 2, 3, 4)


class DarenSerialError(ValueError):
    """Invalid or incompatible response, safe to report without raw data."""


# Documented alarm/protection bits for Daren DR-1363 service 0x42.
# See cpttinkering/daren-485, Daren485.get_realtime_data(): not all
# non-zero status bits indicate protection. In particular CURRENT bit 1
# (0x0002) is NOT documented as an overcurrent/short-circuit alarm.
#
# Voltage: cell/pack over/under-voltage, warnings, imbalance, fuse.
# Current: CHG_OC_PROT, short circuit, DISCH_OC1/2_PROT, CHG/DISCH_OC_ALARM.
# Temperature: charge/discharge/internal over/under-temperature and alarms.
# Warning: internal component failures bits 1..6 and low battery bit 7.
#
# Preserve the original four raw words in problem_code for diagnostics.
VOLTAGE_ALARM_MASK = 0x61FF   # bits 0..8, 13, 14
CURRENT_ALARM_MASK = 0x00FC   # bits 2..7 (NOT bit 1)
TEMPERATURE_ALARM_MASK = 0x7F7F  # bits 0..6 and 8..14
WARNING_ALARM_MASK = 0x00FE  # bits 1..7


def active_alarm_flags(voltage: int, current: int, temperature: int, warning: int) -> tuple[int, int, int, int]:
    """Return only verified alarm bits; do not treat ordinary status flags as faults."""
    return (
        voltage & VOLTAGE_ALARM_MASK,
        current & CURRENT_ALARM_MASK,
        temperature & TEMPERATURE_ALARM_MASK,
        warning & WARNING_ALARM_MASK,
    )


def validate_port(path: str) -> str:
    path = str(path or "").strip()
    if not path:
        raise ValueError("Champ serial_port vide : indiquez le port série USB")
    if not path.startswith("/dev/") or ".." in Path(path).parts:
        raise ValueError("Le port série doit être un chemin /dev/...")
    return path


def parse_serial_addresses(value: str) -> tuple[int, ...]:
    """Blank probes Docan PACK0 (host) and PACK1..PACK4 (up to four slaves)."""
    if not str(value or "").strip():
        return DEFAULT_SCAN_IDS
    parts = [x for x in re.split(r"[,;\s]+", str(value).strip()) if x]
    ids: list[int] = []
    for text in parts:
        if not text.isdigit() or not 0 <= int(text) <= 15:
            raise ValueError("Adresses RS485 attendues : 0 à 15, séparées par virgule")
        address = int(text)
        if address not in ids:
            ids.append(address)
    return tuple(ids)


def length_checksum(length: int) -> int:
    if not 0 <= length <= 0x0FFF:
        raise ValueError("Trame RS485 trop longue")
    nibble_sum = (length & 15) + ((length >> 4) & 15) + ((length >> 8) & 15)
    return length | (((-nibble_sum) & 15) << 12)


def checksum(text: str) -> int:
    return (-sum(text.encode("ascii"))) & 0xFFFF


def read_request(address: int) -> bytes:
    """Only GetDeviceInfo 0x42. Explicitly no write commands."""
    if not 0 <= address <= 15:
        raise ValueError("Adresse RS485 invalide : 0 à 15")
    # Docan PACK0 (host) is a real device address in this battery family.
    # This is a read-only DR-1363 command, not Modbus RTU broadcast.
    # The information field is the addressed battery's address as ASCII hex.
    prefix = f"22{address:02X}4A42{length_checksum(2):04X}{address:02X}"
    return f"~{prefix}{checksum(prefix):04X}\r".encode("ascii")


def decode_response(frame: bytes, expected_address: int) -> dict:
    """Validate protocol framing/checksums then decode dynamic 16S telemetry."""
    if not frame or len(frame) > 512:
        raise DarenSerialError("Aucune réponse RS485 valide")
    try:
        text = frame.decode("ascii")
    except UnicodeDecodeError as exc:
        raise DarenSerialError("Réponse non ASCII : protocole RS485 différent") from exc
    if not text.startswith("~") or not text.endswith("\r") or len(text) < 18:
        raise DarenSerialError("Trame RS485 incomplète")
    core = text[1:-5]
    transmitted = text[-5:-1]
    if not ASCII_HEX.fullmatch(core) or not ASCII_HEX.fullmatch(transmitted):
        raise DarenSerialError("Trame RS485 non hexadécimale")
    if checksum(core) != int(transmitted, 16):
        raise DarenSerialError("Checksum RS485 invalide")
    if core[:2].upper() != "22" or int(core[2:4], 16) != expected_address or core[4:6].upper() != "4A":
        raise DarenSerialError("Réponse d'une autre adresse ou protocole")
    if core[6:8].upper() != "00":
        raise DarenSerialError("BMS RS485 : réponse d'erreur")
    length_id = int(core[8:12], 16)
    payload_hex = core[12:]
    if length_id != length_checksum(len(payload_hex)):
        raise DarenSerialError("Longueur RS485 incohérente")
    if len(payload_hex) % 2:
        raise DarenSerialError("Longueur de télémétrie impaire")
    try:
        raw = bytes.fromhex(payload_hex)
        idx = 0
        def u8():
            nonlocal idx
            if idx >= len(raw):
                raise DarenSerialError("Données RS485 incomplètes")
            v = raw[idx]
            idx += 1
            return v
        def u16():
            nonlocal idx
            if idx + 2 > len(raw):
                raise DarenSerialError("Données RS485 incomplètes")
            v = struct.unpack_from(">H", raw, idx)[0]
            idx += 2
            return v
        def s16():
            nonlocal idx
            if idx + 2 > len(raw):
                raise DarenSerialError("Données RS485 incomplètes")
            v = struct.unpack_from(">h", raw, idx)[0]
            idx += 2
            return v

        _data_flag = u8()
        soc = u16() / 100
        voltage = u16() / 100
        cells_count = u8()
        if not 8 <= cells_count <= 32:
            raise DarenSerialError("Nombre de cellules incompatible")
        cells = [u16() / 1000 for _ in range(cells_count)]
        ambient = s16() / 10
        pack_temp = s16() / 10
        mos_temp = s16() / 10
        cell_temp_count = u8()
        if cell_temp_count > 16:
            raise DarenSerialError("Nombre de sondes incompatible")
        cell_temps = [s16() / 10 for _ in range(cell_temp_count)]
        current = s16() / 100
        _resistance = u16()
        soh = u16()
        _user_code = u8()
        capacity = u16() / 100
        remaining = u16() / 100
        cycles = u16()
        voltage_flags = u16()
        current_flags = u16()
        temp_flags = u16()
        warning_flags = u16()
        fet_flags = u16()
        # Tail of service 42 contains 12 optional status words. Decode if present.
        tail_words = []
        while idx + 2 <= len(raw) and len(tail_words) < 12:
            tail_words.append(u16())
    except (struct.error, IndexError) as exc:
        raise DarenSerialError("Trame de télémétrie RS485 tronquée") from exc

    if not (0 <= soc <= 100 and 10 <= voltage <= 70 and all(1.5 <= v <= 4.6 for v in cells)):
        raise DarenSerialError("Valeurs RS485 incohérentes : protocole non confirmé")
    if not 0 <= soh <= 100 or capacity < 0 or remaining < 0:
        raise DarenSerialError("Capacité ou SOH RS485 incohérents")

    validated_alarms = active_alarm_flags(
        voltage_flags, current_flags, temp_flags, warning_flags
    )
    # Protect against false positives: these words mix status and alarm bits.
    # E.g. current_flags == 0x0002 alone is not a documented overcurrent alarm.
    # Raw status words are still retained for diagnostics.
    sample = {
        "battery_level": round(soc, 2),
        "voltage": voltage,
        "cell_voltages": cells,
        "current": current,
        "power": round(voltage * current, 2),
        "battery_health": soh,
        "design_capacity": capacity,
        "cycle_charge": remaining,
        "cycles": cycles,
        "temperature": pack_temp,
        "temp_values": [ambient, pack_temp, mos_temp] + cell_temps,
        "temp_sensors": 3 + cell_temp_count,
        "problem": any(validated_alarms),
        # problem_code is the already-known MQTT entity: actual alarm bits only.
        # Keep the unmodified registers in a new diagnostic sensor so status
        # information (including current bit 1) is never lost.
        "problem_code": "/".join(f"{word:04X}" for word in validated_alarms),
        "status_code_raw": f"{voltage_flags:04X}/{current_flags:04X}/{temp_flags:04X}/{warning_flags:04X}",
        "chrg_mosfet": bool(fet_flags & 1),
        "dischrg_mosfet": bool(fet_flags & 2),
        "battery_charging": current > 0.05,
    }
    if len(tail_words) >= 6:
        # After FET flags: OV protection, UV protection, OV alarm, UV alarm,
        # then the two cell-balancing words (low and high).
        balance_flags = tail_words[4] | (tail_words[5] << 16)
        sample["balancer"] = bool(balance_flags)
    if current < -0.2 and remaining > 0:
        sample["runtime"] = round(remaining / abs(current) * 3600)
    return sample


def _extract_frames(buffer: bytearray) -> list[bytes]:
    """Recover ASCII ~...CR frames from serial chunks, ignoring stray preamble.

    RS485 converters sometimes echo their own request or receive fragmented
    frames. Keep complete frames separated by CR until a valid reply arrives.
    A second '~' supersedes an incomplete/noisy prefix. Never interpret a
    binary frame as a Daren telemetry frame.
    """
    frames: list[bytes] = []
    while (end := buffer.find(b"\r")) >= 0:
        chunk = bytes(buffer[:end + 1])
        del buffer[:end + 1]
        start = chunk.rfind(b"~")
        if start >= 0:
            frames.append(chunk[start:])
    if len(buffer) > 1024:
        # A protocol with no CR delimiters must not grow the memory forever.
        start = buffer.rfind(b"~")
        if start >= 0 and len(buffer) - start <= 512:
            del buffer[:start]
        else:
            buffer.clear()
    return frames


def read_bms(port: str, baudrate: int, address: int, timeout: float = 2.2) -> dict:
    """Read one BMS, tolerating an echoed request and unrelated serial noise.

    Serial diagnostics intentionally log only counts/error classes; no raw
    frames, serial numbers or personal battery information are recorded.
    """
    validate_port(port)
    if int(baudrate) not in SUPPORTED_BAUDS:
        raise ValueError("Vitesse RS485 non prévue (9600 ou 19200)")
    import serial

    req = read_request(address)
    bytes_seen = 0
    non_ascii = 0
    echoed = 0
    rejected = 0
    last_error = "aucune trame complète"
    buffer = bytearray()
    with serial.Serial(
        port, baudrate=int(baudrate), timeout=min(timeout, 0.25),
        write_timeout=timeout, bytesize=8, parity="N", stopbits=1,
    ) as ser:
        ser.reset_input_buffer()
        ser.write(req)
        ser.flush()
        deadline = time.monotonic() + timeout
        # One query per address: never send additional commands on a noisy bus.
        while time.monotonic() < deadline and bytes_seen < 2048:
            received = ser.read_until(b"\r", size=512)
            if not received:
                continue
            bytes_seen += len(received)
            non_ascii += sum(c not in (10, 13) and not 32 <= c <= 126 for c in received)
            buffer.extend(received)
            for frame in _extract_frames(buffer):
                if frame == req:
                    echoed += 1
                    continue
                try:
                    return decode_response(frame, address)
                except DarenSerialError as exc:
                    rejected += 1
                    last_error = str(exc)
                    # One bad reply must not hide a later valid reply.

    if bytes_seen == 0:
        raise TimeoutError(
            "Aucun octet reçu (adresse RS485, A/B, vitesse, protocole ou bus)"
        )
    raise DarenSerialError(
        "Aucune réponse Daren valide : "
        f"octets={bytes_seen}, échos={echoed}, "
        f"octets_non_ASCII={non_ascii}, trames_rejetées={rejected} ; "
        f"dernier_défaut={last_error}"
    )


def _plausible_passive_telemetry(sample: dict) -> bool:
    """Sanity gates for *unverified* v23/85 layout compatibility.

    Correct checksum alone says nothing about the meaning of the data bytes.
    In particular, never publish guessed battery data unless independent pack
    voltage and summed cell voltages agree closely.
    """
    cells = sample.get("cell_voltages") or []
    voltage = sample.get("voltage")
    temperatures = sample.get("temp_values") or []
    capacity = sample.get("design_capacity")
    remaining = sample.get("cycle_charge")
    try:
        return (
            8 <= len(cells) <= 32
            and 10 <= voltage <= 70
            and abs(sum(cells) - voltage) <= max(0.75, 0.02 * voltage)
            and all(1.5 <= cell <= 4.6 for cell in cells)
            and -400 <= sample.get("current") <= 400
            and len(temperatures) >= 3
            and all(-40 <= t <= 105 for t in temperatures)
            and 10 <= capacity <= 2000
            and 0 <= remaining <= capacity * 1.15
            and 0 <= sample.get("cycles") <= 100000
        )
    except (TypeError, ValueError):
        return False


def decode_passive_response(frame: bytes) -> tuple[int, dict, str]:
    """Parse documented v22/00 telemetry or guarded v23/85 candidate layouts.

    The user's Docan Master sends ASCII v23/4A/84 requests and v23/4A/85
    replies of 98 data bytes. This v23 payload layout is NOT yet independently
    documented; try only two one-byte differences vs the v22/00 99-byte
    reference. A response is accepted only if the framing is correct and
    exactly one candidate yields physically coherent voltage and cell data.

    Never transmit or alter any BMS. No raw payload is logged.
    """
    if not frame or len(frame) > 512:
        raise DarenSerialError("Trame passive absente ou trop longue")
    try:
        text = frame.decode("ascii")
    except UnicodeDecodeError as exc:
        raise DarenSerialError("Trame passive non ASCII") from exc
    if not text.startswith("~") or not text.endswith("\r") or len(text) < 18:
        raise DarenSerialError("Trame passive non encadrée")
    core = text[1:-5]
    transmitted = text[-5:-1]
    if (
        len(core) < 12 or (len(core) - 12) % 2
        or not ASCII_HEX.fullmatch(core)
        or not ASCII_HEX.fullmatch(transmitted)
    ):
        raise DarenSerialError("Trame passive non hexadécimale")
    if checksum(core) != int(transmitted, 16):
        raise DarenSerialError("Checksum RS485 passif invalide")
    if int(core[8:12], 16) != length_checksum(len(core) - 12):
        raise DarenSerialError("Longueur RS485 passive incohérente")
    version, addr_text, cid1, cid2 = (
        core[:2].upper(), core[2:4], core[4:6].upper(), core[6:8].upper()
    )
    if cid1 != "4A":
        raise DarenSerialError("Service RS485 non Daren 4A")
    address = int(addr_text, 16)
    if not 0 <= address <= 15:
        raise DarenSerialError("Adresse RS485 non prise en charge")
    if version == "22" and cid2 == "00":
        return address, decode_response(frame, address), "v22/00"
    if version != "23" or cid2 != "85":
        raise DarenSerialError("Service RS485 non télémétrique")
    raw = bytes.fromhex(core[12:])
    if len(raw) != 98:
        raise DarenSerialError("Longueur v23/85 inattendue : pas de décodage")

    candidates: list[tuple[str, dict]] = []
    for layout, conventional in (
        ("v23/85_sans_flag_initial", b"\x00" + raw),
        ("v23/85_sans_octet_final", raw + b"\x00"),
    ):
        # Reuse the established v22 parser rather than duplicating numerous
        # offsets, but never accept its output without independent checks.
        old_core = (
            f"22{address:02X}4A00{length_checksum(len(conventional) * 2):04X}"
            + conventional.hex().upper()
        )
        old_frame = f"~{old_core}{checksum(old_core):04X}\r".encode("ascii")
        try:
            sample = decode_response(old_frame, address)
        except DarenSerialError:
            continue
        if _plausible_passive_telemetry(sample):
            candidates.append((layout, sample))

    if len(candidates) != 1:
        if not candidates:
            raise DarenSerialError(
                "Trame v23/85 intègre, mais aucune disposition de données validée"
            )
        raise DarenSerialError(
            "Trame v23/85 ambiguë : plusieurs dispositions plausibles"
        )
    layout, sample = candidates[0]
    return address, sample, layout


def _passive_telemetry_address(frame: bytes) -> int | None:
    """Find addressed service-42-like response; reject master requests.

    Candidate responses: v22 / 4A / 00, or v23 / 4A / 85.
    Master polling requests such as v23 / 4A / 84 are explicitly ignored.
    A separate decoder validates checksums and data plausibility.
    """
    try:
        text = frame.decode("ascii")
        if not text.startswith("~") or len(text) < 18:
            return None
        version = text[1:3].upper()
        service = text[5:9].upper()
        if (version, service) not in (("22", "4A00"), ("23", "4A85")):
            return None
        address = int(text[3:5], 16)
        if not 0 <= address <= 15:
            return None
        return address
    except (UnicodeDecodeError, ValueError):
        return None



def passive_frame_signature(frame: bytes) -> str:
    """Privacy-preserving description of a captured serial frame.

    Only log framing metadata, not raw telegrams, payload, serial numbers,
    battery measurements or MAC addresses. This distinguishes service-42
    replies from the different commands a real Master may actually use.
    """
    try:
        line = frame.decode("ascii")
    except UnicodeDecodeError:
        return "non_ASCII"
    if not line.startswith("~") or not line.endswith("\r") or len(line) < 18:
        return "hors_format"
    core, crc = line[1:-5], line[-5:-1]
    if (
        len(core) < 12 or len(core[12:]) % 2
        or not ASCII_HEX.fullmatch(core)
        or not ASCII_HEX.fullmatch(crc)
    ):
        return "non_hexadecimal"
    version = core[0:2].upper()
    address = core[2:4].upper()
    cid1 = core[4:6].upper()
    cid2 = core[6:8].upper()
    body_chars = len(core) - 12
    payload_bytes = body_chars // 2
    checksum_ok = checksum(core) == int(crc, 16)
    length_ok = length_checksum(body_chars) == int(core[8:12], 16)
    # The fields below are protocol header metadata only. No DATAI is logged.
    return (
        f"v{version}-a{address}-c{cid1}-k{cid2}-"
        f"{payload_bytes}o-"
        f"{'crcOK' if checksum_ok else 'crcKO'}-"
        f"{'lenOK' if length_ok else 'lenKO'}"
    )


class PassiveSerialMonitor:
    """Single persistent RS485 receive-only connection, **never transmits**.

    This is intended for buses on which an existing BMS Master already queries
    slave batteries. Passive reception is only possible if that traffic crosses
    the USB-RS485 adapter and contains usable service-42 responses. The Master
    pack's own telemetry may not be visible when it never replies on that bus.
    """

    def __init__(self, port: str, baudrate: int, timeout: float = 0.20):
        self.port = validate_port(port)
        if int(baudrate) not in SUPPORTED_BAUDS:
            raise ValueError("Vitesse RS485 non prévue (9600 ou 19200)")
        self.baudrate = int(baudrate)
        self.timeout = timeout
        self._port = None
        self._pending = bytearray()

    def open(self) -> None:
        if self._port is not None:
            return
        import serial

        # Important: no reset_input_buffer() and no write()/flush(). It is
        # strictly a monitor of traffic that already exists on the bus.
        self._port = serial.Serial(
            self.port, baudrate=self.baudrate, timeout=self.timeout,
            bytesize=8, parity="N", stopbits=1,
        )

    def read_window(self, seconds: float = 4.0) -> tuple[dict[int, dict], dict[str, int]]:
        if self._port is None:
            raise RuntimeError("Port d'écoute passive non ouvert")
        deadline = time.monotonic() + max(0.01, min(float(seconds), 15.0))
        records: dict[int, dict] = {}
        stats = {"bytes": 0, "frames": 0, "responses": 0,
                 "other_frames": 0, "rejected": 0, "signatures": {},
                 "layouts": {}, "reject_reasons": {}}
        while time.monotonic() < deadline:
            chunk = self._port.read_until(b"\r", size=512)
            if not chunk:
                continue
            stats["bytes"] += len(chunk)
            self._pending.extend(chunk)
            for frame in _extract_frames(self._pending):
                stats["frames"] += 1
                signature = passive_frame_signature(frame)
                kinds = stats["signatures"]
                kinds[signature] = kinds.get(signature, 0) + 1
                address = _passive_telemetry_address(frame)
                if address is None:
                    stats["other_frames"] += 1
                    continue
                try:
                    decoded_address, sample, layout = decode_passive_response(frame)
                except DarenSerialError as exc:
                    stats["rejected"] += 1
                    # The exception is a fixed category, never a raw frame.
                    reasons = stats["reject_reasons"]
                    category = str(exc)[:90]
                    reasons[category] = reasons.get(category, 0) + 1
                    continue
                if decoded_address != address:
                    stats["rejected"] += 1
                    continue
                stats["responses"] += 1
                layouts = stats["layouts"]
                layouts[layout] = layouts.get(layout, 0) + 1
                records[address] = sample
        return records, stats

    def close(self) -> None:
        old = self._port
        self._port = None
        if old is not None:
            old.close()
