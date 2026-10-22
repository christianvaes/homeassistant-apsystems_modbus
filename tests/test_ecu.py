"""Tests for the register model, against the mock ECU."""

from __future__ import annotations

from modbus_connection import ModbusTimeoutError
from modbus_connection.mock import MockModbusConnection, WriteEvent
import pytest

from custom_components.apsystems_modbus.ecu import (
    GRID_CONNECTION_ADDRESS,
    Identity,
    Inverter,
    InverterEvent,
    OperatingState,
    UnsupportedLayoutError,
    channel_count,
)

from .conftest import add_inverter, set_float32


async def test_identity(mock_ecu: MockModbusConnection) -> None:
    identity = Identity(mock_ecu.for_unit(1))
    await identity.async_update()
    identity.check_layout()

    assert identity.manufacturer == "APsystems"
    assert identity.model == "DS3"
    assert identity.serial_number == "704000000001"


async def test_measurements_by_day(mock_ecu: MockModbusConnection) -> None:
    inverter = Inverter(mock_ecu.for_unit(1), 1)
    report = await inverter.async_poll(Inverter.POLLED)

    assert report.complete
    m = inverter.measurements
    assert m.ac_power == 204.0
    assert m.ac_voltage == 228.0
    assert m.ac_current == pytest.approx(1.45)
    assert m.frequency == pytest.approx(49.98)
    assert m.energy_today == pytest.approx(554.08, abs=0.01)
    assert m.temperature == 24.0
    assert m.is_online is True
    assert m.dc_voltage(1) == pytest.approx(34.01, abs=0.01)
    assert m.dc_current(2) == pytest.approx(3.381, abs=0.001)
    assert m.dc_power(1) == pytest.approx(111.8, abs=0.1)
    assert m.dc_voltage(3) == 0.0
    assert inverter.status.operating_state is OperatingState.MPPT
    assert inverter.status.events == InverterEvent(0)


async def test_controls_block_read(mock_ecu: MockModbusConnection) -> None:
    inverter = Inverter(mock_ecu.for_unit(2), 2)
    await inverter.async_poll(Inverter.POLLED)

    m = inverter.measurements
    assert m.export_limit == 100.0
    assert m.export_limit_enabled is False
    assert m.grid_connected is True


@pytest.mark.parametrize("watts", [0.0, 1.0])
async def test_offline_inverter(mock_ecu: MockModbusConnection, watts: float) -> None:
    # At night the ECU may keep 1 W of an inverter that is off the grid.
    unit = mock_ecu.for_unit(1)
    set_float32(unit, 40138, 0.0)  # voltage
    set_float32(unit, 40144, watts)  # power
    set_float32(unit, 40146, 0.0)  # frequency
    inverter = Inverter(unit, 1)
    await inverter.async_poll(("measurements",))

    assert inverter.measurements.is_online is False
    assert inverter.measurements.ac_power == watts
    # The daily counter keeps its value while the inverter is offline.
    assert inverter.measurements.energy_today == pytest.approx(554.08, abs=0.01)


async def test_stray_zero_frequency_still_online(
    mock_ecu: MockModbusConnection,
) -> None:
    unit = mock_ecu.for_unit(1)
    set_float32(unit, 40146, 0.0)
    inverter = Inverter(unit, 1)
    await inverter.async_poll(("measurements",))

    assert inverter.measurements.is_online is True


async def test_unsupported_layout(mock_ecu: MockModbusConnection) -> None:
    unit = mock_ecu.for_unit(1)
    unit.holding[40070] = 103  # a three-phase model instead of 101
    identity = Identity(unit)
    await identity.async_update()

    with pytest.raises(UnsupportedLayoutError, match="40070"):
        identity.check_layout()


@pytest.mark.parametrize("serial", ["/../../configuration.yaml", "7040.0001", ""])
async def test_serial_number_must_be_letters_and_digits(
    mock_ecu: MockModbusConnection, serial: str
) -> None:
    # The serial number names the file of the stored totals.
    add_inverter(mock_ecu, 9, serial)
    identity = Identity(mock_ecu.for_unit(9))
    await identity.async_update()

    with pytest.raises(UnsupportedLayoutError, match="serial number"):
        identity.check_layout()


async def test_timeout_propagates(mock_ecu: MockModbusConnection) -> None:
    unit = mock_ecu.for_unit(1)
    inverter = Inverter(unit, 1)
    await inverter.async_ensure_setup()
    unit.fail_requests(ModbusTimeoutError())

    with pytest.raises(ModbusTimeoutError):
        await inverter.async_poll(Inverter.POLLED)


async def test_partial_poll(mock_ecu: MockModbusConnection) -> None:
    unit = mock_ecu.for_unit(1)
    inverter = Inverter(unit, 1)
    await inverter.async_ensure_setup()
    unit.fail_read(40108, ModbusTimeoutError())

    report = await inverter.async_poll(Inverter.POLLED)

    assert report.updated == {"measurements"}
    assert set(report.failed) == {"status"}


async def test_write_export_limit(mock_ecu: MockModbusConnection) -> None:
    unit = mock_ecu.for_unit(1)
    writes: list[WriteEvent] = []
    unit.on_write(writes.append)
    inverter = Inverter(unit, 1)
    await inverter.async_poll(("measurements",))

    await inverter.async_set_export_limit(10)
    await inverter.async_set_export_limit_enabled(True)

    assert [(w.address, list(w.values)) for w in writes] == [
        (40189, [100]),
        (40193, [1]),
    ]
    # The ECU only answers "write multiple registers" (function code 16).
    assert {w.function_code for w in writes} == {0x10}
    assert all(w.address != GRID_CONNECTION_ADDRESS for w in writes)
    await inverter.async_poll(("measurements",))
    assert inverter.measurements.export_limit == 10.0
    assert inverter.measurements.export_limit_enabled is True


@pytest.mark.parametrize("percent", [-1, 101])
async def test_write_export_limit_out_of_range(
    mock_ecu: MockModbusConnection, percent: int
) -> None:
    inverter = Inverter(mock_ecu.for_unit(1), 1)
    await inverter.async_poll(("measurements",))

    with pytest.raises(ValueError, match="0-100"):
        await inverter.async_set_export_limit(percent)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("DS3", 2),
        ("DS3-L", 2),
        ("YC600", 2),
        ("QT2", 4),
        ("DS3D", 4),
        ("NEW1", 4),  # an unknown model keeps every panel
        (None, 4),
    ],
)
def test_channel_count(model: str | None, expected: int) -> None:
    assert channel_count(model) == expected
