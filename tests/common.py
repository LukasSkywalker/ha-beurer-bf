"""Test helpers."""

from __future__ import annotations

from datetime import UTC, datetime
import time

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

from custom_components.beurer_bf700.protocol import Measurement

ADDRESS = "AA:BB:CC:DD:EE:01"
NAME = "Beurer BF700"
BASE_UUIDS = [
    "0000ffe0-0000-1000-8000-00805f9b34fb",
    "0000fea3-0000-1000-8000-00805f9b34fb",
    "0000180a-0000-1000-8000-00805f9b34fb",
]


def stamp_service_data(stamp: int) -> dict[str, bytes]:
    """Encode an activity stamp the way the scale advertises it."""
    raw = stamp.to_bytes(4, "big")
    short = int.from_bytes(raw[:2], "little")
    return {f"0000{short:04x}-0000-1000-8000-00805f9b34fb": raw[2:]}


def service_info(
    stamp: int | None = None, name: str = NAME, address: str = ADDRESS
) -> BluetoothServiceInfoBleak:
    """Build an advertisement of the scale."""
    service_data = stamp_service_data(stamp) if stamp is not None else {}
    uuids = BASE_UUIDS + [k for k in service_data]
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data={},
        service_data=service_data,
        service_uuids=uuids,
        source="local",
        device=BLEDevice(address, name, None),
        advertisement=AdvertisementData(
            local_name=name,
            manufacturer_data={},
            service_data=service_data,
            service_uuids=uuids,
            tx_power=None,
            rssi=-60,
            platform_data=(),
        ),
        connectable=True,
        time=time.monotonic(),
        tx_power=None,
    )


def measurement(ts: int, weight: float, **kwargs) -> Measurement:
    """Build a measurement at unix time ``ts``."""
    return Measurement(timestamp=datetime.fromtimestamp(ts, UTC), weight=weight, **kwargs)
