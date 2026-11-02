"""Wire protocol of the Beurer BF700 family (BF700/BF800, BF710, Sanitas SBF70/SBF75).

Pure Python, no Home Assistant or Bluetooth imports, so it can be unit tested
against captured frames.

All traffic goes through characteristic 0xFFE1 of service 0xFFE0. A frame starts
with a start byte whose high nibble identifies the vendor flavour (0xF for
Beurer BF700/BF800, 0xE for BF710/Sanitas) and whose low nibble is 7 for normal
commands. Three "alternative" frames use other low nibbles: 6 (init), 9 (set
time) and 0xA (disconnect).

Normal frames are ``start, command, payload...``. The scale confirms commands
with ``start, 0xF0, command, status, ...``; multi-part data frames
(``start, command, total, index, ...``) must each be confirmed by the app with
``start, 0xF1, command, total, index``.

Protocol knowledge comes from openScale's BeurerSanitas driver and from frames
captured on a real BF700 (firmware reporting version 6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

FLAVOUR_BEURER = 0xF0  # BF700, BF800, Runtastic Libra
FLAVOUR_SANITAS = 0xE0  # BF710, Sanitas SBF70/SBF75, SilverCrest, Crane

NIBBLE_INIT = 0x6
NIBBLE_CMD = 0x7
NIBBLE_SET_TIME = 0x9
NIBBLE_DISCONNECT = 0xA


class Cmd(IntEnum):
    """Command bytes (second byte of a normal frame)."""

    USER_ADD = 0x31
    USER_DELETE = 0x32
    USER_LIST = 0x33
    USER_INFO = 0x34
    USER_UPDATE = 0x35
    USER_DETAILS = 0x36
    DO_MEASUREMENT = 0x40
    GET_SAVED_MEASUREMENTS = 0x41
    SAVED_MEASUREMENT = 0x42
    DELETE_SAVED_MEASUREMENTS = 0x43
    GET_UNKNOWN_MEASUREMENTS = 0x46
    UNKNOWN_MEASUREMENT_INFO = 0x47
    DELETE_UNKNOWN_MEASUREMENT = 0x49
    ASSIGN_UNKNOWN_MEASUREMENT = 0x4B
    UNKNOWN_MEASUREMENT = 0x4C
    SET_UNIT = 0x4D
    SCALE_STATUS = 0x4F
    WEIGHT_MEASUREMENT = 0x58
    MEASUREMENT = 0x59
    SCALE_ACK = 0xF0
    APP_ACK = 0xF1


UNITS = {1: "kg", 2: "lb", 4: "st"}

# Raw units of the measurement record.
WEIGHT_STEP_KG = 0.05
BONE_STEP_KG = 0.05  # openScale's interpretation; see README "Known limitations"


class ProtocolError(Exception):
    """A frame could not be parsed."""


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "big")


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "big")


def decode_timestamp(raw: int) -> datetime:
    """Decode a scale timestamp (unix seconds, UTC).

    Some firmwares set bit 31 in live measurement frames; Unix time does not
    need it before 2038, so it is masked off.
    """
    return datetime.fromtimestamp(raw & 0x7FFF_FFFF, tz=UTC)


def decode_initials(data: bytes) -> str:
    """Decode the up-to-three character user name stored on the scale."""
    return data.split(b"\0", 1)[0].decode("ascii", errors="replace").strip()


# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------


def start_byte(flavour: int, nibble: int = NIBBLE_CMD) -> int:
    """Return the start byte for a flavour and frame kind."""
    return (flavour & 0xF0) | (nibble & 0x0F)


def build_command(flavour: int, cmd: int, payload: bytes = b"") -> bytes:
    """Build a normal command frame."""
    return bytes((start_byte(flavour), cmd)) + payload


def build_init(flavour: int) -> bytes:
    """Build the hello frame the scale must acknowledge first."""
    return bytes((start_byte(flavour, NIBBLE_INIT), 0x01))


def build_set_time(flavour: int, when: datetime) -> bytes:
    """Build the frame that sets the scale clock (no ACK is sent back)."""
    return bytes((start_byte(flavour, NIBBLE_SET_TIME),)) + int(
        when.timestamp()
    ).to_bytes(4, "big")


def build_disconnect(flavour: int) -> bytes:
    """Build the frame that asks the scale to drop the connection."""
    return bytes((start_byte(flavour, NIBBLE_DISCONNECT),))


def build_app_ack(flavour: int, frame: bytes) -> bytes:
    """Build the app's acknowledgement of a multi-part data frame."""
    return build_command(flavour, Cmd.APP_ACK, frame[1:4])


