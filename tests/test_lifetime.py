"""Tests for the lifetime energy and peak power bookkeeping."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from homeassistant.core import HomeAssistant

from custom_components.apsystems_modbus.lifetime import (
    EnergyLedger,
    async_request_start,
    async_stored_lifetime,
    ecu_day,
)

KEY = "704000000001"


TODAY = date(2026, 10, 14)


def poll(ledger: EnergyLedger, *, complete: bool = True, **counters: float) -> float:
    """Feed one poll of daily counters (Wh) on one day; return the lifetime."""
    for serial, watt_hours in counters.items():
        ledger.record_daily(serial, watt_hours, TODAY)
    ledger.finish_poll(complete=complete)
    return ledger.lifetime


@pytest.fixture
async def ledger(hass: HomeAssistant) -> EnergyLedger:
    ledger = EnergyLedger(hass, KEY)
    await ledger.async_load()
    return ledger


async def test_counts_up_during_the_day(ledger: EnergyLedger) -> None:
    assert poll(ledger, a=100, b=200) == 300
    assert poll(ledger, a=500, b=700) == 1200
    assert ledger.lifetime_of("a") == 500


async def test_night_reset_is_booked_after_confirmation(ledger: EnergyLedger) -> None:
    poll(ledger, a=9800)
    # The ECU resets the counter: held for one poll, the total stays.
    assert poll(ledger, a=0) == 9800
    # Confirmed by the next poll: the day is booked, nothing is lost.
    assert poll(ledger, a=0) == 9800
    assert poll(ledger, a=50) == 9850


async def test_single_stray_zero_is_ignored(ledger: EnergyLedger) -> None:
    poll(ledger, a=5000)
    assert poll(ledger, a=0) == 5000
    # Back up on the next poll: it was a stray reading, not a new day.
    assert poll(ledger, a=5100) == 5100
    assert poll(ledger, a=5200) == 5200


async def test_reset_while_home_assistant_was_off(
    hass: HomeAssistant, ledger: EnergyLedger
) -> None:
    poll(ledger, a=9800)
    await ledger.async_save()

    # Next morning, after a restart: the counter already restarted at night.
    restarted = EnergyLedger(hass, KEY)
    await restarted.async_load()
    assert poll(restarted, a=300) == 9800
    assert poll(restarted, a=350) == 9800 + 350


async def test_missing_inverter_keeps_its_share(ledger: EnergyLedger) -> None:
    poll(ledger, a=1000, b=2000)
    # Inverter b does not answer: its share stays in the total.
    assert poll(ledger, a=1500) == 3500


async def test_start_value_is_applied_after_the_first_poll(
    hass: HomeAssistant,
) -> None:
    await async_request_start(hass, KEY, 21_971_040)
    ledger = EnergyLedger(hass, KEY)
    await ledger.async_load()

    # Today's production is part of the start value, so it is not added twice.
    assert poll(ledger, a=1000, b=2000) == 21_971_040
    assert poll(ledger, a=1100, b=2000) == 21_971_140


async def test_start_over(hass: HomeAssistant, ledger: EnergyLedger) -> None:
    poll(ledger, a=1000)
    ledger.calibrate(50_000)
    await ledger.async_save()
    assert await async_stored_lifetime(hass, KEY) == 50_000

    await async_request_start(hass, KEY, None)
    assert await async_stored_lifetime(hass, KEY) is None


async def test_calibrate_continues_from_the_new_value(ledger: EnergyLedger) -> None:
    poll(ledger, a=1000)
    ledger.calibrate(20_000)
    assert ledger.lifetime == 20_000
    assert poll(ledger, a=1500) == 20_500


async def test_calibrate_waits_while_a_reading_waits(ledger: EnergyLedger) -> None:
    poll(ledger, a=1000)
    # A jump waits for confirmation; the new value already includes it.
    poll(ledger, a=4000)
    ledger.calibrate(20_000)
    assert ledger.lifetime == 1000
    assert poll(ledger, a=4100) == 20_000
    assert poll(ledger, a=4200) == 20_100


async def test_calibrate_is_not_held_up_by_the_nightly_reset(
    ledger: EnergyLedger,
) -> None:
    poll(ledger, a=3000)
    # The counter dropped to 0 at the reset and waits for production.
    poll(ledger, a=0)
    ledger.calibrate(50_000)
    assert ledger.lifetime == 50_000
    assert poll(ledger, a=500) == 50_500


async def test_start_value_does_not_count_a_waiting_reading_twice(
    hass: HomeAssistant, ledger: EnergyLedger
) -> None:
    # Counted 1 kWh this morning, then removed and added again hours later.
    poll(ledger, a=1000)
    await ledger.async_save()
    await async_request_start(hass, KEY, 100_000)
    again = EnergyLedger(hass, KEY)
    await again.async_load()

    assert poll(again, a=4000) == 1000
    assert poll(again, a=4100) == 100_000
    assert poll(again, a=4200) == 100_100


async def test_polls_without_data_do_not_count_towards_the_start(
    hass: HomeAssistant,
) -> None:
    await async_request_start(hass, KEY, 50_000)
    ledger = EnergyLedger(hass, KEY)
    await ledger.async_load()
    # Inverter b never answers, so no poll is complete.
    poll(ledger, complete=False, a=1000)
    for _ in range(20):
        ledger.finish_poll(complete=False, read=False)
    assert ledger.lifetime != 50_000

    for _ in range(11):
        poll(ledger, complete=False, a=1000)
    assert ledger.lifetime == 50_000


async def test_peak_power(ledger: EnergyLedger) -> None:
    ledger.record_power(4200)
    ledger.record_power(3000)
    assert ledger.peak_power == 4200
    ledger.set_peak_power(0)
    assert ledger.peak_power == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -5, 4_000_000_000])
async def test_invalid_daily_counters_are_ignored(
    ledger: EnergyLedger, value: float
) -> None:
    poll(ledger, a=1000)
    assert poll(ledger, a=value) == 1000


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -5])
async def test_invalid_power_is_ignored(ledger: EnergyLedger, value: float) -> None:
    ledger.record_power(value)
    assert ledger.peak_power == 0


async def test_state_survives_a_restart(
    hass: HomeAssistant, ledger: EnergyLedger, hass_storage: dict[str, Any]
) -> None:
    poll(ledger, a=1000)
    ledger.record_power(700)
    await ledger.async_save()
    assert f"apsystems_modbus.{KEY}" in hass_storage

    restored = EnergyLedger(hass, KEY)
    await restored.async_load()
    assert restored.lifetime == 1000
    assert restored.peak_power == 700


async def test_two_low_readings_by_day_do_not_count_twice(ledger: EnergyLedger) -> None:
    poll(ledger, a=5000)
    assert poll(ledger, a=0) == 5000
    assert poll(ledger, a=0) == 5000
    # Back at the old level: the drop was not a new day.
    assert poll(ledger, a=5100) == 5100


async def test_restart_of_the_counter_by_day_is_booked(ledger: EnergyLedger) -> None:
    # The ECU restarts its counter in the middle of the day.
    poll(ledger, a=5000)
    assert poll(ledger, a=0) == 5000
    assert poll(ledger, a=3) == 5003
    assert poll(ledger, a=10) == 5010


async def test_single_high_reading_is_ignored(ledger: EnergyLedger) -> None:
    poll(ledger, a=1000)
    assert poll(ledger, a=45_000) == 1000
    assert poll(ledger, a=1050) == 1050
    # Normal readings afterwards never book the stray value.
    assert poll(ledger, a=900) == 1050
    assert poll(ledger, a=1100) == 1100


async def test_large_rise_counts_once_confirmed(ledger: EnergyLedger) -> None:
    # After a long outage the counter has gone up a lot: real, so it counts.
    poll(ledger, a=1000)
    assert poll(ledger, a=6000) == 1000
    assert poll(ledger, a=6050) == 6050


async def test_start_value_waits_for_every_inverter(hass: HomeAssistant) -> None:
    await async_request_start(hass, KEY, 21_971_040)
    ledger = EnergyLedger(hass, KEY)
    await ledger.async_load()

    # Inverter b is missing on the first poll: the start value waits.
    poll(ledger, complete=False, a=1000)
    assert poll(ledger, a=1000, b=2000) == 21_971_040


async def test_start_value_applied_eventually_without_one_inverter(
    hass: HomeAssistant,
) -> None:
    await async_request_start(hass, KEY, 21_971_040)
    ledger = EnergyLedger(hass, KEY)
    await ledger.async_load()
    for _ in range(11):
        poll(ledger, complete=False, a=1000)
    assert poll(ledger, complete=False, a=1000) == 21_971_040


async def test_start_over_keeps_peak_power(
    hass: HomeAssistant, ledger: EnergyLedger
) -> None:
    poll(ledger, a=1000)
    ledger.record_power(4200)
    await ledger.async_save()
    await async_request_start(hass, KEY, None)

    restored = EnergyLedger(hass, KEY)
    await restored.async_load()
    assert restored.lifetime == 0
    assert restored.peak_power == 4200


DAY_1 = date(2026, 10, 13)
DAY_2 = date(2026, 10, 14)


def read(ledger: EnergyLedger, day: date, *watt_hours: float) -> float:
    """Feed polls of one inverter on ``day`` and return its lifetime."""
    for value in watt_hours:
        ledger.record_daily("a", value, day)
        ledger.finish_poll(complete=True)
    total = ledger.lifetime_of("a")
    assert total is not None
    return total


async def test_reset_missed_while_not_read_is_still_booked(
    ledger: EnergyLedger,
) -> None:
    read(ledger, DAY_1, 900, 1100)
    # Not read at night; the next day's counter is already above yesterday's.
    assert read(ledger, DAY_2, 2000) == 1100
    assert read(ledger, DAY_2, 2050) == 1100 + 2050


async def test_drop_then_rise_above_yesterday_is_a_new_day(
    ledger: EnergyLedger,
) -> None:
    read(ledger, DAY_1, 1100)
    assert read(ledger, DAY_2, 700, 1900) == 1100 + 1900


async def test_stray_reading_before_the_ecu_resets_is_dropped(
    ledger: EnergyLedger,
) -> None:
    read(ledger, DAY_1, 1100)
    # After midnight the ECU still reports yesterday's counter until ~02:15.
    assert read(ledger, DAY_2, 1100, 5, 1100, 1100) == 1100
    # Its reset is booked as usual, and the day runs up from there.
    assert read(ledger, DAY_2, 0, 0, 300) == 1100 + 300
    assert read(ledger, DAY_2, 400) == 1100 + 400


async def test_daily_counter_of_today_survives_a_restart(
    hass: HomeAssistant, ledger: EnergyLedger
) -> None:
    read(ledger, DAY_1, 1100)
    read(ledger, DAY_2, 0, 0, 800)
    await ledger.async_save()

    restarted = EnergyLedger(hass, KEY)
    await restarted.async_load()
    assert restarted.daily_of("a", DAY_2) == 800
    assert restarted.daily_of("a", DAY_1) is None
    assert restarted.daily_of("b", DAY_2) is None
    # The date is kept too, so a reading on the next day is still recognised.
    assert read(restarted, date(2026, 10, 15), 900, 950) == 1100 + 800 + 950


LOCAL = ZoneInfo("Europe/Amsterdam")


def at_times(ledger: EnergyLedger, *readings: tuple[str, float]) -> float:
    """Feed readings of one inverter at local times; return its lifetime."""
    for moment, value in readings:
        now = datetime.fromisoformat(moment).replace(tzinfo=LOCAL)
        ledger.record_daily("a", value, ecu_day(now))
        ledger.finish_poll(complete=True)
    total = ledger.lifetime_of("a")
    assert total is not None
    return total


def test_the_ecu_day_starts_after_its_nightly_restart() -> None:
    assert ecu_day(datetime(2026, 10, 14, 1, 30, tzinfo=LOCAL)) == date(2026, 10, 13)
    assert ecu_day(datetime(2026, 10, 14, 4, 30, tzinfo=LOCAL)) == date(2026, 10, 14)


async def test_final_counter_seen_after_midnight_counts_once(
    ledger: EnergyLedger,
) -> None:
    # Home Assistant was off in the evening and comes back at 01:00, while
    # the ECU still shows yesterday's final counter.
    total = at_times(
        ledger,
        ("2026-10-13 18:00", 900),
        ("2026-10-14 01:00", 1100),
        ("2026-10-14 01:05", 1100),
        ("2026-10-14 02:20", 0),
        ("2026-10-14 02:25", 0),
        ("2026-10-14 09:00", 50),
        ("2026-10-14 10:00", 300),
    )
    assert total == 1100 + 300


async def test_jump_confirmed_after_midnight_counts_once(ledger: EnergyLedger) -> None:
    total = at_times(
        ledger,
        ("2026-10-13 22:00", 1100),
        ("2026-10-13 23:50", 3500),
        ("2026-10-14 00:10", 3500),
        ("2026-10-14 02:20", 0),
        ("2026-10-14 02:25", 0),
        ("2026-10-14 09:00", 200),
    )
    assert total == 3500 + 200


async def test_stray_reading_around_midnight_counts_nothing(
    ledger: EnergyLedger,
) -> None:
    total = at_times(
        ledger,
        ("2026-10-13 22:00", 1100),
        ("2026-10-13 23:55", 5),
        ("2026-10-14 00:05", 5),
        ("2026-10-14 00:10", 1100),
        ("2026-10-14 02:20", 0),
        ("2026-10-14 02:25", 0),
        ("2026-10-14 09:00", 100),
    )
    assert total == 1100 + 100


async def test_no_counter_of_today_while_a_drop_waits(ledger: EnergyLedger) -> None:
    read(ledger, DAY_2, 0, 0, 800)
    assert ledger.daily_of("a", DAY_2) == 800
    ledger.record_daily("a", 0, DAY_2)
    # The ECU may already show its restarted counter; do not show the old one.
    assert ledger.daily_of("a", DAY_2) is None
