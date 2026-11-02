"""Coordinator and sensor tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
import pytest

from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.beurer_bf700.client import SyncResult
from custom_components.beurer_bf700.const import DOMAIN
from custom_components.beurer_bf700.coordinator import BF700Coordinator
from custom_components.beurer_bf700.protocol import ScaleStatus, ScaleUser
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .common import ADDRESS, NAME, measurement, service_info

pytestmark = pytest.mark.usefixtures("mock_bluetooth")

USER_A = ScaleUser(uid=0x65, initials="ABC", birth_year=1980)
USER_B = ScaleUser(uid=0x66, initials="XY", birth_year=1990)
STATUS = ScaleStatus(
    result=0, battery=50, weight_threshold_kg=4.0, body_fat_threshold_pct=22.0, unit="kg", version=6
)

M_A = measurement(1_700_000_100, 80.0, impedance=450, body_fat=20.0, user_uid=0x65)
U_OLD = measurement(1_700_000_050, 70.0, impedance=500, source="unassigned", unknown_index=1)
U_NEW = measurement(1_700_000_200, 81.5, impedance=455, source="unassigned", unknown_index=2)


def result(*unassigned, saved_a=(M_A,)) -> SyncResult:
    return SyncResult(
        flavour=0xF0,
        status=STATUS,
        users=[USER_A, USER_B],
        saved={USER_A.uid: list(saved_a), USER_B.uid: []},
        unassigned=list(unassigned),
    )


def now_stamp(offset: int = 0) -> int:
    return int(dt_util.utcnow().timestamp()) + offset


@pytest.fixture
def mock_sync():
    """Patch the BLE layer; returns the mocked sync()."""
    client = MagicMock(disconnect=AsyncMock())
    with (
        patch(
            "custom_components.beurer_bf700.coordinator.bluetooth.async_ble_device_from_address",
            return_value=BLEDevice(ADDRESS, NAME, None),
        ),
        patch(
            "custom_components.beurer_bf700.coordinator.establish_connection",
            AsyncMock(return_value=client),
        ),
        patch(
            "custom_components.beurer_bf700.coordinator.scale_client.sync", AsyncMock()
        ) as sync,
    ):
        yield sync


async def setup(hass: HomeAssistant) -> tuple[MockConfigEntry, BF700Coordinator]:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, title=NAME)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, entry.runtime_data


async def advertise(hass: HomeAssistant, coordinator: BF700Coordinator, stamp: int) -> None:
    coordinator._async_on_advertisement(service_info(stamp), BluetoothChange.ADVERTISEMENT)
    await hass.async_block_till_done()


def state(hass: HomeAssistant, entity_id: str) -> str | None:
    s = hass.states.get(entity_id)
    return None if s is None else s.state


async def test_sync_flow(hass: HomeAssistant, mock_sync: AsyncMock) -> None:
    """First advertisement syncs, our echo is ignored, real activity syncs again."""
    _entry, coordinator = await setup(hass)
    assert state(hass, "sensor.beurer_bf700_last_weight") == STATE_UNKNOWN

    mock_sync.return_value = result(U_OLD)
    await advertise(hass, coordinator, now_stamp(-3600))
    assert mock_sync.await_count == 1

    # Newest record overall is user A's saved measurement.
    assert state(hass, "sensor.beurer_bf700_last_weight") == "80.0"
    assert state(hass, "sensor.beurer_bf700_last_user") == "ABC"
    assert state(hass, "sensor.beurer_bf700_unassigned_weigh_ins") == "1"
    assert state(hass, "sensor.beurer_bf700_abc_weight") == "80.0"
    assert state(hass, "sensor.beurer_bf700_abc_body_fat") == "20.0"
    assert state(hass, "sensor.beurer_bf700_xy_weight") == STATE_UNKNOWN
    # Values this firmware never sends get no entity.
    assert hass.states.get("sensor.beurer_bf700_abc_body_water") is None

    # The scale updates its stamp after our own session: no new connection.
    await advertise(hass, coordinator, now_stamp(30))
    assert mock_sync.await_count == 1

    # Someone weighs in later: the stamp changes again and we sync.
    mock_sync.return_value = result(U_OLD, U_NEW)
    await advertise(hass, coordinator, now_stamp(3600))
    assert mock_sync.await_count == 2
    assert state(hass, "sensor.beurer_bf700_last_weight") == "81.5"
    assert state(hass, "sensor.beurer_bf700_last_user") == "unassigned"
    assert state(hass, "sensor.beurer_bf700_unassigned_weigh_ins") == "2"
    assert state(hass, "sensor.beurer_bf700_abc_weight") == "80.0"

    # The same stamp again does nothing.
    await advertise(hass, coordinator, now_stamp(3600))
    assert mock_sync.await_count == 2


async def test_old_records_are_not_new(hass: HomeAssistant, mock_sync: AsyncMock) -> None:
    """A record with an older timestamp that we have seen before does not win."""
    _entry, coordinator = await setup(hass)
    mock_sync.return_value = result(U_NEW)
    await advertise(hass, coordinator, now_stamp(-3600))
    assert state(hass, "sensor.beurer_bf700_last_weight") == "81.5"

    # The scale clock is unreliable: a new weigh-in can carry an older timestamp.
    late = measurement(1_600_000_000, 79.0, source="unassigned", unknown_index=3)
    mock_sync.return_value = result(U_NEW, late)
    await advertise(hass, coordinator, now_stamp(3600))
    assert state(hass, "sensor.beurer_bf700_last_weight") == "79.0"


async def test_live_measurement(hass: HomeAssistant, mock_sync: AsyncMock) -> None:
    """A weigh-in while connected is published at once, with full body composition."""
    _entry, coordinator = await setup(hass)
    live = measurement(
        1_700_000_300, 79.5, impedance=448, body_fat=19.5, water=58.0, muscle=42.0,
        bone=3.1, bmr=1800, amr=2900, bmi=23.9, user_uid=0x65, source="live",
    )

    async def fake_sync(client, **kwargs):
        kwargs["on_live_measurement"](live)
        r = result()
        r.live = [live]
        return r

    mock_sync.side_effect = fake_sync
    await advertise(hass, coordinator, now_stamp(-3600))
    assert state(hass, "sensor.beurer_bf700_abc_weight") == "79.5"
    assert state(hass, "sensor.beurer_bf700_abc_body_water") == "58.0"
    assert state(hass, "sensor.beurer_bf700_abc_bmi") == "23.9"

    # Its stored copy (without composition, as on BF700 v6) shows up in the next sync.
    stored = measurement(1_700_000_300, 79.5, impedance=448, body_fat=19.5, user_uid=0x65)
    mock_sync.side_effect = None
    mock_sync.return_value = result(saved_a=(M_A, stored))
    await advertise(hass, coordinator, now_stamp(3600))
    assert state(hass, "sensor.beurer_bf700_abc_body_water") == "58.0"


async def test_failure_backoff(hass: HomeAssistant, mock_sync: AsyncMock) -> None:
    """A failed connection is retried after a back-off, and new activity retries at once."""
    _entry, coordinator = await setup(hass)
    mock_sync.side_effect = BleakError("asleep")
    await advertise(hass, coordinator, now_stamp(-3600))
    assert mock_sync.await_count == 1
    assert coordinator.last_error == "BleakError: asleep"

    await advertise(hass, coordinator, now_stamp(-3600))
    assert mock_sync.await_count == 1  # backing off

    mock_sync.side_effect = None
    mock_sync.return_value = result()
    await advertise(hass, coordinator, now_stamp(3600))
    assert mock_sync.await_count == 2
    assert coordinator.last_error is None


async def test_state_survives_restart(
    hass: HomeAssistant, mock_sync: AsyncMock, entity_registry: er.EntityRegistry
) -> None:
    """Values come back from storage before the scale is reachable again."""
    entry, coordinator = await setup(hass)
    mock_sync.return_value = result(U_OLD)
    await advertise(hass, coordinator, now_stamp(-3600))

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert state(hass, "sensor.beurer_bf700_last_weight") == "80.0"
    assert state(hass, "sensor.beurer_bf700_abc_body_fat") == "20.0"
    assert entity_registry.async_get("sensor.beurer_bf700_abc_impedance").disabled_by
