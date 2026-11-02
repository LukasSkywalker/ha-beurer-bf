"""Protocol tests.

Frames marked "openScale" are real captures published in openScale's test suite.
Frames marked "BF700 v6" reproduce the layout captured on a BF700 reporting
version 6 (values altered): it sends the first record half twice.
"""

from datetime import UTC, datetime

import pytest

from custom_components.beurer_bf700.protocol import (
    FLAVOUR_BEURER,
    FLAVOUR_SANITAS,
    MultiPartCollector,
    activity_stamp,
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
)

h = bytes.fromhex


def saved(frames: list[str], uid: int = 1):
    collector = MultiPartCollector()
    for f in frames:
        collector.add_saved(h(f))
    return [parse_measurement_record(r, user_uid=uid, source="saved") for r in collector.records]


def test_frame_builders() -> None:
    assert build_init(FLAVOUR_BEURER) == h("f601")
    assert build_init(FLAVOUR_SANITAS) == h("e601")
    assert build_set_time(FLAVOUR_BEURER, datetime.fromtimestamp(0x6AE9C476, UTC)) == h(
        "f96ae9c476"
    )
    assert build_command(FLAVOUR_BEURER, 0x4F, encode_uid(0x65)) == h("f74f0000000000000065")
    assert build_app_ack(FLAVOUR_BEURER, h("f74202016a000000064001c000c802")) == h("f7f1420201")


def test_user_info_openscale() -> None:
    total, index, user = parse_user_info(h("f7340301000000000000006546415759"))
    assert (total, index) == (3, 1)
    assert user.uid == 0x65
    assert user.initials == "FAW"
    assert user.birth_year == 1989


def test_status_bf700_v6() -> None:
    status = parse_status(h("f7f04f000928dc0100000106"))
    assert status.result == 0
    assert status.battery == 9
    assert status.weight_threshold_kg == 4.0
    assert status.body_fat_threshold_pct == 22.0
    assert status.unit == "kg"
    assert status.version == 6


def test_saved_full_record_openscale() -> None:
    [m] = saved(["f74214015a20f7e4060d01cc00aa02", "f74214024701ab00ef07080d0300f7"])
    assert m.timestamp == datetime.fromtimestamp(1512110052, UTC)
    assert m.weight == 77.45
    assert m.impedance == 460
    assert m.body_fat == 17.0
    assert m.water == 58.3
    assert m.muscle == 42.7
    assert m.bmr == 1800
    assert m.amr == 3331
    assert m.bmi == 24.7


def test_saved_without_body_composition_openscale() -> None:
    [m] = saved(["f74214055a23f9ca061a0000000000", "f7421406000000000007100d1200f9"])
    assert m.weight == 78.10
    assert m.impedance is None
    assert m.body_fat is None
    assert m.water is None
    assert m.bone is None


def test_saved_duplicated_half_bf700_v6() -> None:
    first = "f74204016a00000006400" + "1c000c802"
    records = saved([first, "f7420402" + first[8:], "f742040369f0000005dc0000000000", "f742040469f0000005dc0000000000"])
    assert len(records) == 2
    m = records[0]
    assert m.timestamp == datetime.fromtimestamp(0x6A000000, UTC)
    assert m.weight == 80.0
    assert m.impedance == 448
    assert m.body_fat == 20.0
    # The second half is a copy of the first: nothing else can be trusted.
    assert m.water is None and m.muscle is None and m.bmi is None and m.bmr is None
    assert records[1].weight == 75.0
    assert records[1].body_fat is None


def test_live_measurement_openscale_bf800_bit31() -> None:
    m = parse_live_measurement_frames(
        [
            h("f7590301010000000000000065"),
            h("f7590302ea9290c2066f01ba00f501"),
            h("f7590303ea017c007304dd074c00f1"),
        ]
    )
    assert m.user_uid == 0x65
    assert m.timestamp == datetime.fromtimestamp(1787990210, UTC)
    assert m.weight == 82.35
    assert m.body_fat == 24.5
    assert m.water == 49.0
    assert m.muscle == 38.0
    assert m.impedance == 442
    assert m.source == "live"


def test_live_measurement_unmatched_bf700_v6() -> None:
    m = parse_live_measurement_frames(
        [
            h("f7590301000000000000000000"),
            h("f75903026a00010006400000000000"),
            h("f75903036a00010006400000000000"),
        ]
    )
    assert m.user_uid is None
    assert m.weight == 80.0
    assert m.impedance is None
    assert m.body_fat is None


def test_live_weight() -> None:
    assert parse_live_weight(h("f758010640")) == (False, 80.0)
    assert parse_live_weight(h("f758000640")) == (True, 80.0)


def test_unknown_measurement_bf700_v6() -> None:
    total, index, m = parse_unknown_measurement(h("f7471402056a000000064001c0"))
    assert (total, index) == (20, 2)
    assert m.unknown_index == 5
    assert m.timestamp == datetime.fromtimestamp(0x6A000000, UTC)
    assert m.weight == 80.0
    assert m.impedance == 448
    assert m.user_uid is None
    assert m.source == "unassigned"


@pytest.mark.parametrize(
    ("service_data", "expected"),
    [
        ({"0000856a-0000-1000-8000-00805f9b34fb": h("5803")}, 0x6A855803),
        (
            {
                "0000856a-0000-1000-8000-00805f9b34fb": h("5803"),
                "0000e96a-0000-1000-8000-00805f9b34fb": h("c476"),
            },
            0x6AE9C476,
        ),
        ({}, None),
        ({"0000fea3-0000-1000-8000-00805f9b34fb": h("010203")}, None),
    ],
)
def test_activity_stamp(service_data: dict[str, bytes], expected: int | None) -> None:
    assert activity_stamp(service_data) == expected
