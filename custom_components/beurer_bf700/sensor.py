"""Sensors for the Beurer BF700."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfMass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import BF700ConfigEntry, BF700Coordinator, ScaleData
from .protocol import Measurement

PARALLEL_UPDATES = 0

type Value = float | int | str | datetime | None


@dataclass(frozen=True, kw_only=True)
class MeasurementSensorDescription(SensorEntityDescription):
    """Describes a value taken from a measurement."""

    value_fn: Callable[[Measurement], Value]
    # Only create the entity once the scale has delivered a value for it.
    on_demand: bool = False


@dataclass(frozen=True, kw_only=True)
class ScaleSensorDescription(SensorEntityDescription):
    """Describes a value taken from the scale state."""

    value_fn: Callable[[ScaleData], Value]


WEIGHT = MeasurementSensorDescription(
    key="weight",
    translation_key="weight",
    device_class=SensorDeviceClass.WEIGHT,
    state_class=SensorStateClass.MEASUREMENT,
    native_unit_of_measurement=UnitOfMass.KILOGRAMS,
    suggested_display_precision=2,
    value_fn=lambda m: m.weight,
)
IMPEDANCE = MeasurementSensorDescription(
    key="impedance",
    translation_key="impedance",
    state_class=SensorStateClass.MEASUREMENT,
    native_unit_of_measurement="Ω",
    entity_registry_enabled_default=False,
    value_fn=lambda m: m.impedance,
)
MEASURED_AT = MeasurementSensorDescription(
    key="measured_at",
    translation_key="measured_at",
    device_class=SensorDeviceClass.TIMESTAMP,
    value_fn=lambda m: m.timestamp,
)


def _pct(key: str, fn: Callable[[Measurement], Value], *, on_demand: bool = True):
    return MeasurementSensorDescription(
        key=key,
        translation_key=key,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        value_fn=fn,
        on_demand=on_demand,
    )


USER_SENSORS: tuple[MeasurementSensorDescription, ...] = (
    WEIGHT,
    _pct("body_fat", lambda m: m.body_fat, on_demand=False),
    _pct("body_water", lambda m: m.water),
    _pct("muscle", lambda m: m.muscle),
    MeasurementSensorDescription(
        key="bone_mass",
        translation_key="bone_mass",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        suggested_display_precision=1,
        value_fn=lambda m: m.bone,
        on_demand=True,
    ),
    MeasurementSensorDescription(
        key="bmi",
        translation_key="bmi",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda m: m.bmi,
        on_demand=True,
    ),
    MeasurementSensorDescription(
        key="bmr",
        translation_key="bmr",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="kcal",
        value_fn=lambda m: m.bmr,
        on_demand=True,
    ),
    MeasurementSensorDescription(
        key="amr",
        translation_key="amr",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="kcal",
        value_fn=lambda m: m.amr,
        on_demand=True,
    ),
    IMPEDANCE,
    MEASURED_AT,
)

LAST_WEIGH_IN_SENSORS: tuple[MeasurementSensorDescription, ...] = (
    WEIGHT,
    IMPEDANCE,
    MEASURED_AT,
)

SCALE_SENSORS: tuple[ScaleSensorDescription, ...] = (
    ScaleSensorDescription(
        key="user",
        translation_key="user",
        value_fn=lambda d: (
            None
            if d.latest is None
            else (d.users[d.latest.user_uid].initials
                  if d.latest.user_uid in d.users else "unassigned")
        ),
    ),
    ScaleSensorDescription(
        key="unassigned_count",
        translation_key="unassigned_count",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.unassigned_count,
    ),
    ScaleSensorDescription(
        key="last_sync",
        translation_key="last_sync",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.last_sync,
    ),
)


def scale_device_info(coordinator: BF700Coordinator) -> DeviceInfo:
    """Device entry of the scale itself."""
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.address)},
        connections={(CONNECTION_BLUETOOTH, coordinator.address)},
        manufacturer=MANUFACTURER,
        model="BF700",
        name=coordinator.config_entry.title,
        sw_version=(
            str(coordinator.firmware_version) if coordinator.firmware_version is not None else None
        ),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BF700ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors, adding per-user entities as users and values appear."""
    coordinator = entry.runtime_data
    added: set[str] = set()

    entities: list[SensorEntity] = [
        LastWeighInSensor(coordinator, d) for d in LAST_WEIGH_IN_SENSORS
    ] + [ScaleSensor(coordinator, d) for d in SCALE_SENSORS]
    async_add_entities(entities)

    @callback
    def _add_user_entities() -> None:
        data = coordinator.data
        new: list[SensorEntity] = []
        for uid in data.users:
            latest = data.latest_by_user.get(uid)
            for description in USER_SENSORS:
                key = f"{uid:x}-{description.key}"
                if key in added:
                    continue
                if description.on_demand and (
                    latest is None or description.value_fn(latest) is None
                ):
                    continue
                added.add(key)
                new.append(UserSensor(coordinator, description, uid))
        if new:
            async_add_entities(new)

    _add_user_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_user_entities))


class BF700Entity(CoordinatorEntity[BF700Coordinator]):
    """Base entity. Always available: values describe the last weigh-in, not a live state."""

    _attr_has_entity_name = True

    @property
    def available(self) -> bool:
        """The scale sleeps most of the time; keep showing the last known values."""
        return True


class ScaleSensor(BF700Entity, SensorEntity):
    """Sensor on the scale device describing the scale state."""

    entity_description: ScaleSensorDescription

    def __init__(self, coordinator: BF700Coordinator, description: ScaleSensorDescription) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}-{description.key}"
        self._attr_device_info = scale_device_info(coordinator)

    @property
    def native_value(self) -> Value:
        """Return the value."""
        return self.entity_description.value_fn(self.coordinator.data)


class LastWeighInSensor(BF700Entity, SensorEntity):
    """The most recent weigh-in on the scale, whoever it was (also unassigned ones)."""

    entity_description: MeasurementSensorDescription

    def __init__(
        self, coordinator: BF700Coordinator, description: MeasurementSensorDescription
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}-last-{description.key}"
        self._attr_translation_key = f"last_{description.translation_key}"
        self._attr_device_info = scale_device_info(coordinator)

    @property
    def native_value(self) -> Value:
        """Return the value."""
        latest = self.coordinator.data.latest
        return None if latest is None else self.entity_description.value_fn(latest)


class UserSensor(BF700Entity, SensorEntity):
    """Latest value for one user profile stored on the scale."""

    entity_description: MeasurementSensorDescription

    def __init__(
        self, coordinator: BF700Coordinator, description: MeasurementSensorDescription, uid: int
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._uid = uid
        user = coordinator.data.users[uid]
        self._attr_unique_id = f"{coordinator.address}-{uid:x}-{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.address}-{uid:x}")},
            manufacturer=MANUFACTURER,
            model="BF700 user profile",
            name=f"{coordinator.config_entry.title} {user.initials}",
        )
        if "via_device_id" in DeviceInfo.__annotations__ and coordinator.device_id:
            self._attr_device_info["via_device_id"] = coordinator.device_id
        else:  # Home Assistant before via_device_id existed
            self._attr_device_info["via_device"] = (DOMAIN, coordinator.address)

    @property
    def native_value(self) -> Value:
        """Return the value."""
        latest = self.coordinator.data.latest_by_user.get(self._uid)
        return None if latest is None else self.entity_description.value_fn(latest)
