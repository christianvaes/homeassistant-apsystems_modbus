"""Tests for the power limit: writing, showing and keeping the ECU aligned."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from modbus_connection import ModbusConnectionError, ModbusError, ModbusTimeoutError
from modbus_connection.mock import MockModbusConnection, MockModbusUnit, WriteEvent
import pytest

from homeassistant.components import persistent_notification
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.apsystems_modbus.const import (
    CONF_INVERTERS,
    CONF_MODEL,
    CONF_SERIAL_NUMBER,
    CONF_UNIT_ID,
)
from custom_components.apsystems_modbus.coordinator import EcuCoordinator
from custom_components.apsystems_modbus.ecu import (
    EXPORT_LIMIT_ADDRESS,
    EXPORT_LIMIT_ENABLED_ADDRESS,
    GRID_CONNECTION_ADDRESS,
    Inverter,
)
from custom_components.apsystems_modbus.power_limit import (
    REFRESH_DELAY,
    REFRESH_REPEAT,
    REWRITE_INTERVAL,
    STEP_HOLD,
    effective_limit,
)

from .conftest import INVERTERS, add_inverter, set_float32

ENTITY = "number.apsystems_ecu_power_limit"


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> EcuCoordinator:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry.runtime_data


def record_writes(unit: MockModbusUnit) -> list[tuple[int, int]]:
    """Collect (address, value) of every write to a unit."""
    writes: list[tuple[int, int]] = []
    unit.on_write(lambda event: writes.append((event.address, event.values[0])))
    return writes


def count_write_attempts(unit: MockModbusUnit) -> list[int]:
    """Collect the address of every write attempt, including failing ones."""
    attempts: list[int] = []
    original = unit.write_registers

    async def write_registers(address: int, values: list[int]) -> None:
        attempts.append(address)
        await original(address, values)

    unit.write_registers = write_registers  # type: ignore[method-assign]
    return attempts


def set_ecu(unit: MockModbusUnit, percent: int, enabled_raw: int) -> None:
    """Make the ECU report a limit (percent as whole percent, flag as raw value)."""
    unit.holding[EXPORT_LIMIT_ADDRESS] = percent * 10
    unit.holding[EXPORT_LIMIT_ENABLED_ADDRESS] = enabled_raw


async def set_limit(hass: HomeAssistant, value: float) -> None:
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: ENTITY, ATTR_VALUE: value},
        blocking=True,
    )


def notifications(hass: HomeAssistant) -> dict:
    """Return the persistent notifications currently shown."""
    return persistent_notification._async_get_or_create_notifications(hass)


def state_of(hass: HomeAssistant) -> str:
    """Return the state as a plain number string, "100" for 100 or 100.0."""
    state = hass.states.get(ENTITY)
    assert state is not None
    if state.state == STATE_UNKNOWN:
        return state.state
    return str(int(float(state.state)))


def attributes_of(hass: HomeAssistant) -> dict:
    state = hass.states.get(ENTITY)
    assert state is not None
    return state.attributes


def entry_with_four_inverters(mock_ecu: MockModbusConnection) -> MockConfigEntry:
    """A config entry for four inverters, so a poll can be cut short."""
    extra = []
    for unit_id in (3, 4):
        serial = f"70300000000{unit_id}"
        add_inverter(mock_ecu, unit_id, serial)
        extra.append(
            {CONF_UNIT_ID: unit_id, CONF_SERIAL_NUMBER: serial, CONF_MODEL: "DS3"}
        )
    return MockConfigEntry(
        domain="apsystems_modbus",
        title="APsystems ECU",
        unique_id="704000000001",
        data={"host": "192.0.2.10", "port": 502, CONF_INVERTERS: [*INVERTERS, *extra]},
    )


async def test_set_below_100_writes_percent_then_enable(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    assert state_of(hass) == "100"
    writes = record_writes(mock_ecu.for_unit(1))
    others = record_writes(mock_ecu.for_unit(2))

    await set_limit(hass, 30)

    assert writes == [(EXPORT_LIMIT_ADDRESS, 300), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert others == []  # Other inverters are never written.
    assert state_of(hass) == "30"
    assert attributes_of(hass)["desired"] == 30
    assert attributes_of(hass)["pending"] is False


async def test_value_is_rounded_to_whole_percent(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    writes = record_writes(mock_ecu.for_unit(1))

    await coordinator.power_limit.async_set(57.6)

    assert writes[0] == (EXPORT_LIMIT_ADDRESS, 580)


async def test_first_use_of_the_reported_value_writes_nothing(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    set_ecu(mock_ecu.for_unit(1), 30, 49)
    await setup_entry(hass, mock_config_entry)
    assert state_of(hass) == "30"
    writes = record_writes(mock_ecu.for_unit(1))

    await set_limit(hass, 30)

    # The ECU holds it already: writing it again would change nothing.
    assert writes == []


async def test_failed_first_write_is_pending_and_retried_at_next_poll(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())

    await set_limit(hass, 30)
    assert writes == []
    assert state_of(hass) == "100"
    assert attributes_of(hass)["desired"] == 30
    assert attributes_of(hass)["pending"] is True
    assert "did not reach the ECU" in caplog.text

    # The ECU answers again: the next poll completes the pending write.
    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 300), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert coordinator.power_limit.pending is False
    assert coordinator.power_limit.shown == 30


async def test_pending_retry_is_rate_limited(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    await set_limit(hass, 30)
    writes = record_writes(unit)

    # The first retry fails as well ...
    await coordinator.async_refresh()
    assert writes == []
    assert coordinator.power_limit.pending is True

    # ... and although the ECU would accept now, the next poll does not retry.
    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    await coordinator.async_refresh()
    assert writes == []

    freezer.tick(REWRITE_INTERVAL + timedelta(seconds=1))
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 300), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert coordinator.power_limit.pending is False


async def test_failed_second_write_at_100_is_not_undone(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)
    attempts = count_write_attempts(unit)
    unit.fail_write(EXPORT_LIMIT_ENABLED_ADDRESS, ModbusTimeoutError())

    await set_limit(hass, 100)

    # The percentage landed, the flag did not: 1000 with enable is full power.
    # The flag is not tried again at once; the next poll takes care of it.
    assert writes == [(EXPORT_LIMIT_ADDRESS, 1000)]
    assert attempts.count(EXPORT_LIMIT_ENABLED_ADDRESS) == 1
    assert coordinator.power_limit.desired == 100
    assert coordinator.power_limit.pending is True

    # The ECU reads 1000/1 back: effectively 100, so nothing is written again.
    unit.fail_write(EXPORT_LIMIT_ENABLED_ADDRESS, None)
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 1000)]
    assert coordinator.power_limit.pending is False
    assert state_of(hass) == "100"


async def test_failed_second_write_below_100_is_completed(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)
    unit.fail_write(EXPORT_LIMIT_ENABLED_ADDRESS, ModbusTimeoutError())

    await set_limit(hass, 30)
    # 300 without the flag does nothing; the choice stays pending and is not shown.
    assert writes == [(EXPORT_LIMIT_ADDRESS, 300)]
    assert state_of(hass) == "100"

    unit.fail_write(EXPORT_LIMIT_ENABLED_ADDRESS, None)
    await coordinator.async_refresh()
    # The ECU holds 300 already, so only the flag is still written.
    assert writes[1:] == [(EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert state_of(hass) == "30"


async def test_pending_is_retried_while_one_inverter_stays_silent(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    await set_limit(hass, 30)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    writes = record_writes(unit)

    # A broken inverter that never answers holds the retry up for one poll:
    # right after it the ECU is still busy. From then on it is read last,
    # after the retry.
    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()
    assert writes == []
    await coordinator.async_refresh()

    assert writes == [(EXPORT_LIMIT_ADDRESS, 300), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert coordinator.power_limit.pending is False


async def test_pending_is_not_retried_in_a_poll_cut_short(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = entry_with_four_inverters(mock_ecu)
    coordinator = await setup_entry(hass, entry)
    unit = mock_ecu.for_unit(1)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    await set_limit(hass, 30)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    writes = record_writes(unit)
    caplog.clear()

    # Unit 1 answers, but the ECU struggles: the cycle is cut short.
    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    mock_ecu.for_unit(3).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()

    assert coordinator.data.cut_short is True
    assert writes == []
    assert coordinator.power_limit.pending is True
    assert not [
        r for r in caplog.records if r.levelno > logging.DEBUG and "limit" in r.message
    ]


async def test_pending_and_same_value_again_writes_now(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusConnectionError("dropped"))
    await set_limit(hass, 30)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    writes = record_writes(unit)

    await set_limit(hass, 30)

    assert writes == [(EXPORT_LIMIT_ADDRESS, 300), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert attributes_of(hass)["pending"] is False


async def test_no_write_while_the_ecu_works_on_an_unanswered_request(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    await set_limit(hass, 30)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    writes = record_writes(unit)

    # Trying again at once would queue up behind the unanswered write.
    await set_limit(hass, 30)
    assert writes == []

    # The next poll starts on a fresh link and writes the choice.
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 300), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert attributes_of(hass)["pending"] is False


async def test_one_deviating_poll_moves_nothing(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 100)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    set_ecu(unit, 30, 1)  # the dusk glitch: factory values for one poll
    await coordinator.async_refresh()
    assert state_of(hass) == "100"
    assert writes == []

    set_ecu(unit, 100, 48)
    await coordinator.async_refresh()
    assert state_of(hass) == "100"
    assert writes == []
    assert coordinator.power_limit.deviations == 0


async def test_two_deviating_polls_show_and_restore(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    # The ECU lost the setting (restart): it reports no limit twice.
    set_ecu(unit, 100, 48)
    await coordinator.async_refresh()
    assert state_of(hass) == "30"
    assert writes == []
    await coordinator.async_refresh()

    # The control followed the ECU and the choice was written back at once.
    assert writes == [(EXPORT_LIMIT_ADDRESS, 300), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert state_of(hass) == "30"
    assert "set it back to 30 %" in caplog.text

    await coordinator.async_refresh()
    assert state_of(hass) == "30"
    assert len(writes) == 2


async def test_restore_happens_at_most_every_ten_minutes(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    # A stubborn ECU that keeps reporting no limit whatever is written.
    unit.on_write(lambda event: set_ecu(unit, 100, 48))
    set_ecu(unit, 100, 48)
    for _ in range(4):
        await coordinator.async_refresh()
    assert len(writes) == 2

    freezer.tick(REWRITE_INTERVAL + timedelta(seconds=1))
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert len(writes) == 4


async def test_failed_corrective_writes_stop_after_three(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    set_ecu(unit, 50, 1)  # The ECU moved away from the choice.

    # It answers reads but refuses every write.
    write = AsyncMock(side_effect=ModbusError("refused"))
    with patch.object(coordinator.power_limit, "_async_write", write):
        for _ in range(10):
            await coordinator.async_refresh()
            freezer.tick(REWRITE_INTERVAL + timedelta(seconds=1))

    assert write.await_count == 3
    assert "no more writes until the limit is chosen again" in caplog.text


async def test_unanswered_corrective_writes_keep_trying(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    set_ecu(mock_ecu.for_unit(1), 50, 1)

    # A busy ECU is no refusal: the choice is not given up.
    write = AsyncMock(side_effect=ModbusTimeoutError())
    with patch.object(coordinator.power_limit, "_async_write", write):
        for _ in range(6):
            await coordinator.async_refresh()
            freezer.tick(REWRITE_INTERVAL + timedelta(seconds=1))

    assert write.await_count > 3


async def test_no_correction_while_a_change_is_being_written(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)
    set_ecu(unit, 50, 1)
    await coordinator.async_refresh()

    # A new choice is being written while this poll reads the old value.
    async with coordinator.power_limit._lock:
        await coordinator.async_refresh()
    assert writes == []

    # Afterwards the next poll judges again. The flag is on, so only the
    # percentage is written.
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 300)]


async def test_a_stubborn_ecu_is_not_written_over_and_over(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    # Whatever is written, the ECU keeps reporting no limit.
    unit.on_write(lambda event: set_ecu(unit, 100, 48))
    set_ecu(unit, 100, 48)
    for _ in range(10):
        await coordinator.async_refresh()
        freezer.tick(REWRITE_INTERVAL + timedelta(seconds=1))
    # Three tries, then it waits for the user.
    assert len(writes) == 6
    assert "no more writes until the limit is chosen again" in caplog.text

    # Choosing the same value again tries again, as does a new value.
    await set_limit(hass, 30)
    assert writes[6:8] == [
        (EXPORT_LIMIT_ADDRESS, 300),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 1),
    ]
    await set_limit(hass, 40)
    assert writes[8:] == [(EXPORT_LIMIT_ADDRESS, 400)]


async def test_no_choice_means_no_write(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    for percent, raw in ((0, 49), (30, 1), (0, 49)):
        set_ecu(unit, percent, raw)
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        assert state_of(hass) == str(percent)
    assert writes == []
    assert attributes_of(hass)["desired"] is None


async def test_reload_keeps_the_choice_and_refreshes_it(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)

    writes = record_writes(mock_ecu.for_unit(1))
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert attributes_of(hass)["desired"] == 30
    # The ECU may have restarted meanwhile: the choice is sent again at once.
    assert writes == [(EXPORT_LIMIT_ADDRESS, 290)]


async def test_stale_readback_of_a_silent_control_unit_does_not_count(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    unit.fail_requests(ModbusTimeoutError())
    unit.holding[EXPORT_LIMIT_ENABLED_ADDRESS] = 48  # not readable anyway
    for _ in range(3):
        await coordinator.async_refresh()

    assert writes == []
    assert coordinator.power_limit.deviations == 0
    assert state_of(hass) == "30"


async def test_wrong_serial_on_control_unit_does_not_count(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))

    add_inverter(mock_ecu, 1, "703000000002")  # unit 2's inverter on address 1
    for _ in range(3):
        await coordinator.async_refresh()

    assert writes == []


async def test_replaced_control_unit_keeps_the_limit(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    add_inverter(mock_ecu, 1, "704000009999")  # a new inverter on address 1
    writes = record_writes(unit)

    # The ECU lost the setting: the limit is still sent back.
    set_ecu(unit, 100, 48)
    await coordinator.async_refresh()
    await coordinator.async_refresh()

    assert writes == [(EXPORT_LIMIT_ADDRESS, 300), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]


@pytest.mark.parametrize("poll_between", [True, False])
async def test_only_registers_that_differ_are_written(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    poll_between: bool,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    writes = record_writes(mock_ecu.for_unit(1))

    # The flag is on after the first change: writing it again made the ECU
    # stop answering. Without a poll in between, the last read still says
    # "no limit", so what was written counts.
    for percent in (50, 0, 100):
        await set_limit(hass, percent)
        if poll_between:
            await coordinator.async_refresh()

    assert writes == [
        (EXPORT_LIMIT_ADDRESS, 500),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 1),
        (EXPORT_LIMIT_ADDRESS, 0),
        (EXPORT_LIMIT_ADDRESS, 1000),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 0),
    ]
    assert state_of(hass) == "100"


async def test_debug_log_shows_what_is_written(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.apsystems_modbus")

    await set_limit(hass, 50)
    await coordinator.async_refresh()
    await set_limit(hass, 0)

    lines = [
        record.getMessage()
        for record in caplog.records
        if "power limit register" in record.getMessage().lower()
    ]
    assert lines == [
        "Writing power limit register 40189 = 50",
        "Writing power limit register 40193 = 1",
        "Writing power limit register 40189 = 0",
        "Power limit register 40193 already holds 1, not written",
    ]


async def test_a_change_made_elsewhere_is_seen_at_the_next_read(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 50)
    unit = mock_ecu.for_unit(1)
    # Someone switches the limit off in EMA Manager.
    unit.holding[EXPORT_LIMIT_ENABLED_ADDRESS] = ord("0")
    writes = record_writes(unit)

    await coordinator.async_refresh()
    await coordinator.async_refresh()

    # The newer read wins over what was written before it.
    assert (EXPORT_LIMIT_ENABLED_ADDRESS, 1) in writes
    assert (EXPORT_LIMIT_ADDRESS, 500) not in writes


async def test_a_write_between_two_blocks_of_a_poll_counts(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    # The ECU holds 30 %, but the flag was switched off in EMA Manager.
    set_ecu(unit, 30, ord("0"))
    await coordinator.async_refresh()
    writes = record_writes(unit)

    # A change waits for the read of the limit and is written right after it.
    lead = coordinator.lead.measurements
    read = lead.async_update
    changes: list[asyncio.Task[None]] = []

    async def read_then_change(*args: object, **kwargs: object) -> None:
        await read(*args, **kwargs)
        if not changes:
            changes.append(
                hass.async_create_task(coordinator.power_limit.async_set(30))
            )
            await asyncio.sleep(0)

    with patch.object(lead, "async_update", read_then_change):
        await coordinator.async_refresh()
    await changes[0]
    assert writes == [(EXPORT_LIMIT_ENABLED_ADDRESS, 1)]

    # The flag that change set counts, even though the poll ended later.
    await coordinator.power_limit.async_set(40)
    assert writes[1:] == [(EXPORT_LIMIT_ADDRESS, 400)]


async def test_an_unanswered_flag_is_not_sent_again_before_a_read(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)
    # The ECU takes the flag but does not answer.
    unit.fail_write(EXPORT_LIMIT_ENABLED_ADDRESS, ModbusTimeoutError())
    await set_limit(hass, 50)
    unit.fail_write(EXPORT_LIMIT_ENABLED_ADDRESS, None)

    # At the next poll a change gets in before the limit is read again.
    drop = coordinator._async_drop_link
    changes: list[asyncio.Task[None]] = []

    async def drop_then_change() -> None:
        if not changes:
            changes.append(
                hass.async_create_task(coordinator.power_limit.async_set(40))
            )
            await asyncio.sleep(0)
        await drop()

    with patch.object(coordinator, "_async_drop_link", drop_then_change):
        await coordinator.async_refresh()
    await changes[0]

    # Only answered writes are recorded: the flag was not sent a second time.
    assert writes == [(EXPORT_LIMIT_ADDRESS, 500), (EXPORT_LIMIT_ADDRESS, 400)]


async def test_a_flag_given_up_on_is_not_sent_again_before_a_read(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    writes = record_writes(mock_ecu.for_unit(1))
    lead = coordinator.lead
    send = lead.async_set_export_limit_enabled

    # The flag is sent, then the automation that set it is stopped.
    async def send_then_stop(enabled: bool) -> None:
        await send(enabled)
        raise asyncio.CancelledError

    with (
        patch.object(lead, "async_set_export_limit_enabled", send_then_stop),
        pytest.raises(asyncio.CancelledError),
    ):
        await coordinator.power_limit.async_set(50)
    await coordinator.power_limit.async_set(40)

    assert writes == [
        (EXPORT_LIMIT_ADDRESS, 500),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 1),
        (EXPORT_LIMIT_ADDRESS, 400),
    ]


async def test_a_failed_read_does_not_undo_what_was_written(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)
    await set_limit(hass, 50)

    # The next read of the limit fails, so the older read is all there is.
    unit.fail_read(40124, ModbusError("exception response"))
    await coordinator.async_refresh()
    await set_limit(hass, 40)

    assert writes == [
        (EXPORT_LIMIT_ADDRESS, 500),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 1),
        (EXPORT_LIMIT_ADDRESS, 400),
    ]


async def test_latest_of_concurrent_calls_wins(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    writes = record_writes(mock_ecu.for_unit(1))

    # A poll read is in progress, so three quick changes pile up behind it.
    async with coordinator.write_lock:
        calls = [
            asyncio.create_task(coordinator.power_limit.async_set(value))
            for value in (30, 45, 60)
        ]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    await asyncio.gather(*calls)

    # The first call was already committed; the middle one is skipped.
    # The flag was switched on by the first call, so it is not sent again.
    assert writes == [
        (EXPORT_LIMIT_ADDRESS, 300),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 1),
        (EXPORT_LIMIT_ADDRESS, 600),
    ]
    assert coordinator.power_limit.desired == 60
    assert coordinator.power_limit.shown == 60


@pytest.mark.parametrize(
    ("percent_raw", "enabled_raw", "expected"),
    [
        (300, 1, 30),
        (300, 49, 30),
        (50, 48, 100),
        (50, 0, 100),
        (1000, 49, 100),
        (0xFFFF, 1, None),
        (300, 7, None),
    ],
)
async def test_effective_limit(
    mock_ecu: MockModbusConnection,
    percent_raw: int,
    enabled_raw: int,
    expected: int | None,
) -> None:
    unit = mock_ecu.for_unit(1)
    unit.holding[EXPORT_LIMIT_ADDRESS] = percent_raw
    unit.holding[EXPORT_LIMIT_ENABLED_ADDRESS] = enabled_raw
    inverter = Inverter(unit, 1)
    await inverter.async_poll(("measurements",))

    assert effective_limit(inverter.measurements) == expected


async def test_unreadable_limit_keeps_the_shown_value(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    unit.holding[EXPORT_LIMIT_ENABLED_ADDRESS] = 7

    await coordinator.async_refresh()
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert state_of(hass) == "100"


async def test_stays_available_while_the_ecu_is_silent(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()
    assert coordinator.data.available == set()
    assert state_of(hass) == "100"

    # Setting is attempted and reports the failure instead of being skipped.
    await set_limit(hass, 30)


async def test_unknown_before_the_first_poll_then_sent_after_it(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_read(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    coordinator = await setup_entry(hass, mock_config_entry)
    assert state_of(hass) == STATE_UNKNOWN

    # Nothing read yet: the choice waits instead of being written blindly.
    writes = record_writes(mock_ecu.for_unit(1))
    await set_limit(hass, 40)
    assert writes == []
    assert coordinator.power_limit.pending

    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_read(EXPORT_LIMIT_ADDRESS, None)
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert writes == [(EXPORT_LIMIT_ADDRESS, 400), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert state_of(hass) == "40"


async def test_removal_releases_an_active_limit(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)
    unit.read_events.clear()

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(unit.read_events) == 1
    assert writes == [(EXPORT_LIMIT_ADDRESS, 1000), (EXPORT_LIMIT_ENABLED_ADDRESS, 0)]
    assert not notifications(hass)


async def test_removal_without_a_limit_leaves_the_ecu_alone(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    # A limit set by someone else, for example the installer.
    unit = mock_ecu.for_unit(1)
    set_ecu(unit, 30, 49)
    writes = record_writes(unit)
    unit.read_events.clear()

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert unit.read_events == []
    assert writes == []
    assert not notifications(hass)


async def test_removal_at_100_releases_a_change_that_did_not_arrive(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusConnectionError("dropped"))
    await set_limit(hass, 100)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    writes = record_writes(unit)

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    # The ECU still limits at 30 %: the choice of 100 is completed.
    assert writes == [(EXPORT_LIMIT_ADDRESS, 1000), (EXPORT_LIMIT_ENABLED_ADDRESS, 0)]
    assert not notifications(hass)


async def test_removal_at_100_without_an_answer_is_quiet(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 100)
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not notifications(hass)


async def test_removal_tries_the_next_inverter_when_the_first_is_silent(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    mock_ecu.for_unit(1).fail_requests(ModbusTimeoutError())
    unit_2 = mock_ecu.for_unit(2)
    set_ecu(unit_2, 30, 49)
    writes = record_writes(unit_2)

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert writes == [(EXPORT_LIMIT_ADDRESS, 1000), (EXPORT_LIMIT_ENABLED_ADDRESS, 0)]


async def test_removal_warns_when_the_ecu_is_silent(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_config_entry.entry_id not in {
        e.entry_id for e in hass.config_entries.async_entries()
    }
    assert "Could not release the power limit" in caplog.text
    assert len(notifications(hass)) == 1


async def test_grid_connection_is_never_written(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    events: list[WriteEvent] = []
    unit.on_write(events.append)

    await set_limit(hass, 0)
    await set_limit(hass, 100)
    set_ecu(unit, 30, 1)
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert events
    assert all(event.address != GRID_CONNECTION_ADDRESS for event in events)
    # The ECU only answers "write multiple registers" (function code 16).
    assert {event.function_code for event in events} == {0x10}
    # The autouse fixture fails this test if 40188 was even attempted.


async def test_restore_waits_until_the_ecu_is_not_busy(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
) -> None:
    entry = entry_with_four_inverters(mock_ecu)
    coordinator = await setup_entry(hass, entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    # The ECU lost the limit while it struggles. The first poll ends with
    # unit 2 unanswered and the second with unit 3 before unit 1 is read, so
    # nothing is written while the ECU may still be busy.
    set_ecu(unit, 100, 48)
    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    mock_ecu.for_unit(3).fail_requests(ModbusTimeoutError())
    # A poll in which nothing answered marks no unit, so learning both silent
    # units takes three polls.
    for _ in range(3):
        await coordinator.async_refresh()
    assert writes == []

    # Units 2 and 3 are read last from now on, after the limit: restored,
    # although they stay silent.
    await coordinator.async_refresh()
    assert state_of(hass) == "30"
    assert writes == [(EXPORT_LIMIT_ADDRESS, 300), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]


async def test_whole_ecu_silent_changes_nothing(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))

    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())
    await coordinator.async_refresh()
    await coordinator.async_refresh()

    assert coordinator.data.available == set()
    assert writes == []
    assert coordinator.power_limit.deviations == 0
    assert state_of(hass) == "30"


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
async def test_non_finite_value_is_refused(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    value: float,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    writes = record_writes(mock_ecu.for_unit(1))

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.power_limit.async_set(value)

    assert err.value.translation_key == "power_limit_invalid"
    assert writes == []
    assert coordinator.power_limit.desired is None


async def test_removal_does_not_write_on_an_unreadable_limit(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    unit.holding[EXPORT_LIMIT_ENABLED_ADDRESS] = 7
    writes = record_writes(unit)

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert writes == []
    assert len(notifications(hass)) == 1


async def test_refresh_sends_the_limit_again_via_one_percent_less(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))

    freezer.tick(REFRESH_DELAY - timedelta(seconds=1))
    await coordinator.async_refresh()
    assert writes == []

    freezer.tick(timedelta(seconds=1))
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 290)]
    assert coordinator.power_limit.refreshing is True
    assert state_of(hass) == "30"

    # The in-between value stays one ECU round, and is never shown.
    freezer.tick(STEP_HOLD - timedelta(seconds=1))
    await coordinator.async_refresh()
    assert len(writes) == 1
    assert state_of(hass) == "30"

    freezer.tick(timedelta(seconds=1))
    await coordinator.async_refresh()
    assert writes[1:] == [(EXPORT_LIMIT_ADDRESS, 300)]
    assert coordinator.power_limit.refreshing is False
    assert state_of(hass) == "30"


REFRESH_AT_100 = (
    100,
    # At 100 the in-between value is 99, with the limit switched on.
    [
        (EXPORT_LIMIT_ADDRESS, 990),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 1),
        (EXPORT_LIMIT_ADDRESS, 1000),
        (EXPORT_LIMIT_ENABLED_ADDRESS, 0),
    ],
)
REFRESH_AT_0 = (
    0,
    # At 0 there is nothing below, so it is 1.
    [
        (EXPORT_LIMIT_ADDRESS, 10),
        (EXPORT_LIMIT_ADDRESS, 0),
    ],
)


@pytest.mark.parametrize("case", [REFRESH_AT_100, REFRESH_AT_0])
async def test_refresh_at_the_ends_of_the_range(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    case: tuple[int, list[tuple[int, int]]],
) -> None:
    percent, expected = case
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, percent)
    writes = record_writes(mock_ecu.for_unit(1))

    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    freezer.tick(STEP_HOLD)
    await coordinator.async_refresh()

    assert writes == expected
    assert state_of(hass) == str(percent)


async def test_two_refreshes_per_change_then_none(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 100)
    writes = record_writes(mock_ecu.for_unit(1))

    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    freezer.tick(STEP_HOLD)
    await coordinator.async_refresh()
    assert len(writes) == 4

    # The second one comes an hour after the first started, not earlier.
    freezer.tick(REFRESH_REPEAT - STEP_HOLD - timedelta(seconds=1))
    await coordinator.async_refresh()
    assert len(writes) == 4
    freezer.tick(timedelta(seconds=1))
    await coordinator.async_refresh()
    freezer.tick(STEP_HOLD)
    await coordinator.async_refresh()
    assert len(writes) == 8

    for _ in range(6):
        freezer.tick(REFRESH_REPEAT)
        await coordinator.async_refresh()
    assert len(writes) == 8
    assert coordinator.power_limit.next_refresh is None


async def test_change_during_a_refresh_takes_over(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 290)]

    await set_limit(hass, 60)
    freezer.tick(STEP_HOLD)
    await coordinator.async_refresh()

    # Only the new choice was written; its own refreshes start afresh.
    assert writes[1:] == [(EXPORT_LIMIT_ADDRESS, 600)]
    assert coordinator.power_limit.refreshing is False
    assert state_of(hass) == "60"
    freezer.tick(REFRESH_DELAY - STEP_HOLD)
    await coordinator.async_refresh()
    assert writes[2:] == [(EXPORT_LIMIT_ADDRESS, 590)]


async def test_same_value_again_does_not_move_the_refresh(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    planned = coordinator.power_limit.next_refresh

    freezer.tick(timedelta(minutes=10))
    await set_limit(hass, 30)

    assert coordinator.power_limit.next_refresh == planned


async def test_no_refresh_while_an_inverter_is_offline(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))

    # Early morning: unit 2 answers but does not feed in yet.
    unit = mock_ecu.for_unit(2)
    set_float32(unit, 40138, 0.0)
    set_float32(unit, 40144, 0.0)
    set_float32(unit, 40146, 0.0)
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    assert writes == []

    set_float32(unit, 40138, 230.0)
    set_float32(unit, 40144, 150.0)
    set_float32(unit, 40146, 50.0)
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 290)]


async def test_finishing_a_refresh_waits_until_the_ecu_is_not_busy(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = entry_with_four_inverters(mock_ecu)
    coordinator = await setup_entry(hass, entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()

    # Not right after unit 2 went unanswered, nor in the poll where unit 3
    # does; then units 2 and 3 come after the refresh.
    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    mock_ecu.for_unit(3).fail_requests(ModbusTimeoutError())
    freezer.tick(STEP_HOLD)
    for _ in range(3):
        await coordinator.async_refresh()
        assert writes[1:] == []
    await coordinator.async_refresh()

    assert writes[1:] == [(EXPORT_LIMIT_ADDRESS, 300)]


async def test_failed_finish_is_retried_at_the_next_poll(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()

    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    freezer.tick(STEP_HOLD)
    await coordinator.async_refresh()
    assert coordinator.power_limit.refreshing is True
    assert attributes_of(hass)["pending"] is False

    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    await coordinator.async_refresh()
    assert writes[1:] == [(EXPORT_LIMIT_ADDRESS, 300)]
    assert coordinator.power_limit.refreshing is False


@pytest.mark.parametrize(
    ("percent", "error"),
    [
        # At dusk the ECU often stops answering without dropping the connection.
        (30, ModbusTimeoutError()),
        # A broken connection, as after a restart of the ECU, at no limit.
        (100, ModbusConnectionError("restart")),
    ],
    ids=["quiet_evening", "restart_at_100"],
)
async def test_silent_ecu_does_not_refresh(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    percent: int,
    error: Exception,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, percent)
    writes = record_writes(mock_ecu.for_unit(1))

    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(error)
    await coordinator.async_refresh()
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(None)
    await coordinator.async_refresh()

    assert writes == []


async def test_lost_connection_is_reopened_in_the_same_poll(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))
    inverter = coordinator.inverters[1]
    original = inverter.async_poll
    calls = 0

    async def lose_once(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ModbusConnectionError("connection reset by peer")
        return await original(*args, **kwargs)

    inverter.async_poll = lose_once  # type: ignore[method-assign]
    await coordinator.async_refresh()

    # Everything stays available, and below 100 the limit is sent again.
    # Unit 1 is read block by block: the lost one again, then the status.
    assert coordinator.last_update_success is True
    assert coordinator.data.available == {1, 2}
    assert calls == 3
    assert writes == [(EXPORT_LIMIT_ADDRESS, 290)]


def store_choice(hass_storage: dict, entry: MockConfigEntry, data: dict) -> None:
    """Put a chosen limit in the store, as left by an earlier run."""
    hass_storage[f"apsystems_modbus.power_limit.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"apsystems_modbus.power_limit.{entry.entry_id}",
        "data": data,
    }


async def test_choice_is_restored_after_a_restart(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    mock_config_entry.add_to_hass(hass)
    store_choice(hass_storage, mock_config_entry, {"desired": 30})
    set_ecu(mock_ecu.for_unit(1), 30, 49)
    writes = record_writes(mock_ecu.for_unit(1))
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator: EcuCoordinator = mock_config_entry.runtime_data

    # The ECU may have restarted meanwhile: the first poll sends it again.
    assert attributes_of(hass)["desired"] == 30
    assert writes == [(EXPORT_LIMIT_ADDRESS, 290)]
    assert coordinator.power_limit.refreshing is True


async def test_choice_is_kept_with_the_number_disabled(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    mock_config_entry.add_to_hass(hass)
    store_choice(hass_storage, mock_config_entry, {"desired": 30})
    entity_registry = er.async_get(hass)
    entity_registry.async_get_or_create(
        NUMBER_DOMAIN,
        "apsystems_modbus",
        "ecu-704000000001-power_limit",
        config_entry=mock_config_entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(ENTITY) is None
    assert mock_config_entry.runtime_data.power_limit.desired == 30


async def test_factory_value_after_a_power_cut_is_put_back(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    mock_config_entry.add_to_hass(hass)
    store_choice(hass_storage, mock_config_entry, {"desired": 100})
    unit = mock_ecu.for_unit(1)
    set_ecu(unit, 30, 1)  # the ECU's factory value
    writes = record_writes(unit)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator: EcuCoordinator = mock_config_entry.runtime_data

    # The first poll after the start writes the remembered 100 back.
    assert writes == [(EXPORT_LIMIT_ADDRESS, 1000), (EXPORT_LIMIT_ENABLED_ADDRESS, 0)]
    assert state_of(hass) == "100"
    assert coordinator.power_limit.desired == 100


@pytest.mark.parametrize(
    "stored", [{"desired": 130}, {"desired": "0"}, {"desired": True}, {}]
)
async def test_unusable_stored_choice_is_ignored(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
    stored: dict,
) -> None:
    mock_config_entry.add_to_hass(hass)
    store_choice(hass_storage, mock_config_entry, stored)
    writes = record_writes(mock_ecu.for_unit(1))
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator: EcuCoordinator = mock_config_entry.runtime_data

    await coordinator.async_refresh()
    assert attributes_of(hass)["desired"] is None
    assert writes == []


async def test_removal_forgets_the_choice(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    # Unloading writes the store at once.
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    key = f"apsystems_modbus.power_limit.{mock_config_entry.entry_id}"
    assert hass_storage[key]["data"] == {"desired": 30}

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert key not in hass_storage


async def test_failed_in_between_write_is_retried_at_the_next_poll(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    assert writes == []
    assert coordinator.power_limit.refreshing is False
    assert attributes_of(hass)["pending"] is False

    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 290)]


async def test_failed_change_during_a_refresh_is_retried_at_once(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 50)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 490)]

    # The user releases, but the ECU does not take the write.
    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    await set_limit(hass, 100)
    assert coordinator.power_limit.refreshing is False

    # The next poll delivers it, without waiting for the refresh to end.
    # The unanswered write did set 100, so only the flag is still needed.
    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    await coordinator.async_refresh()
    assert writes[2:] == [(EXPORT_LIMIT_ENABLED_ADDRESS, 0)]
    assert coordinator.power_limit.pending is False


async def test_lost_connection_refreshes_at_most_once_an_hour(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    writes = record_writes(mock_ecu.for_unit(1))

    def silent_ecu() -> None:
        for unit_id in (1, 2):
            mock_ecu.for_unit(unit_id).fail_requests(ModbusConnectionError("down"))

    def ecu_back() -> None:
        for unit_id in (1, 2):
            mock_ecu.for_unit(unit_id).fail_requests(None)

    # A restart of the ECU breaks the connection for a while. Below 100 the
    # limit is sent again once it answers.
    silent_ecu()
    await coordinator.async_refresh()
    assert coordinator.data.available == set()
    ecu_back()
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 290)]
    freezer.tick(STEP_HOLD)
    await coordinator.async_refresh()
    assert len(writes) == 2

    # A network that drops the connection again soon does not cause a loop.
    freezer.tick(timedelta(minutes=10))
    silent_ecu()
    await coordinator.async_refresh()
    ecu_back()
    await coordinator.async_refresh()
    assert len(writes) == 2


ENTITY_KW = "number.apsystems_ecu_power_limit_kw"
AC_POWER = 40144


def entry_with_power(kw: float) -> MockConfigEntry:
    """The usual two-inverter entry, with an installation power in the options."""
    return MockConfigEntry(
        domain="apsystems_modbus",
        title="APsystems ECU",
        unique_id="704000000001",
        data={"host": "192.0.2.10", "port": 502, CONF_INVERTERS: INVERTERS},
        options={"installation_power": kw},
    )


def set_power(mock_ecu: MockModbusConnection, watts: float) -> None:
    """Make both inverters report the same AC power."""
    for unit_id in (1, 2):
        set_float32(mock_ecu.for_unit(unit_id), AC_POWER, watts)


async def set_kw(hass: HomeAssistant, value: float) -> None:
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: ENTITY_KW, ATTR_VALUE: value},
        blocking=True,
    )


@pytest.mark.parametrize(
    ("total", "kw", "percent"),
    [(6.0, 0.6, 10), (5.8, 2.0, 35), (5.8, 0, 0), (5.8, 5.8, 100)],
)
async def test_kw_is_set_as_the_next_whole_percent(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    total: float,
    kw: float,
    percent: int,
) -> None:
    await setup_entry(hass, entry_with_power(total))
    writes = record_writes(mock_ecu.for_unit(1))

    await set_kw(hass, kw)

    # The ECU starts without a limit, so 100 needs no write.
    expected = [(EXPORT_LIMIT_ADDRESS, percent * 10), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]
    assert writes == (expected if percent < 100 else [])
    assert state_of(hass) == str(percent)


async def test_kw_and_percent_show_the_same_setting(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    await setup_entry(hass, entry_with_power(5.8))
    assert float(hass.states.get(ENTITY_KW).state) == 5.8

    await set_limit(hass, 30)
    assert float(hass.states.get(ENTITY_KW).state) == 1.7

    await set_kw(hass, 2.0)
    assert state_of(hass) == "35"
    assert float(hass.states.get(ENTITY_KW).state) == 2.0


async def refresh_after_change(
    coordinator: EcuCoordinator, freezer: FrozenDateTimeFactory
) -> None:
    """Let the first planned refresh, if any, run to its end."""
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    freezer.tick(STEP_HOLD)
    await coordinator.async_refresh()


async def test_inverter_above_the_limit_is_refreshed(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, entry_with_power(1.46))
    await set_limit(hass, 0)
    set_power(mock_ecu, 25.0)  # The inverters follow the limit.
    await refresh_after_change(coordinator, freezer)
    writes = record_writes(mock_ecu.for_unit(1))

    # 0 % of 730 W allows about 56 W; both deliver 300 W, so they missed it.
    set_power(mock_ecu, 300.0)
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    assert writes == []  # One poll is not enough.

    freezer.tick(timedelta(minutes=1))
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 10)]
    assert coordinator.power_limit.overshoot_refreshes == 1


@pytest.mark.parametrize("watts", [50.0, 6000.0])
async def test_no_refresh_below_the_limit_or_for_impossible_values(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    freezer: FrozenDateTimeFactory,
    watts: float,
) -> None:
    coordinator = await setup_entry(hass, entry_with_power(1.46))
    await set_limit(hass, 0)
    set_power(mock_ecu, 25.0)  # The inverters follow the limit.
    await refresh_after_change(coordinator, freezer)
    writes = record_writes(mock_ecu.for_unit(1))

    set_power(mock_ecu, watts)
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    freezer.tick(timedelta(minutes=1))
    await coordinator.async_refresh()

    assert writes == []


async def test_no_overshoot_check_soon_after_a_write(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, entry_with_power(1.46))
    set_power(mock_ecu, 300.0)
    await set_limit(hass, 0)
    writes = record_writes(mock_ecu.for_unit(1))

    # The new limit may still be on its way to the inverters.
    for _ in range(2):
        freezer.tick(timedelta(minutes=5))
        await coordinator.async_refresh()
    assert writes == []


async def test_brake_after_two_refreshes_tells_the_user(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = entry_with_power(1.46)
    coordinator = await setup_entry(hass, entry)
    await set_limit(hass, 0)
    await refresh_after_change(coordinator, freezer)
    set_power(mock_ecu, 300.0)  # e.g. DS3s of 880 W entered as 730 W

    for _ in range(2):
        freezer.tick(REFRESH_DELAY)
        await coordinator.async_refresh()
        freezer.tick(timedelta(minutes=1))
        await coordinator.async_refresh()  # starts a refresh
        freezer.tick(STEP_HOLD)
        await coordinator.async_refresh()  # finishes it
    assert coordinator.power_limit.overshoot_refreshes == 2

    writes = record_writes(mock_ecu.for_unit(1))
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    freezer.tick(timedelta(minutes=1))
    await coordinator.async_refresh()

    assert writes == []
    issue_id = f"check_installation_power_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue("apsystems_modbus", issue_id)
    assert "check the installation power" in caplog.text

    # A new choice starts over and clears the notice.
    await set_limit(hass, 20)
    assert ir.async_get(hass).async_get_issue("apsystems_modbus", issue_id) is None
    assert coordinator.power_limit.overshoot_refreshes == 0


async def test_restart_soon_after_a_refresh_is_postponed_not_dropped(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 0)
    await refresh_after_change(coordinator, freezer)  # started at +15
    freezer.tick(REFRESH_REPEAT - STEP_HOLD)
    await coordinator.async_refresh()  # second refresh starts at +75
    freezer.tick(STEP_HOLD)
    await coordinator.async_refresh()
    assert coordinator.power_limit.next_refresh is None
    writes = record_writes(mock_ecu.for_unit(1))

    # The ECU restarts 25 minutes after the last refresh started.
    freezer.tick(timedelta(minutes=19))
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusConnectionError("restart"))
    await coordinator.async_refresh()
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(None)
    await coordinator.async_refresh()
    assert writes == []  # Not now: within the hour after the last refresh.

    freezer.tick(timedelta(minutes=35))
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 10)]


async def test_two_different_readbacks_do_not_move_the_slider(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    assert state_of(hass) == "100"

    set_ecu(unit, 50, 1)
    await coordinator.async_refresh()
    set_ecu(unit, 70, 1)
    await coordinator.async_refresh()
    assert state_of(hass) == "100"

    await coordinator.async_refresh()
    assert state_of(hass) == "70"


@pytest.mark.parametrize(
    ("language", "opening"),
    [
        ("nl", "De APsystems ECU kon niet"),
        ("de", "Die APsystems ECU konnte"),
        ("fr", "L'ECU APsystems n'a pas pu"),
    ],
)
async def test_removal_notice_is_in_the_users_language(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    language: str,
    opening: str,
) -> None:
    await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    await hass.config.async_update(language=language)
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    (notice,) = notifications(hass).values()
    assert notice["message"].startswith(opening)


async def test_reload_clears_the_installation_power_notice(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    entry = entry_with_power(1.46)
    await setup_entry(hass, entry)
    issue_id = f"check_installation_power_{entry.entry_id}"
    ir.async_create_issue(
        hass,
        "apsystems_modbus",
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="check_installation_power",
        translation_placeholders={"limit": "0"},
    )

    # Saving the options reloads the integration, which checks afresh.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ir.async_get(hass).async_get_issue("apsystems_modbus", issue_id) is None


async def test_watched_limit_is_not_refreshed_on_a_timer(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, entry_with_power(1.46))
    await set_limit(hass, 9)
    set_power(mock_ecu, 25.0)  # The inverters follow the limit.
    writes = record_writes(mock_ecu.for_unit(1))

    # The power would show a missed limit, so the ECU is not written again.
    for wait in (REFRESH_DELAY, STEP_HOLD, REFRESH_REPEAT, STEP_HOLD):
        freezer.tick(wait)
        await coordinator.async_refresh()

    assert writes == []
    assert coordinator.power_limit.next_refresh is None


async def test_watched_limit_after_an_ecu_restart_is_only_set_back(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, entry_with_power(1.46))
    await set_limit(hass, 9)
    set_power(mock_ecu, 25.0)
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusConnectionError("restart"))
    await coordinator.async_refresh()
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(None)
    await coordinator.async_refresh()
    assert writes == []  # No refresh after the restart.

    # The restarted ECU reports another value: the choice is written back.
    unit.holding[EXPORT_LIMIT_ADDRESS] = 80
    for _ in range(2):
        freezer.tick(timedelta(minutes=5))
        await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 90)]


async def test_watched_limit_after_a_start_is_checked_on_the_power(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = entry_with_power(1.46)
    await setup_entry(hass, entry)
    await set_limit(hass, 9)
    set_power(mock_ecu, 25.0)
    writes = record_writes(mock_ecu.for_unit(1))

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert writes == []  # Nothing written at the start.

    # An inverter that lost the limit meanwhile is still noticed.
    coordinator: EcuCoordinator = entry.runtime_data
    set_power(mock_ecu, 300.0)
    for _ in range(2):
        freezer.tick(timedelta(minutes=5))
        await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 80)]
    assert coordinator.power_limit.overshoot_refreshes == 1


async def test_watched_limit_missed_again_after_a_night_is_refreshed(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, entry_with_power(1.46))
    await set_limit(hass, 0)
    set_power(mock_ecu, 25.0)
    await refresh_after_change(coordinator, freezer)

    async def missed_for_a_day() -> None:
        set_power(mock_ecu, 300.0)
        for _ in range(2):
            freezer.tick(REFRESH_DELAY)
            await coordinator.async_refresh()
            freezer.tick(timedelta(minutes=1))
            await coordinator.async_refresh()  # starts a refresh
            freezer.tick(STEP_HOLD)
            await coordinator.async_refresh()  # finishes it

    await missed_for_a_day()
    assert coordinator.power_limit.overshoot_refreshes == 2

    # Night: every inverter is off the grid.
    for unit_id in (1, 2):
        unit = mock_ecu.for_unit(unit_id)
        set_float32(unit, 40138, 0.0)
        set_float32(unit, AC_POWER, 0.0)
        set_float32(unit, 40146, 0.0)
    freezer.tick(timedelta(hours=8))
    await coordinator.async_refresh()
    assert coordinator.power_limit.overshoot_refreshes == 0

    # Next day the ECU restarted overnight and the inverters lost the limit.
    for unit_id in (1, 2):
        unit = mock_ecu.for_unit(unit_id)
        set_float32(unit, 40138, 230.0)
        set_float32(unit, 40146, 50.0)
    writes = record_writes(mock_ecu.for_unit(1))
    await missed_for_a_day()
    assert writes[:2] == [(EXPORT_LIMIT_ADDRESS, 10), (EXPORT_LIMIT_ADDRESS, 0)]
    assert coordinator.power_limit.overshoot_refreshes == 2


async def test_notice_goes_with_a_new_choice_after_a_night(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = entry_with_power(1.46)
    coordinator = await setup_entry(hass, entry)
    await set_limit(hass, 0)
    set_power(mock_ecu, 300.0)  # e.g. DS3s of 880 W entered as 730 W
    for _ in range(3):
        freezer.tick(REFRESH_DELAY)
        await coordinator.async_refresh()
        freezer.tick(timedelta(minutes=1))
        await coordinator.async_refresh()
        freezer.tick(STEP_HOLD)
        await coordinator.async_refresh()
    issue_id = f"check_installation_power_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue("apsystems_modbus", issue_id)

    # Night starts the count over, but the notice stays until a new choice.
    for unit_id in (1, 2):
        unit = mock_ecu.for_unit(unit_id)
        set_float32(unit, 40138, 0.0)
        set_float32(unit, AC_POWER, 0.0)
        set_float32(unit, 40146, 0.0)
    freezer.tick(timedelta(hours=8))
    await coordinator.async_refresh()
    assert coordinator.power_limit.overshoot_refreshes == 0
    assert ir.async_get(hass).async_get_issue("apsystems_modbus", issue_id)

    await set_limit(hass, 20)
    assert ir.async_get(hass).async_get_issue("apsystems_modbus", issue_id) is None


async def test_overshoot_count_holds_until_a_new_choice(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, entry_with_power(1.46))
    await set_limit(hass, 0)
    await refresh_after_change(coordinator, freezer)
    set_power(mock_ecu, 300.0)
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    freezer.tick(timedelta(minutes=1))
    await coordinator.async_refresh()
    freezer.tick(STEP_HOLD)
    await coordinator.async_refresh()
    assert coordinator.power_limit.overshoot_refreshes == 1

    # A cloud is no proof the limit holds: the count stays.
    set_power(mock_ecu, 25.0)
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    assert coordinator.power_limit.overshoot_refreshes == 1

    await set_limit(hass, 10)
    assert coordinator.power_limit.overshoot_refreshes == 0


async def test_choice_is_stored_without_waiting_for_a_stop(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    freezer.tick(timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    key = f"apsystems_modbus.power_limit.{mock_config_entry.entry_id}"
    assert hass_storage[key]["data"] == {"desired": 30}


async def test_no_refresh_at_the_start_without_a_limit(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    mock_config_entry.add_to_hass(hass)
    store_choice(hass_storage, mock_config_entry, {"desired": 100})
    writes = record_writes(mock_ecu.for_unit(1))
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    # Saving the options reloads the integration: no 99 % step at 100.
    assert writes == []
    assert mock_config_entry.runtime_data.power_limit.refreshing is False


async def test_setting_the_shown_kw_again_changes_nothing(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    await setup_entry(hass, entry_with_power(5.8))
    await set_limit(hass, 31)
    assert float(hass.states.get(ENTITY_KW).state) == 1.8

    # 1.8 kW on its own would be 32 %.
    await set_kw(hass, 1.8)
    assert state_of(hass) == "31"


async def test_kw_number_goes_when_the_installation_power_is_cleared(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    entry = entry_with_power(5.8)
    await setup_entry(hass, entry)
    assert hass.states.get(ENTITY_KW) is not None

    hass.config_entries.async_update_entry(entry, options={})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert er.async_get(hass).async_get(ENTITY_KW) is None
    assert hass.states.get(ENTITY_KW) is None


async def test_refresh_waits_at_most_an_hour_for_a_dark_inverter(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 0)
    writes = record_writes(mock_ecu.for_unit(1))

    # Unit 2 answers but stays dark all day, e.g. a broken panel.
    unit = mock_ecu.for_unit(2)
    set_float32(unit, 40138, 0.0)
    set_float32(unit, 40144, 0.0)
    set_float32(unit, 40146, 0.0)
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    assert writes == []

    freezer.tick(REFRESH_REPEAT)
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 10)]


async def test_no_refresh_at_night(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 0)
    writes = record_writes(mock_ecu.for_unit(1))

    for unit_id in (1, 2):
        set_float32(mock_ecu.for_unit(unit_id), 40138, 0.0)
        set_float32(mock_ecu.for_unit(unit_id), 40144, 0.0)
        set_float32(mock_ecu.for_unit(unit_id), 40146, 0.0)
    # The ECU may keep 1 W of an inverter that is off for the night.
    set_float32(mock_ecu.for_unit(2), 40144, 1.0)
    freezer.tick(REFRESH_DELAY + REFRESH_REPEAT)
    await coordinator.async_refresh()
    assert writes == []


async def test_no_refresh_while_a_choice_is_pending(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 30)
    await refresh_after_change(coordinator, freezer)
    unit = mock_ecu.for_unit(1)

    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusTimeoutError())
    await set_limit(hass, 20)
    await coordinator.async_refresh()  # the retry fails as well
    assert attributes_of(hass)["pending"] is True

    # The planned refresh of 20 falls due, but 20 never reached the ECU: the
    # choice itself is written, not the in-between value 19.
    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)
    writes = record_writes(unit)
    freezer.tick(REFRESH_DELAY)
    await coordinator.async_refresh()
    assert writes == [(EXPORT_LIMIT_ADDRESS, 200)]
    assert coordinator.power_limit.refreshing is False
    assert attributes_of(hass)["pending"] is False


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
async def test_non_finite_kw_is_refused(
    hass: HomeAssistant, mock_ecu: MockModbusConnection, value: float
) -> None:
    await setup_entry(hass, entry_with_power(5.8))
    writes = record_writes(mock_ecu.for_unit(1))

    with pytest.raises(HomeAssistantError):
        await set_kw(hass, value)
    assert writes == []


async def test_limit_waits_while_the_lead_was_never_read(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    # The lead stays silent, unit 2 answers, so there is data but no limit.
    set_ecu(mock_ecu.for_unit(1), 0, 1)
    mock_ecu.for_unit(1).fail_requests(ModbusTimeoutError())
    coordinator = await setup_entry(hass, mock_config_entry)
    await coordinator.async_refresh()
    assert coordinator.data is not None
    writes = record_writes(mock_ecu.for_unit(1))

    await set_limit(hass, 0)
    assert writes == []

    mock_ecu.for_unit(1).fail_requests(None)
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert writes == []
    assert not coordinator.power_limit.pending


async def test_unreadable_limit_that_was_read_can_still_be_set(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    # The registers were read, but the flag holds a value the ECU never uses.
    set_ecu(mock_ecu.for_unit(1), 30, 7)
    await setup_entry(hass, mock_config_entry)
    writes = record_writes(mock_ecu.for_unit(1))

    await set_limit(hass, 40)
    assert writes == [(EXPORT_LIMIT_ADDRESS, 400), (EXPORT_LIMIT_ENABLED_ADDRESS, 1)]


async def test_factory_table_with_a_failed_choice_plans_refreshes(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    unit = mock_ecu.for_unit(1)
    unit.fail_write(EXPORT_LIMIT_ADDRESS, ModbusError("refused"))
    await set_limit(hass, 50)
    assert coordinator.power_limit.pending
    unit.fail_write(EXPORT_LIMIT_ADDRESS, None)

    # The ECU shows its factory table; writing back also delivers the choice.
    set_ecu(unit, 30, 1)
    await coordinator.async_refresh()
    assert not coordinator.power_limit.pending
    assert coordinator.power_limit.next_refresh is not None


@pytest.mark.parametrize(("flag_raw", "new_refreshes"), [(1, False), (49, True)])
async def test_only_the_factory_table_plans_no_new_refreshes(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    *,
    flag_raw: int,
    new_refreshes: bool,
) -> None:
    coordinator = await setup_entry(hass, mock_config_entry)
    await set_limit(hass, 50)
    planned = coordinator.power_limit.next_refresh
    unit = mock_ecu.for_unit(1)
    writes = record_writes(unit)

    # 30 % with a raw 1 is the ECU's factory table; with ASCII "1" it is a
    # limit the ECU really holds.
    freezer.tick(timedelta(minutes=5))
    set_ecu(unit, 30, flag_raw)
    await coordinator.async_refresh()
    await coordinator.async_refresh()

    assert writes[0] == (EXPORT_LIMIT_ADDRESS, 500)
    assert state_of(hass) == "50"
    assert (coordinator.power_limit.next_refresh != planned) is new_refreshes