def encode_uid(uid: int) -> bytes:
    """Encode a scale user id (8 bytes, big endian)."""
    return uid.to_bytes(8, "big")


# ---------------------------------------------------------------------------
# Parsed data
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScaleUser:
    """A user profile stored on the scale."""

    uid: int
    initials: str
    birth_year: int

    @property
    def key(self) -> str:
        """Stable identifier used for entity unique ids."""
        return f"{self.uid:016x}"


@dataclass(frozen=True, slots=True)
class ScaleStatus:
    """Answer to SCALE_STATUS."""

    result: int
    battery: int
    weight_threshold_kg: float
    body_fat_threshold_pct: float
    unit: str | None
    version: int


@dataclass(frozen=True, slots=True)
class Measurement:
    """One weigh-in. Body composition is ``None`` when the scale did not provide it."""

    timestamp: datetime
    weight: float
    impedance: int | None = None
    body_fat: float | None = None
    water: float | None = None
    muscle: float | None = None
    bone: float | None = None
    bmr: int | None = None
    amr: int | None = None
    bmi: float | None = None
    user_uid: int | None = None
    source: str = "saved"  # saved | live | unassigned
    unknown_index: int | None = None

    @property
    def has_body_composition(self) -> bool:
        """Return True if the scale computed body fat for this weigh-in."""
        return self.body_fat is not None


def parse_scale_ack(frame: bytes) -> tuple[int, bytes]:
    """Return (acknowledged command, remaining bytes) of a SCALE_ACK frame."""
    if len(frame) < 3 or frame[1] != Cmd.SCALE_ACK:
        raise ProtocolError(f"not a scale ack: {frame.hex()}")
    return frame[2], frame[3:]


def parse_status(frame: bytes) -> ScaleStatus:
    """Parse the SCALE_ACK for SCALE_STATUS."""
    if len(frame) < 12:
        raise ProtocolError(f"short status frame: {frame.hex()}")
    return ScaleStatus(
        result=frame[3],
        battery=frame[4],
        weight_threshold_kg=frame[5] / 10,
        body_fat_threshold_pct=frame[6] / 10,
        unit=UNITS.get(frame[7]),
        version=frame[11],
    )


def parse_user_info(frame: bytes) -> tuple[int, int, ScaleUser]:
    """Parse a USER_INFO frame. Returns (total, index, user)."""
    if len(frame) < 16 or frame[1] != Cmd.USER_INFO:
        raise ProtocolError(f"bad user info frame: {frame.hex()}")
    user = ScaleUser(
        uid=_u32(frame, 4) << 32 | _u32(frame, 8),
        initials=decode_initials(frame[12:15]),
        birth_year=1900 + frame[15],
    )
    return frame[2], frame[3], user


def _all_zero(data: bytes) -> bool:
    return not any(data)


def parse_measurement_record(
    record: bytes, *, user_uid: int | None, source: str
) -> Measurement:
    """Parse the 22-byte measurement record (two 11-byte halves).

    Layout (big endian): timestamp u32, weight u16 (50 g), impedance u16,
    body fat u16 (0.1 %), water u16 (0.1 %), muscle u16 (0.1 %), bone u16,
    BMR u16 (kcal), AMR u16 (kcal), BMI u16 (0.1).

    Some firmwares send the first half twice instead of the second half for
    stored measurements; in that case only timestamp, weight, impedance and
    body fat are trustworthy.
    """
    if len(record) < 11:
        raise ProtocolError(f"short measurement record: {record.hex()}")
    head = record[:11]
    tail = record[11:22]
    second_half_valid = len(tail) == 11 and tail != head

    impedance = _u16(head, 6)
    has_composition = impedance != 0 and _u16(head, 8) != 0

    def pct(value: int) -> float | None:
        return round(value / 10, 1) if has_composition else None

    m = Measurement(
        timestamp=decode_timestamp(_u32(head, 0)),
        weight=round(_u16(head, 4) * WEIGHT_STEP_KG, 2),
        impedance=impedance or None,
        body_fat=pct(_u16(head, 8)),
        user_uid=user_uid,
        source=source,
    )
    if not second_half_valid:
        return m

    full = head + tail
    bmi_raw = _u16(full, 20)
    return Measurement(
        timestamp=m.timestamp,
        weight=m.weight,
        impedance=m.impedance,
        body_fat=m.body_fat,
        water=pct(_u16(full, 10)),
        muscle=pct(_u16(full, 12)),
        bone=round(_u16(full, 14) * BONE_STEP_KG, 2) if has_composition else None,
        bmr=_u16(full, 16) or None,
        amr=_u16(full, 18) or None,
        bmi=round(bmi_raw / 10, 1) if bmi_raw else None,
        user_uid=user_uid,
        source=source,
    )


