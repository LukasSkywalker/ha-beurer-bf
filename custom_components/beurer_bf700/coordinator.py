"""Coordinator: decides when to connect to the scale and keeps the latest data."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import client as scale_client
from .const import (
    CONNECT_ATTEMPTS,
    DOMAIN,
    ECHO_WINDOW,
    FULL_SYNC_INTERVAL,
    LISTEN_IDLE_SECONDS,
    LISTEN_MAX_SECONDS,
    QUIET_AFTER_EMPTY_SYNCS,
    QUIET_SYNC_INTERVAL,
    RETRY_MAX,
    RETRY_MIN,
    STORAGE_VERSION,
)
from .protocol import Measurement, ScaleStatus, ScaleUser, activity_stamp

_LOGGER = logging.getLogger(__name__)

type BF700ConfigEntry = ConfigEntry[BF700Coordinator]


def fingerprint(m: Measurement) -> str:
    """Identify a stored record across syncs (the scale has no record ids)."""
    owner = "u" if m.user_uid is None else f"{m.user_uid:x}"
    return f"{owner}:{int(m.timestamp.timestamp())}:{m.weight}:{m.impedance or 0}"


def _measurement_to_dict(m: Measurement) -> dict[str, Any]:
    data = asdict(m)
    data["timestamp"] = m.timestamp.isoformat()
    return data


def _measurement_from_dict(data: dict[str, Any]) -> Measurement:
    return Measurement(**{**data, "timestamp": datetime.fromisoformat(data["timestamp"])})


@dataclass
class ScaleData:
    """State exposed to the entities."""

    users: dict[int, ScaleUser] = field(default_factory=dict)
    latest: Measurement | None = None
    latest_by_user: dict[int, Measurement] = field(default_factory=dict)
    unassigned_count: int | None = None
    status: ScaleStatus | None = None
    last_sync: datetime | None = None


class BF700Coordinator(DataUpdateCoordinator[ScaleData]):
    """Connects to the scale when its advertisement shows new activity."""

    config_entry: BF700ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: BF700ConfigEntry, address: str) -> None:
        """Initialize."""
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {address}")
        self.address = address
        self.data = ScaleData()
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{address.replace(':', '').lower()}"
        )
        self._known: set[str] = set()
        self._stamp: int | None = None
        self._session: asyncio.Task[None] | None = None
        self._session_window: tuple[datetime, datetime] | None = None
        self._echo_absorbed = True
        self._pending_reason: str | None = "startup"
        self._failures = 0
        self._next_attempt = datetime.min.replace(tzinfo=UTC)
        self._empty_syncs = 0
        self._last_stamp_sync: datetime | None = None
        self.last_frames: list[str] = []
        self.last_error: str | None = None
        self.firmware_version: int | None = None
        self.device_id: str | None = None

    # -- persistence --------------------------------------------------------

    async def async_load(self) -> None:
        """Restore the last known state so entities have values before the first sync."""
        stored = await self._store.async_load()
        if not stored:
            return
        users = {u["uid"]: ScaleUser(**u) for u in stored.get("users", [])}
        latest = stored.get("latest")
        self.data = ScaleData(
            users=users,
            latest=_measurement_from_dict(latest) if latest else None,
            latest_by_user={
                int(uid): _measurement_from_dict(m)
                for uid, m in stored.get("latest_by_user", {}).items()
            },
            unassigned_count=stored.get("unassigned_count"),
            status=ScaleStatus(**stored["status"]) if stored.get("status") else None,
            last_sync=(
                datetime.fromisoformat(stored["last_sync"]) if stored.get("last_sync") else None
            ),
        )
        self._known = set(stored.get("known", []))
        self._stamp = stored.get("stamp")
        self.firmware_version = self.data.status.version if self.data.status else None

    def _save(self) -> None:
        """Persist right away: writes are rare (after a sync or stamp change)."""
        data = self.data
        snapshot = {
            "users": [asdict(u) for u in data.users.values()],
            "latest": _measurement_to_dict(data.latest) if data.latest else None,
            "latest_by_user": {
                str(uid): _measurement_to_dict(m) for uid, m in data.latest_by_user.items()
            },
            "unassigned_count": data.unassigned_count,
            "status": asdict(data.status) if data.status else None,
            "last_sync": data.last_sync.isoformat() if data.last_sync else None,
            "known": sorted(self._known),
            "stamp": self._stamp,
        }
        self.hass.async_create_task(self._store.async_save(snapshot), eager_start=True)

    # -- advertisement handling ---------------------------------------------

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Start listening for advertisements. Returns the stop callback."""
        cancel = bluetooth.async_register_callback(
            self.hass,
            self._async_on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self.address),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )

        @callback
        def _stop() -> None:
            cancel()
            if self._session and not self._session.done():
                self._session.cancel()

        return _stop

    async def _async_update_data(self) -> ScaleData:
        """Entities never poll; data arrives through advertisements and sessions."""
        return self.data

    @callback
    def _async_on_advertisement(
        self, service_info: bluetooth.BluetoothServiceInfoBleak, change: bluetooth.BluetoothChange
    ) -> None:
        now = dt_util.utcnow()
        stamp = activity_stamp(service_info.service_data)
        if stamp is not None and stamp != self._stamp:
            self._on_new_stamp(stamp, now)
        if (
            self._pending_reason is None
            and self.data.last_sync is not None
            and now - self.data.last_sync > FULL_SYNC_INTERVAL
        ):
            self._pending_reason = "periodic"
        if self._pending_reason is None or now < self._next_attempt:
            return
        if self._session is not None and not self._session.done():
            return
        if not service_info.connectable and not bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        ):
            return
        reason, self._pending_reason = self._pending_reason, None
        self._session = self.config_entry.async_create_background_task(
            self.hass, self._async_session(reason), f"{DOMAIN} sync {self.address}"
        )

    def _on_new_stamp(self, stamp: int, now: datetime) -> None:
        previous, self._stamp = self._stamp, stamp
        self._save()
        if previous is None and self._pending_reason == "startup":
            return  # first advertisement after start; a sync is pending anyway
        stamp_time = datetime.fromtimestamp(stamp, tz=UTC)
        if not self._echo_absorbed and self._session_window is not None:
            start, end = self._session_window
            if start - timedelta(minutes=1) <= stamp_time <= end + ECHO_WINDOW:
                self._echo_absorbed = True
                _LOGGER.debug("%s: stamp %x is the echo of our own session", self.address, stamp)
                return
        if (
            self._empty_syncs >= QUIET_AFTER_EMPTY_SYNCS
            and self._last_stamp_sync is not None
            and now - self._last_stamp_sync < QUIET_SYNC_INTERVAL
        ):
            _LOGGER.debug("%s: ignoring stamp %x (quiet period)", self.address, stamp)
            return
        _LOGGER.debug("%s: new activity stamp %x", self.address, stamp)
        self._pending_reason = "activity"
        self._next_attempt = now  # new activity: retry right away even after failures

    # -- session ---------------------------------------------------------------

    async def _async_session(self, reason: str) -> None:
        start = dt_util.utcnow()
        if reason == "activity":
            self._last_stamp_sync = start
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        try:
            if ble_device is None:
                raise BleakError("no connectable path to the scale")
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                ble_device.name or self.address,
                max_attempts=CONNECT_ATTEMPTS,
                ble_device_callback=lambda: (
                    bluetooth.async_ble_device_from_address(
                        self.hass, self.address, connectable=True
                    )
                    or ble_device
                ),
            )
            try:
                result = await scale_client.sync(
                    client,
                    name=ble_device.name,
                    listen_idle=LISTEN_IDLE_SECONDS,
                    listen_max=LISTEN_MAX_SECONDS,
                    on_live_measurement=self._on_live_measurement,
                )
            finally:
                await client.disconnect()
        except (BleakError, TimeoutError, scale_client.ScaleSessionError) as err:
            self._failures += 1
            delay = min(RETRY_MIN * 2 ** (self._failures - 1), RETRY_MAX)
            self._next_attempt = dt_util.utcnow() + delay
            self._pending_reason = self._pending_reason or reason
            self.last_error = f"{type(err).__name__}: {err}"
            _LOGGER.debug(
                "%s: sync (%s) failed, retrying in %s: %s", self.address, reason, delay, err
            )
            return
        self._failures = 0
        self.last_error = None
        self._session_window = (start, dt_util.utcnow())
        self._echo_absorbed = False
        self._apply(result)

    @callback
    def _on_live_measurement(self, measurement: Measurement) -> None:
        """Publish a live weigh-in immediately; the full sync result follows later."""
        self._publish_new([measurement], self.data.users)

    def _apply(self, result: scale_client.SyncResult) -> None:
        self.last_frames = result.frames
        if result.status and result.status.version != self.firmware_version:
            self.firmware_version = result.status.version
            if self.device_id:
                dr.async_get(self.hass).async_update_device(
                    self.device_id, sw_version=str(self.firmware_version)
                )
        users = {u.uid: u for u in result.users}
        records = [m for ms in result.saved.values() for m in ms] + result.unassigned
        current = {fingerprint(m): m for m in records}
        # Everything is new on the first sync. Live weigh-ins were published on
        # arrival; their stored copies come with the next sync and are known then.
        new = [m for fp, m in current.items() if fp not in self._known]
        self._known = set(current) | {fingerprint(m) for m in result.live}
        self._empty_syncs = 0 if (new or result.live) else self._empty_syncs + 1
        _LOGGER.debug(
            "%s: sync read %d users, %d records (%d new), %d live",
            self.address, len(users), len(records), len(new), len(result.live),
        )
        self.data = replace(
            self.data,
            users=users,
            unassigned_count=len(result.unassigned),
            status=result.status,
            last_sync=dt_util.utcnow(),
        )
        self._publish_new(new, users)
        # Records were read before the listening phase, so live weigh-ins are newer.
        if result.live:
            self._publish_new(result.live, users)

    def _publish_new(self, new: list[Measurement], users: dict[int, ScaleUser]) -> None:
        latest = self.data.latest
        latest_by_user = dict(self.data.latest_by_user)
        if new:
            newest = max(new, key=lambda m: m.timestamp)
            latest = newest
            for uid in {m.user_uid for m in new if m.user_uid is not None}:
                mine = max((m for m in new if m.user_uid == uid), key=lambda m: m.timestamp)
                latest_by_user[uid] = mine
        # Forget users that were deleted from the scale.
        latest_by_user = {uid: m for uid, m in latest_by_user.items() if uid in users}
        self.data = replace(self.data, latest=latest, latest_by_user=latest_by_user)
        self._save()
        self.async_set_updated_data(self.data)

