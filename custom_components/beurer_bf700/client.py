"""BLE session with a Beurer BF700-family scale (bleak based, no Home Assistant imports)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import time

from bleak import BleakClient
from bleak.exc import BleakError

from .protocol import (
    CHAR_UUID,
    FLAVOUR_BEURER,
    FLAVOUR_SANITAS,
    NIBBLE_INIT,
    Cmd,
    Measurement,
    MultiPartCollector,
    ProtocolError,
    ScaleStatus,
    ScaleUser,
    build_app_ack,
    build_command,
    build_init,
    build_set_time,
    encode_uid,
    parse_live_measurement_frames,
    parse_live_weight,
    parse_measurement_record,
    parse_status,
    parse_unknown_measurement,
    parse_user_info,
    start_byte,
)

_LOGGER = logging.getLogger(__name__)

FRAME_TIMEOUT = 5.0
SANITAS_NAME_HINTS = ("bf710", "sbf7", "aicdscale")


class ScaleSessionError(Exception):
    """The scale did not answer as expected."""


@dataclass
class LiveWeight:
    """A weight reading streamed while someone stands on the scale."""

    weight: float
    stable: bool


@dataclass
class SyncResult:
    """Everything read from the scale during one session."""

    flavour: int
    status: ScaleStatus | None = None
    users: list[ScaleUser] = field(default_factory=list)
    saved: dict[int, list[Measurement]] = field(default_factory=dict)
    unassigned: list[Measurement] = field(default_factory=list)
    live: list[Measurement] = field(default_factory=list)
    init_byte: int | None = None
    frames: list[str] = field(default_factory=list)
    started: datetime = field(default_factory=lambda: datetime.now(UTC))


def guess_flavour(name: str | None) -> int:
    """Pick the likely start-byte flavour from the advertised name."""
    lowered = (name or "").lower()
    if any(hint in lowered for hint in SANITAS_NAME_HINTS):
        return FLAVOUR_SANITAS
    return FLAVOUR_BEURER


class ScaleSession:
    """Request/response session on top of a connected BleakClient."""

    def __init__(
        self,
        client: BleakClient,
        *,
        flavour: int = FLAVOUR_BEURER,
        on_live_weight: Callable[[LiveWeight], None] | None = None,
        on_live_measurement: Callable[[Measurement], None] | None = None,
        record_frames: bool = True,
    ) -> None:
        self._client = client
        self.flavour = flavour
        self._rx: asyncio.Queue[bytes] = asyncio.Queue()
        self._on_live_weight = on_live_weight
        self._on_live_measurement = on_live_measurement
        self._live_parts: list[bytes] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._record = record_frames
        self.frames: list[str] = []
        self.live: list[Measurement] = []
        self.last_rx = time.monotonic()
        self.live_activity = asyncio.Event()

    # -- transport --------------------------------------------------------

    def _log(self, direction: str, frame: bytes) -> None:
        _LOGGER.debug("%s %s %s", self._client.address, direction, frame.hex())
        if self._record and len(self.frames) < 400:
            self.frames.append(f"{direction} {frame.hex()}")

    async def _write(self, frame: bytes) -> None:
        self._log("->", frame)
        await self._client.write_gatt_char(CHAR_UUID, frame, response=True)

    def _on_notify(self, _char: object, data: bytearray) -> None:
        frame = bytes(data)
        self._log("<-", frame)
        self.last_rx = time.monotonic()
        if len(frame) >= 2 and frame[0] == start_byte(self.flavour):
            if frame[1] == Cmd.WEIGHT_MEASUREMENT:
                self._handle_live_weight(frame)
                return
            if frame[1] == Cmd.MEASUREMENT:
                self._handle_live_measurement(frame)
                return
        self._rx.put_nowait(frame)

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _handle_live_weight(self, frame: bytes) -> None:
        try:
            stable, weight = parse_live_weight(frame)
        except ProtocolError:
            _LOGGER.debug("Ignoring malformed weight frame %s", frame.hex())
            return
        self.live_activity.set()
        if self._on_live_weight:
            self._on_live_weight(LiveWeight(weight=weight, stable=stable))

    def _handle_live_measurement(self, frame: bytes) -> None:
        self.live_activity.set()
        self._spawn(self._ack(frame))
        if len(frame) < 4:
            return
        if frame[3] == 1:
            self._live_parts = []
        self._live_parts.append(frame)
        if frame[3] < frame[2]:
            return
        try:
            measurement = parse_live_measurement_frames(self._live_parts)
        except ProtocolError as err:
            _LOGGER.debug("Discarding live measurement: %s", err)
            return
        finally:
            self._live_parts = []
        self.live.append(measurement)
        if self._on_live_measurement:
            self._on_live_measurement(measurement)

    async def _ack(self, frame: bytes) -> None:
        try:
            await self._write(build_app_ack(self.flavour, frame))
        except BleakError as err:
            _LOGGER.debug("Could not acknowledge %s: %s", frame.hex(), err)

    async def _expect(
        self, predicate: Callable[[bytes], bool], timeout: float = FRAME_TIMEOUT
    ) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._client.is_connected:
                raise ScaleSessionError("timed out waiting for the scale")
            try:
                frame = await asyncio.wait_for(self._rx.get(), remaining)
            except TimeoutError:
                raise ScaleSessionError("timed out waiting for the scale") from None
            if predicate(frame):
                return frame
            _LOGGER.debug("Ignoring unexpected frame %s", frame.hex())

    def _is_ack_for(self, cmd: int) -> Callable[[bytes], bool]:
        sb = start_byte(self.flavour)
        return lambda f: len(f) >= 3 and f[0] == sb and f[1] == Cmd.SCALE_ACK and f[2] == cmd

    def _is_cmd(self, cmd: int) -> Callable[[bytes], bool]:
        sb = start_byte(self.flavour)
        return lambda f: len(f) >= 4 and f[0] == sb and f[1] == cmd

    # -- protocol steps ---------------------------------------------------

    async def start(self) -> int | None:
        """Subscribe and say hello, auto-detecting the start-byte flavour.

        Returns the extra byte of the scale's INIT answer, if any.
        """
        await self._client.start_notify(CHAR_UUID, self._on_notify)
        candidates = [self.flavour] + [
            f for f in (FLAVOUR_BEURER, FLAVOUR_SANITAS) if f != self.flavour
        ]
        for flavour in candidates:
            await self._write(build_init(flavour))
            init_ack = start_byte(flavour, NIBBLE_INIT)
            try:
                frame = await self._expect(lambda f, a=init_ack: f[0] == a, 3.0)
            except ScaleSessionError:
                continue
            self.flavour = flavour
            return frame[2] if len(frame) > 2 else None
        raise ScaleSessionError("scale did not answer the init frame")

    async def set_time(self, when: datetime | None = None) -> None:
        """Set the scale clock (the scale sends no answer)."""
        await self._write(build_set_time(self.flavour, when or datetime.now(UTC)))

    async def status(self, uid: int = 0) -> ScaleStatus:
        """Read battery, unit and firmware version."""
        await self._write(build_command(self.flavour, Cmd.SCALE_STATUS, encode_uid(uid)))
        return parse_status(await self._expect(self._is_ack_for(Cmd.SCALE_STATUS)))

    async def users(self) -> list[ScaleUser]:
        """Read the user profiles stored on the scale."""
        await self._write(build_command(self.flavour, Cmd.USER_LIST))
        ack = await self._expect(self._is_ack_for(Cmd.USER_LIST))
        count = ack[4] if len(ack) > 4 else 0
        users: dict[int, ScaleUser] = {}
        while len(users) < count:
            frame = await self._expect(self._is_cmd(Cmd.USER_INFO))
            await self._ack(frame)
            total, index, user = parse_user_info(frame)
            users[index] = user
            if index >= total:
                break
        return [users[i] for i in sorted(users)]

    async def saved_measurements(self, user: ScaleUser) -> list[Measurement]:
        """Read the measurements the scale stored for one user (nothing is deleted)."""
        await self._write(
            build_command(self.flavour, Cmd.GET_SAVED_MEASUREMENTS, encode_uid(user.uid))
        )
        ack = await self._expect(self._is_ack_for(Cmd.GET_SAVED_MEASUREMENTS))
        if len(ack) < 4 or ack[3] == 0:
            return []
        collector = MultiPartCollector()
        while True:
            frame = await self._expect(self._is_cmd(Cmd.SAVED_MEASUREMENT))
            await self._ack(frame)
            if collector.add_saved(frame):
                break
        return [
            parse_measurement_record(r, user_uid=user.uid, source="saved")
            for r in collector.records
        ]

    async def unassigned_measurements(self) -> list[Measurement]:
        """Read weigh-ins the scale could not match to a user (nothing is deleted)."""
        await self._write(build_command(self.flavour, Cmd.GET_UNKNOWN_MEASUREMENTS))
        ack = await self._expect(self._is_ack_for(Cmd.GET_UNKNOWN_MEASUREMENTS))
        if len(ack) > 3 and ack[3] != 0:
            return []
        result: list[Measurement] = []
        timeout = 3.0  # an empty list may produce no frames at all
        while True:
            try:
                frame = await self._expect(
                    self._is_cmd(Cmd.UNKNOWN_MEASUREMENT_INFO), timeout
                )
            except ScaleSessionError:
                if not self._client.is_connected:
                    raise
                break
            timeout = FRAME_TIMEOUT
            await self._ack(frame)
            total, index, measurement = parse_unknown_measurement(frame)
            result.append(measurement)
            if index >= total:
                break
        return result

    async def listen(self, idle: float, maximum: float) -> None:
        """Stay connected to catch a live weigh-in.

        Returns when the scale disconnects, after ``maximum`` seconds, or after
        ``idle`` seconds without any notification (unless a weigh-in is running).
        """
        end = time.monotonic() + maximum
        while self._client.is_connected and time.monotonic() < end:
            if not self._live_parts and time.monotonic() - self.last_rx > idle:
                return
            await asyncio.sleep(0.5)

    async def stop(self) -> None:
        """Wait for pending ACK writes and unsubscribe."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._client.is_connected:
            try:
                await self._client.stop_notify(CHAR_UUID)
            except BleakError:
                pass


async def sync(
    client: BleakClient,
    *,
    name: str | None = None,
    set_time: bool = True,
    listen_idle: float = 15.0,
    listen_max: float = 90.0,
    on_live_weight: Callable[[LiveWeight], None] | None = None,
    on_live_measurement: Callable[[Measurement], None] | None = None,
) -> SyncResult:
    """Run a full read-only sync on a connected client."""
    session = ScaleSession(
        client,
        flavour=guess_flavour(name),
        on_live_weight=on_live_weight,
        on_live_measurement=on_live_measurement,
    )
    result = SyncResult(flavour=session.flavour)
    try:
        result.init_byte = await session.start()
        result.flavour = session.flavour
        if set_time:
            await session.set_time()
        result.status = await session.status()
        result.users = await session.users()
        for user in result.users:
            result.saved[user.uid] = await session.saved_measurements(user)
        result.unassigned = await session.unassigned_measurements()
        if listen_max > 0:
            await session.listen(listen_idle, listen_max)
    finally:
        result.live = list(session.live)
        result.frames = session.frames
        await session.stop()
    return result