def parse_unknown_measurement(frame: bytes) -> tuple[int, int, Measurement]:
    """Parse an UNKNOWN_MEASUREMENT_INFO frame (a weigh-in no user was matched to).

    Layout: start, 0x47, total, index, slot, timestamp u32, weight u16, impedance u16.
    """
    if len(frame) < 13 or frame[1] != Cmd.UNKNOWN_MEASUREMENT_INFO:
        raise ProtocolError(f"bad unknown measurement frame: {frame.hex()}")
    impedance = _u16(frame, 11)
    return (
        frame[2],
        frame[3],
        Measurement(
            timestamp=decode_timestamp(_u32(frame, 5)),
            weight=round(_u16(frame, 9) * WEIGHT_STEP_KG, 2),
            impedance=impedance or None,
            source="unassigned",
            unknown_index=frame[4],
        ),
    )


def parse_live_weight(frame: bytes) -> tuple[bool, float]:
    """Parse a WEIGHT_MEASUREMENT frame. Returns (stable, weight in kg)."""
    if len(frame) < 5 or frame[1] != Cmd.WEIGHT_MEASUREMENT:
        raise ProtocolError(f"bad weight frame: {frame.hex()}")
    return frame[2] == 0, round(_u16(frame, 3) * WEIGHT_STEP_KG, 2)


@dataclass
class MultiPartCollector:
    """Reassembles the two-frame measurement records of SAVED_MEASUREMENT / MEASUREMENT."""

    records: list[bytes] = field(default_factory=list)
    _pending: bytes | None = None

    def add_saved(self, frame: bytes) -> bool:
        """Add a SAVED_MEASUREMENT frame. Returns True when this was the last part."""
        total, index = frame[2], frame[3]
        payload = frame[4:]
        if index % 2 == 1:
            self._pending = payload
        elif self._pending is not None:
            self.records.append(self._pending + payload)
            self._pending = None
        return index >= total


def parse_live_measurement_frames(frames: list[bytes]) -> Measurement:
    """Parse the three MEASUREMENT frames of a live weigh-in.

    Part 1 carries the matched user's uid at offset 5, parts 2 and 3 the record.
    """
    by_index = {f[3]: f for f in frames if len(f) >= 4 and f[1] == Cmd.MEASUREMENT}
    if 1 not in by_index or 2 not in by_index:
        raise ProtocolError("incomplete live measurement")
    head = by_index[1]
    uid = _u32(head, 5) << 32 | _u32(head, 9) if len(head) >= 13 else 0
    record = by_index[2][4:] + (by_index[3][4:] if 3 in by_index else b"")
    return parse_measurement_record(record, user_uid=uid or None, source="live")


def activity_stamp(service_data: dict[str, bytes]) -> int | None:
    """Extract the activity timestamp the scale encodes in its advertisement.

    The scale advertises a 16-bit service data UUID plus two data bytes that
    together form a big-endian unix timestamp of its last activity (last
    weigh-in or last time sync), e.g. UUID 0x856A + ``58 03`` -> 0x6A855803.
    Home Assistant merges service data across advertisements, so the newest
    (largest) stamp wins.
    """
    best: int | None = None
    for uuid, value in service_data.items():
        if len(value) != 2 or not uuid.endswith("-0000-1000-8000-00805f9b34fb"):
            continue
        short = int(uuid[4:8], 16)
        stamp = int.from_bytes(short.to_bytes(2, "little") + value, "big")
        if best is None or stamp > best:
            best = stamp
    return best
