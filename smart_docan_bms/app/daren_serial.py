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
DEFAULT_SCAN_IDS = (1, 2, 3, 4)


class DarenSerialError(ValueError):
    """Invalid or incompatible response, safe to report without raw data."""


def validate_port(path: str) -> str:
    path = str(path or "").strip()
    if not path:
        raise ValueError("Champ serial_port vide : indiquez le port série USB")
    if not path.startswith("/dev/") or ".." in Path(path).parts:
        raise ValueError("Le port série doit être un chemin /dev/...")
    return path


def parse_serial_addresses(value: str) -> tuple[int, ...]:
    """Blank means probe the first four bus addresses; never address 0/broadcast."""
    if not str(value or "").strip():
        return DEFAULT_SCAN_IDS
    parts = [x for x in re.split(r"[,;\s]+", str(value).strip()) if x]
    ids: list[int] = []
    for text in parts:
        if not text.isdigit() or not 1 <= int(text) <= 15:
            raise ValueError("Adresses RS485 attendues : 1 à 15, séparées par virgule")
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
    if not 1 <= address <= 15:
        raise ValueError("Adresse RS485 invalide : 1 à 15")
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

    alarms = voltage_flags | current_flags | temp_flags | warning_flags
    # Clear status flags (0 = no event); avoid assuming BMS has no alarm
    # if a response lacks the corresponding fields.
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
        "problem": bool(alarms),
        "problem_code": f"{voltage_flags:04X}/{current_flags:04X}/{temp_flags:04X}/{warning_flags:04X}",
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
