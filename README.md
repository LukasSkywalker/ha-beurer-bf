# Beurer BF700 for Home Assistant

A [HACS](https://hacs.xyz) custom integration that reads weigh-ins from a
**Beurer BF700** diagnostic bathroom scale over Bluetooth Low Energy. No cloud,
no Beurer app required.

It probably also works with the BF800 and the Runtastic Libra (same protocol),
but only the BF700 has been tested.

## What you get

On the scale device:

| Entity | Description |
| --- | --- |
| Last weight | Weight of the most recent weigh-in, whoever stood on the scale |
| Last weigh-in | Time of that weigh-in (as recorded by the scale) |
| Last user | Initials of the scale user it was matched to, or `unassigned` |
| Last impedance | Body impedance in Ω (disabled by default) |
| Unassigned weigh-ins | Weigh-ins the scale could not match to a user (diagnostic) |
| Last sync | When Home Assistant last read the scale (diagnostic) |

For every user profile stored on the scale (created with the Beurer
HealthManager app) a separate device with that user's latest **weight**,
**body fat**, **impedance** and **measurement time**. Body water, muscle, bone
mass, BMI, BMR and AMR are added automatically as soon as the scale reports them.
See [Known limitations](#known-limitations).

Values are stored, so they survive Home Assistant restarts even while the scale
sleeps.

## Installation

1. HACS → ⋮ → *Custom repositories* → add this repository's URL, type *Integration*.
2. Install **Beurer BF700** and restart Home Assistant.
3. Step on the scale. Home Assistant discovers it and offers to set it up
   (or add it via *Settings → Devices & services → Add integration → Beurer BF700*).

You need a Bluetooth adapter or an ESPHome Bluetooth proxy (with active
connections enabled) within range of the scale.

## How it works

The scale sleeps almost all the time. It still sends a slow advertisement
that contains the time of its last weigh-in. When that timestamp changes,
Home Assistant connects and reads:

- the scale status and the user profiles,
- the stored measurements of every user,
- the weigh-ins the scale could not match to a user,

and then stays connected for a few seconds to catch a weigh-in in progress.
Connecting to the idle scale typically takes 10 to 30 seconds, because it
only rarely accepts a connection. If it fails, the integration retries with a
back-off. The scale's radio is weak, so the adapter or proxy should be within
a few metres of it. Nothing is lost in the meantime, because
the scale keeps its stored measurements.

The integration is **read-only**. It sets the scale's clock on each connection
but never deletes, assigns or changes measurements or users, so it can be
used alongside the Beurer app.

## Known limitations

- **Unassigned weigh-ins.** The scale matches a weigh-in to a user by comparing
  the weight with that user's previous weigh-in. If it cannot, the weigh-in is
  stored as *unassigned*, without body composition (the display then shows
  only the weight), and only shows up in *Last weight*. Assigning those in the Beurer app teaches the scale your
  weight again.
- **Partial body composition on some firmwares.** The BF700 this was developed
  with (status version 6) sends the first half of each stored record twice.
  Only weight, impedance and body fat get through; water, muscle, bone mass,
  BMI, BMR and AMR are missing. The matching entities are only created on
  scales that send them.
- **Bone mass scaling** follows openScale (raw × 50 g) and has not been
  verified against the scale's display.
- **Timestamps** come from the scale's clock. The integration sets that clock
  on every connection, but measurements taken before the first connection may
  have odd dates. New weigh-ins are detected by comparing the stored records,
  not by their timestamps.
- Only one Bluetooth connection is possible at a time: while Home Assistant is
  connected, the Beurer app cannot connect, and the other way round.

## Troubleshooting

Enable debug logging:

```yaml
logger:
  logs:
    custom_components.beurer_bf700: debug
```

The integration's diagnostics download includes the raw frames of the last
sync. Please attach it to bug reports, especially for other models or
firmware versions.

`scripts/bf700_cli.py` runs the same sync from a terminal (needs `bleak` and
`bleak-retry-connector`):

```sh
python scripts/bf700_cli.py [ADDRESS] [--listen 60] [--frames]
```

## Development

```sh
uv venv && uv pip install -r requirements_test.txt
.venv/bin/python -m pytest
```

## Credits

The protocol is documented in the BeurerSanitas driver of
[openScale](https://github.com/oliexdev/openScale) (GPLv3). This integration
is an independent Python implementation, checked against frames captured
from a real BF700.

## License

MIT, see [LICENSE](LICENSE).
