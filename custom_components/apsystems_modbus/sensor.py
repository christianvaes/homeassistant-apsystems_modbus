"""Sensors: totals on the ECU device, measurements per inverter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    MAX_LENGTH_STATE_STATE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import CONF_INVERTERS
from .coordinator import ApsystemsConfigEntry, EcuData
from .ecu import MAX_CHANNELS, Inverter, OperatingState, channel_count
from .entity import EcuEntity, InverterEntity
from .lifetime import EnergyLedger

# Entities only read coordinator data; no per-entity updates to throttle.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EcuSensorDescription(SensorEntityDescription):
    """A total computed over all inverters."""

    value_fn: Callable[[EcuData], StateType]


@dataclass(frozen=True, kw_only=True)
class LedgerSensorDescription(SensorEntityDescription):
    """A total kept by the integration itself, available from storage."""

    value_fn: Callable[[EnergyLedger], float | None]


@dataclass(frozen=True, kw_only=True)
class InverterSensorDescription(SensorEntityDescription):
    """A value read from one inverter."""

    value_fn: Callable[[Inverter], StateType]
    channel: int = 0
    """DC channel this sensor belongs to, 0 for none."""


ECU_SENSORS: tuple[EcuSensorDescription, ...] = (
    EcuSensorDescription(
        key="power",
        translation_key="power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value_fn=lambda data: data.power,
    ),
    EcuSensorDescription(
        key="energy_today",
        translation_key="energy_today",
        device_class=SensorDeviceClass.ENERGY,
        # No state class: the sum of several counters that restart at different
        # moments has no meaningful statistics. Lifetime energy is the one for
        # the Energy dashboard.
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # Shown like EMA (9.81 kWh); the state keeps Wh resolution.
        suggested_display_precision=2,
        value_fn=lambda data: _kwh(data.energy_today),
    ),
    EcuSensorDescription(
        key="inverters_online",
        translation_key="inverters_online",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.online,
    ),
)

LAST_DATA_UPDATE = SensorEntityDescription(
    key="last_data_update",
    translation_key="last_data_update",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
)


def _kwh(watt_hours: float | None) -> float | None:
    """Convert the ECU's Wh counters to kWh, the unit of the Energy dashboard."""
    return None if watt_hours is None else watt_hours / 1000


def _dc_reader(quantity: str, channel: int) -> Callable[[Inverter], float | None]:
    """Return a reader for one DC quantity of one channel."""

    def read(inverter: Inverter) -> float | None:
        value: float | None = getattr(inverter.measurements, quantity)(channel)
        return value

    return read


ACRONYMS = frozenset({"AC", "DC", "HW"})


def _plain(name: str) -> str:
    """Turn an event name such as AC_OVER_VOLTAGE into "AC over voltage"."""
    text = " ".join(
        word if word in ACRONYMS else word.lower() for word in name.split("_")
    )
    return text[:1].upper() + text[1:]


def _event_state(inverter: Inverter) -> str | None:
    """Name the active events in plain words, for example "Under frequency"."""
    events = inverter.status.events
    if events is None:
        return None
    if not events:
        return "OK"
    text = ", ".join(
        _plain(flag.name) for flag in type(events) if flag in events and flag.name
    )
    # Home Assistant refuses a state longer than this.
    return text[:MAX_LENGTH_STATE_STATE]


INVERTER_SENSORS: tuple[InverterSensorDescription, ...] = (
    InverterSensorDescription(
        key="power",
        translation_key="power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value_fn=lambda inverter: inverter.measurements.ac_power,
    ),
    InverterSensorDescription(
        key="ac_voltage",
        translation_key="ac_voltage",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
        value_fn=lambda inverter: inverter.measurements.ac_voltage,
    ),
    InverterSensorDescription(
        key="ac_current",
        translation_key="ac_current",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
        value_fn=lambda inverter: inverter.measurements.ac_current,
    ),
    InverterSensorDescription(
        key="frequency",
        translation_key="frequency",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        suggested_display_precision=2,
        value_fn=lambda inverter: inverter.measurements.frequency,
    ),
    InverterSensorDescription(
        key="temperature",
        translation_key="temperature",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda inverter: inverter.measurements.temperature,
    ),
    InverterSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=[state.name.lower() for state in OperatingState],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda inverter: (
            state.name.lower()
            if (state := inverter.status.operating_state) is not None
            else None
        ),
    ),
    InverterSensorDescription(
        key="event_code",
        translation_key="event_code",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_event_state,
    ),
    *(
        InverterSensorDescription(
            key=f"dc_voltage_{channel}",
            translation_key="dc_voltage",
            entity_category=EntityCategory.DIAGNOSTIC,
            translation_placeholders={"channel": str(channel)},
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            suggested_display_precision=1,
            channel=channel,
            value_fn=_dc_reader("dc_voltage", channel),
        )
        for channel in range(1, MAX_CHANNELS + 1)
    ),
    *(
        InverterSensorDescription(
            key=f"dc_current_{channel}",
            translation_key="dc_current",
            entity_category=EntityCategory.DIAGNOSTIC,
            translation_placeholders={"channel": str(channel)},
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            suggested_display_precision=2,
            channel=channel,
            value_fn=_dc_reader("dc_current", channel),
        )
        for channel in range(1, MAX_CHANNELS + 1)
    ),
    *(
        InverterSensorDescription(
            key=f"dc_power_{channel}",
            translation_key="dc_power",
            entity_category=EntityCategory.DIAGNOSTIC,
            translation_placeholders={"channel": str(channel)},
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfPower.WATT,
            suggested_display_precision=0,
            channel=channel,
            value_fn=_dc_reader("dc_power", channel),
        )
        for channel in range(1, MAX_CHANNELS + 1)
    ),
)


