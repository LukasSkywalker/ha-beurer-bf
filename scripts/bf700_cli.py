"""Talk to a Beurer BF700 from the command line using the integration's own client.

Usage: python scripts/bf700_cli.py [ADDRESS] [--frames] [--listen SECONDS]

Read-only: it syncs the scale clock but never deletes or assigns measurements.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
import sys

from bleak import BleakScanner
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.beurer_bf700.client import LiveWeight, sync  # noqa: E402
from custom_components.beurer_bf700.protocol import Measurement  # noqa: E402


def fmt(m: Measurement) -> str:
    parts = [f"{m.timestamp.astimezone():%Y-%m-%d %H:%M:%S}", f"{m.weight:6.2f} kg"]
    if m.impedance:
        parts.append(f"{m.impedance} Ω")
    for label, value, unit in (
        ("fat", m.body_fat, "%"),
        ("water", m.water, "%"),
        ("muscle", m.muscle, "%"),
        ("bone", m.bone, "kg"),
        ("BMR", m.bmr, "kcal"),
        ("AMR", m.amr, "kcal"),
        ("BMI", m.bmi, ""),
    ):
        if value is not None:
            parts.append(f"{label} {value}{unit}")
    return "  ".join(parts)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("address", nargs="?")
    parser.add_argument("--frames", action="store_true", help="print raw frames")
    parser.add_argument("--listen", type=float, default=0, help="stay connected for a live weigh-in")
    parser.add_argument("--no-time", action="store_true", help="do not set the scale clock")
    parser.add_argument("-v", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.v else logging.INFO)

    found: asyncio.Future = asyncio.get_running_loop().create_future()

    def detected(dev, adv) -> None:
        name = (adv.local_name or "").lower()
        hit = dev.address == args.address if args.address else ("bf700" in name or "bf800" in name)
        if hit and not found.done():
            found.set_result(dev)

    # Keep scanning while connecting: BlueZ forgets sparsely advertising devices otherwise.
    async with BleakScanner(detected):
        try:
            device = await asyncio.wait_for(found, 180)
        except TimeoutError:
            sys.exit("scale not found (step on it to wake it up)")
        print(f"found {device.name} {device.address}")
        client = await establish_connection(
            BleakClientWithServiceCache, device, device.name or device.address
        )
    try:
        result = await sync(
            client,
            name=device.name,
            set_time=not args.no_time,
            listen_idle=args.listen,
            listen_max=args.listen,
            on_live_weight=lambda w: print(f"  live {w.weight:.2f} kg{' (stable)' if w.stable else ''}"),
            on_live_measurement=lambda m: print(f"  LIVE  {fmt(m)}  user={m.user_uid}"),
        )
    finally:
        await client.disconnect()

    print(f"flavour {result.flavour:#x}, init byte {result.init_byte}, status {result.status}")
    names = {u.uid: u.initials for u in result.users}
    for user in result.users:
        saved = result.saved.get(user.uid, [])
        print(f"user {user.initials} ({user.uid:#x}, {user.birth_year}): {len(saved)} saved")
        for m in sorted(saved, key=lambda m: m.timestamp)[-3:]:
            print(f"   {fmt(m)}")
    print(f"unassigned: {len(result.unassigned)}")
    for m in sorted(result.unassigned, key=lambda m: m.timestamp)[-5:]:
        print(f"   #{m.unknown_index:<3} {fmt(m)}")
    for m in result.live:
        print(f"live: {fmt(m)} user={names.get(m.user_uid, m.user_uid)}")
    if args.frames:
        print("\n".join(result.frames))


asyncio.run(main())
