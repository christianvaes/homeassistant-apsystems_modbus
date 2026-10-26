"""Tests for the inverter output switch: turning all inverters off and on."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import datetime, timedelta
import itertools
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
from modbus_connection import ModbusTimeoutError
from modbus_connection.mock import MockModbusConnection, WriteEvent
import pytest

from homeassistant.components import persistent_notification
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.components.switch import (
    DOMAIN as SWITCH_DOMAIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.apsystems_modbus.coordinator import EcuCoordinator
from custom_components.apsystems_modbus.ecu import (
    EXPORT_LIMIT_ADDRESS,
    GRID_CONNECTION_ADDRESS,
)

from .conftest import set_float32

ENTITY = "switch.apsystems_ecu_inverter_output"
AC_POWER = 40144
START = datetime(2026, 10, 20, 11, 1, 30, tzinfo=dt_util.UTC)
"""Just after a poll, between two ECU rounds."""


@pytest.fixture(autouse=True)
def daylight() -> Generator[MagicMock]:
    """Make it day unless a test says otherwise."""
    with patch(
        "custom_components.apsystems_modbus.inverter_output.sun.is_up",
        return_value=True,
    ) as is_up:
        yield is_up


async def setup_entry(
    hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> EcuCoordinator:
    freezer.move_to(START)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry.runtime_data


def conn_writes(mock_ecu: MockModbusConnection, unit_id: int = 1) -> list[int]:
    """Collect the values written to Conn on one unit."""
    values: list[int] = []

    def take(event: WriteEvent) -> None:
        if event.address == GRID_CONNECTION_ADDRESS:
            assert len(event.values) == 1
            values.append(event.values[0])

    mock_ecu.for_unit(unit_id).on_write(take)
    return values


def set_power(mock_ecu: MockModbusConnection, watts: dict[int, float]) -> None:
    """Make inverters report an AC power, per unit id."""
    for unit_id, value in watts.items():
        set_float32(mock_ecu.for_unit(unit_id), AC_POWER, value)


async def switch(hass: HomeAssistant, service: str) -> None:
    await hass.services.async_call(
        SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: ENTITY}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def at(hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: str) -> None:
    """Move the clock to a time today (UTC) and let the timers fire."""
    hour, minute, second = (int(part) for part in when.split(":"))
    freezer.move_to(START.replace(hour=hour, minute=minute, second=second))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_on_by_default_and_nothing_written(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    writes = conn_writes(mock_ecu)
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    await poll(hass, coordinator)  # The capture reports 204 W.
    await at(hass, freezer, "11:05:00")

    assert hass.states.get(ENTITY).state == STATE_ON
    assert writes == []


async def test_off_writes_at_once_and_before_every_round(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    writes = conn_writes(mock_ecu)

    await switch(hass, SERVICE_TURN_OFF)
    assert hass.states.get(ENTITY).state == STATE_OFF
    assert writes == [0]
    set_power(mock_ecu, {1: 0.0, 2: 0.0})

    await at(hass, freezer, "11:05:00")
    await at(hass, freezer, "11:07:30")
    await at(hass, freezer, "11:10:00")
    assert writes == [0, 0, 0]
    # Unit 2 never gets anything: one write on the lead covers the ECU.
    assert conn_writes(mock_ecu, 2) == []


async def test_on_writes_one_once_and_stops(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    writes = conn_writes(mock_ecu)

    await switch(hass, SERVICE_TURN_OFF)
    await switch(hass, SERVICE_TURN_ON)
    await at(hass, freezer, "11:05:00")
    await at(hass, freezer, "11:10:00")

    assert hass.states.get(ENTITY).state == STATE_ON
    assert writes == [0, 1]


async def test_same_choice_again_writes_nothing(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    writes = conn_writes(mock_ecu)

    await switch(hass, SERVICE_TURN_ON)
    await switch(hass, SERVICE_TURN_OFF)
    await switch(hass, SERVICE_TURN_OFF)

    assert writes == [0]


async def test_clock_write_skipped_right_after_a_write(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    writes = conn_writes(mock_ecu)

    freezer.move_to(START.replace(minute=4, second=55))
    await switch(hass, SERVICE_TURN_OFF)
    await at(hass, freezer, "11:05:00")
    assert writes == [0]

    await at(hass, freezer, "11:10:00")
    assert writes == [0, 0]


async def test_late_clock_write_is_skipped_and_caught_up(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    await switch(hass, SERVICE_TURN_OFF)
    writes = conn_writes(mock_ecu)

    # Home Assistant was busy: the write of 11:05 runs only at 11:05:07,
    # in the ECU round, where it would do nothing.
    await at(hass, freezer, "11:05:07")
    assert writes == []
    assert coordinator.inverter_output.catch_up

    # The next healthy poll sends it.
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert writes == [0]
    assert not coordinator.inverter_output.catch_up


async def test_no_write_at_night_then_after_the_first_poll_by_day(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    daylight: MagicMock,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    writes = conn_writes(mock_ecu)
    daylight.return_value = False

    await switch(hass, SERVICE_TURN_OFF)  # No error at night.
    await at(hass, freezer, "11:05:00")
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(ENTITY).state == STATE_OFF
    assert writes == []

    daylight.return_value = True
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert writes == [0]


async def test_slow_ecu_keeps_off_and_the_next_healthy_poll_sends_it(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    writes = conn_writes(mock_ecu)
    coordinator.poll_healthy = False

    await switch(hass, SERVICE_TURN_OFF)
    assert hass.states.get(ENTITY).state == STATE_OFF

    # The clock write waits too while the ECU struggles.
    await at(hass, freezer, "11:05:00")
    assert writes == []

    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.poll_healthy
    assert writes == [0]


async def test_unanswered_write_keeps_off_and_frees_the_link_later(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    unit = mock_ecu.for_unit(1)
    unit.fail_write(GRID_CONNECTION_ADDRESS, ModbusTimeoutError())

    await switch(hass, SERVICE_TURN_OFF)
    assert coordinator.link_busy
    assert coordinator.inverter_output.last_result == "ModbusTimeoutError"

    unit.fail_write(GRID_CONNECTION_ADDRESS, None)
    writes = conn_writes(mock_ecu)
    # The next poll starts on a fresh link and then sends it.
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not coordinator.link_busy
    assert writes == [0]


async def test_failed_on_is_sent_again_after_a_healthy_poll(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    await switch(hass, SERVICE_TURN_OFF)
    unit = mock_ecu.for_unit(1)
    unit.fail_write(GRID_CONNECTION_ADDRESS, ModbusTimeoutError())

    await switch(hass, SERVICE_TURN_ON)
    assert hass.states.get(ENTITY).state == STATE_ON
    assert "Could not send inverter output on" in caplog.text

    unit.fail_write(GRID_CONNECTION_ADDRESS, None)
    writes = conn_writes(mock_ecu)
    for _ in range(2):
        freezer.tick(timedelta(minutes=5))
        await coordinator.async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)

    assert writes == [1]


async def test_switch_never_touches_the_power_limit(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    addresses: list[int] = []
    mock_ecu.for_unit(1).on_write(lambda event: addresses.append(event.address))

    await switch(hass, SERVICE_TURN_OFF)
    await at(hass, freezer, "11:05:00")
    await switch(hass, SERVICE_TURN_ON)

    assert EXPORT_LIMIT_ADDRESS not in addresses
    assert set(addresses) == {GRID_CONNECTION_ADDRESS}


async def test_off_survives_a_reload(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    await switch(hass, SERVICE_TURN_OFF)
    writes = conn_writes(mock_ecu)

    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(ENTITY).state == STATE_OFF
    # The first poll after the start sends it, then the clock again.
    assert writes == [0]
    await at(hass, freezer, "11:05:00")
    assert writes == [0, 0]


async def test_unload_stops_the_clock(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    await switch(hass, SERVICE_TURN_OFF)
    writes = conn_writes(mock_ecu)

    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await at(hass, freezer, "11:05:00")

    assert writes == []


async def test_removal_turns_the_output_on(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    await switch(hass, SERVICE_TURN_OFF)
    writes = conn_writes(mock_ecu)

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert writes == [1]


async def test_removal_with_the_output_on_writes_nothing(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    writes = conn_writes(mock_ecu)

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert writes == []


async def test_removal_tells_the_user_when_the_ecu_is_silent(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    await switch(hass, SERVICE_TURN_OFF)
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    notifications = persistent_notification._async_get_or_create_notifications(hass)
    assert any(
        "Inverter output was off" in n["message"] for n in notifications.values()
    )


async def test_debug_log_shows_the_writes(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, mock_config_entry, freezer)
    caplog.set_level(
        logging.DEBUG, logger="custom_components.apsystems_modbus.inverter_output"
    )

    await switch(hass, SERVICE_TURN_OFF)
    freezer.move_to(START.replace(minute=5, second=7))
    async_fire_time_changed(hass, START.replace(minute=5) + timedelta(seconds=7))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert "Writing grid connection register 40188 = 0" in caplog.text
    assert "answered after" in caplog.text
    assert "too late for the ECU round" in caplog.text


async def test_failed_clock_write_is_caught_up(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    await switch(hass, SERVICE_TURN_OFF)
    unit = mock_ecu.for_unit(1)
    unit.fail_write(GRID_CONNECTION_ADDRESS, ModbusTimeoutError())

    await at(hass, freezer, "11:05:00")
    assert coordinator.inverter_output.catch_up

    unit.fail_write(GRID_CONNECTION_ADDRESS, None)
    writes = conn_writes(mock_ecu)
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert writes == [0]


async def test_quick_off_and_on_ends_on(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    writes = conn_writes(mock_ecu)
    output = coordinator.inverter_output

    # On is chosen while off is still being written: on comes last.
    await asyncio.gather(output.async_set(False), output.async_set(True))
    await at(hass, freezer, "11:05:00")

    assert output.enabled
    assert writes == [0, 1]


async def off_and_quiet(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> list[int]:
    """Turn the output off; the inverters follow. Return the later writes."""
    await switch(hass, SERVICE_TURN_OFF)
    set_power(mock_ecu, {1: 0.0, 2: 0.0})
    return conn_writes(mock_ecu)


async def poll(hass: HomeAssistant, coordinator: EcuCoordinator) -> None:
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_inverter_that_starts_while_off_is_turned_off_after_the_poll(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    writes = await off_and_quiet(hass, mock_ecu)
    caplog.set_level(
        logging.DEBUG, logger="custom_components.apsystems_modbus.inverter_output"
    )

    await poll(hass, coordinator)
    assert writes == []

    # The ECU round started unit 2 again.
    set_power(mock_ecu, {2: 26.0})
    await poll(hass, coordinator)
    assert writes == [0]
    assert not coordinator.inverter_output.catch_up
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name.endswith(".inverter_output")
    ]
    assert any("units 2 (26 W) deliver while off" in line for line in lines)
    assert not any("703000000002" in line for line in lines)


@pytest.mark.parametrize(
    "watts", [{1: 3.0, 2: 10.0}, {2: 65000.0}], ids=["a_few_watts", "impossible"]
)
async def test_off_inverters_reading_no_real_power_need_no_write(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    watts: dict[int, float],
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    writes = await off_and_quiet(hass, mock_ecu)

    set_power(mock_ecu, watts)
    await poll(hass, coordinator)
    assert writes == []


async def test_power_after_a_slow_but_complete_poll_is_turned_off(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    writes = await off_and_quiet(hass, mock_ecu)

    # Each read seems to take a minute: an ECU-R by day, slow but answering.
    clock = itertools.count(step=60.0)
    set_power(mock_ecu, {2: 26.0})
    with patch(
        "custom_components.apsystems_modbus.coordinator.monotonic",
        side_effect=lambda: next(clock),
    ):
        await poll(hass, coordinator)
    assert coordinator.poll_healthy
    assert writes == [0]


async def test_power_at_night_writes_nothing(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    daylight: MagicMock,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    writes = await off_and_quiet(hass, mock_ecu)
    daylight.return_value = False

    set_power(mock_ecu, {2: 26.0})
    await poll(hass, coordinator)
    assert writes == []


async def test_old_values_of_a_silent_inverter_do_not_count(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    writes = await off_and_quiet(hass, mock_ecu)
    set_power(mock_ecu, {2: 26.0})
    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    await poll(hass, coordinator)  # unit 2 goes silent before it is read

    # Unit 2 keeps its old reading of 204 W from the setup; only this poll's
    # reads count, so nothing is written.
    await poll(hass, coordinator)
    assert coordinator.poll_healthy
    assert writes == []


async def test_due_write_and_power_give_one_write(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    writes = await off_and_quiet(hass, mock_ecu)
    coordinator.inverter_output.catch_up = True

    set_power(mock_ecu, {1: 40.0, 2: 26.0})
    await poll(hass, coordinator)
    assert writes == [0]


async def test_lasting_power_gives_at_most_one_write_per_poll(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    writes = await off_and_quiet(hass, mock_ecu)

    set_power(mock_ecu, {2: 26.0})
    await poll(hass, coordinator)
    await poll(hass, coordinator)
    assert writes == [0, 0]


async def test_setting_the_limit_does_not_reuse_the_last_poll(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    writes = await off_and_quiet(hass, mock_ecu)
    set_power(mock_ecu, {2: 26.0})
    await poll(hass, coordinator)
    assert writes == [0]

    # Setting the limit updates the listeners too, without a new poll.
    for value in (50, 0, 100):
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: "number.apsystems_ecu_power_limit", ATTR_VALUE: value},
            blocking=True,
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    assert writes == [0]


async def test_one_silent_inverter_does_not_stop_the_output(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())

    # The poll in which unit 2 first goes silent is not healthy.
    await poll(hass, coordinator)
    assert not coordinator.poll_healthy

    # From then on it is read last and no longer counts.
    await poll(hass, coordinator)
    assert coordinator.poll_healthy
    assert coordinator.data.available == {1}

    writes = conn_writes(mock_ecu)
    await switch(hass, SERVICE_TURN_OFF)
    set_power(mock_ecu, {1: 0.0})
    await at(hass, freezer, "11:05:00")
    assert writes == [0, 0]


async def test_health_of_the_last_poll_holds_during_a_poll(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    assert coordinator.poll_healthy
    seen: list[bool] = []
    read_first = coordinator.inverters[1].async_poll

    async def first(*args: object, **kwargs: object) -> object:
        seen.append(coordinator.poll_healthy)
        return await read_first(*args, **kwargs)

    with patch.object(coordinator.inverters[1], "async_poll", first):
        await poll(hass, coordinator)
    assert seen[0] is True


async def test_unanswered_write_after_the_reads_makes_the_poll_unhealthy(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    await switch(hass, SERVICE_TURN_OFF)
    set_power(mock_ecu, {1: 40.0})  # unit 1 started again
    mock_ecu.for_unit(1).fail_write(GRID_CONNECTION_ADDRESS, ModbusTimeoutError())
    await poll(hass, coordinator)
    assert not coordinator.poll_healthy


async def test_silent_lead_blocks_the_writes_that_can_wait(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    await switch(hass, SERVICE_TURN_OFF)
    mock_ecu.for_unit(1).fail_requests(ModbusTimeoutError())
    for _ in range(3):
        await poll(hass, coordinator)
    assert not coordinator.poll_healthy

    # Every write goes through the lead: nothing is sent while it is silent.
    with patch.object(
        coordinator.lead, "async_set_grid_connection", AsyncMock()
    ) as write:
        set_power(mock_ecu, {2: 40.0})
        await poll(hass, coordinator)
        await at(hass, freezer, "11:05:00")
    write.assert_not_awaited()


async def test_writes_go_before_a_unit_that_was_silent(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry, freezer)
    await switch(hass, SERVICE_TURN_OFF)
    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    await poll(hass, coordinator)  # unit 2 goes silent and is read last now
    order: list[str] = []
    read_second = coordinator.inverters[2].async_poll

    async def second(*args: object, **kwargs: object) -> object:
        order.append("read 2")
        return await read_second(*args, **kwargs)

    mock_ecu.for_unit(1).on_write(lambda event: order.append("write"))
    set_power(mock_ecu, {1: 40.0})  # unit 1 started again
    with patch.object(coordinator.inverters[2], "async_poll", second):
        await poll(hass, coordinator)
    assert order == ["write", "read 2"]