LEDGER_SENSORS: tuple[LedgerSensorDescription, ...] = (
    LedgerSensorDescription(
        key="lifetime_energy",
        translation_key="lifetime_energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=lambda ledger: _kwh(ledger.lifetime),
    ),
    LedgerSensorDescription(
        key="peak_power",
        translation_key="peak_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value_fn=lambda ledger: ledger.peak_power,
    ),
)

# No state class: today's energy follows the ECU, including its nightly reset
# and any odd reading, so it must not feed long-term statistics. Lifetime
# energy is the guarded counter for that.
INVERTER_ENERGY_TODAY = SensorEntityDescription(
    key="energy_today",
    translation_key="energy_today",
    device_class=SensorDeviceClass.ENERGY,
    native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    # Shown like EMA (9.81 kWh); the state keeps Wh resolution.
    suggested_display_precision=2,
)

INVERTER_LIFETIME = SensorEntityDescription(
    key="lifetime_energy",
    translation_key="lifetime_energy",
    device_class=SensorDeviceClass.ENERGY,
    state_class=SensorStateClass.TOTAL_INCREASING,
    native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    suggested_display_precision=1,
    entity_registry_enabled_default=False,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ApsystemsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the ECU totals and one set of sensors per inverter."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        EcuSensor(coordinator, description) for description in ECU_SENSORS
    ]
    entities.append(LastDataUpdateSensor(coordinator, LAST_DATA_UPDATE))
    entities.extend(
        LedgerSensor(coordinator, description) for description in LEDGER_SENSORS
    )
    for item in entry.data[CONF_INVERTERS]:
        channels = channel_count(item["model"])
        entities.extend(
            InverterSensor(coordinator, item, description)
            for description in INVERTER_SENSORS
            # Only the panels the model has.
            if description.channel <= channels
        )
        entities.append(
            InverterEnergyTodaySensor(coordinator, item, INVERTER_ENERGY_TODAY)
        )
        entities.append(InverterLifetimeSensor(coordinator, item, INVERTER_LIFETIME))
    async_add_entities(entities)


FLOAT_DIGITS = 3
"""Float32 registers carry about 7 significant digits; more decimals are noise."""


def _rounded[T](value: T) -> T | float:
    """Round floats so the state holds no float32 noise."""
    if isinstance(value, float):
        return round(value, FLOAT_DIGITS)
    return value


class EcuSensor(EcuEntity, SensorEntity):
    """A total over all inverters."""

    entity_description: EcuSensorDescription

    @property
    def native_value(self) -> StateType:
        """Return the current total."""
        return _rounded(self.entity_description.value_fn(self.coordinator.data))


class LastDataUpdateSensor(EcuEntity, RestoreSensor):
    """When the ECU last reported new values, also while silent and after restarts."""

    async def async_added_to_hass(self) -> None:
        """Take over the time from before a restart."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and isinstance(last.native_value, datetime):
            self.coordinator.restore_last_change(last.native_value)

    @property
    def available(self) -> bool:
        """Available once a time is known."""
        return self.coordinator.last_change is not None

    @property
    def native_value(self) -> datetime | None:
        """Return when the ECU last reported new values."""
        return self.coordinator.last_change


class InverterSensor(InverterEntity, SensorEntity):
    """A value of one inverter."""

    entity_description: InverterSensorDescription

    @property
    def native_value(self) -> StateType:
        """Return the last read value."""
        return _rounded(self.entity_description.value_fn(self.inverter))


class LedgerSensor(EcuEntity, SensorEntity):
    """Lifetime energy or peak power of the ECU.

    Comes from storage, so it is available right after a restart and stays
    available while the ECU is unreachable; the Energy dashboard sees no gap.
    """

    entity_description: LedgerSensorDescription

    @property
    def available(self) -> bool:
        """Available as soon as anything was counted or set."""
        return self.coordinator.ledger.has_data

    @property
    def native_value(self) -> StateType:
        """Return the stored total."""
        return _rounded(self.entity_description.value_fn(self.coordinator.ledger))


class InverterEnergyTodaySensor(InverterEntity, SensorEntity):
    """Daily counter of one inverter as the ECU reports it."""

    @property
    def native_value(self) -> StateType:
        """Return today's energy of this inverter."""
        return _rounded(_kwh(self.coordinator.daily_today(self.unit_id)))


class InverterLifetimeSensor(InverterEntity, SensorEntity):
    """Lifetime energy of one inverter, without the start value of the ECU."""

    @property
    def available(self) -> bool:
        """Available once this inverter has been counted."""
        return self.coordinator.ledger.lifetime_of(self.serial) is not None

    @property
    def native_value(self) -> StateType:
        """Return the counted energy of this inverter."""
        return _rounded(_kwh(self.coordinator.ledger.lifetime_of(self.serial)))
