"""Lifetime energy and peak power, which the ECU does not keep itself.

Over Modbus the ECU only reports a daily energy counter per inverter, reset
to zero once a night. This module turns those counters into a lifetime total:
per inverter it keeps a base of all finished days plus the last counter it
accepted, and stores both so the total survives restarts, reconfiguring and
removing and re-adding the integration.

Readings that do not fit a counter going up wait for the next poll:

* A lower reading is a new day only once the counter runs up again from the
  low value. If the next reading is back at the old level, it was a stray
  reading. A real reset is booked when production starts, without losing
  anything, also when Home Assistant was off during the nightly reset.
* A jump up of more than ``MAX_STEP_WH`` counts only when the next reading
  confirms it, so one bad high reading never sticks.
* The ECU restarts the counters around 02:15, so its day is taken to start
  at 04:00 (``ecu_day``). When an inverter was not read across the restart,
  the first reading of a later ECU day may already be above the last one. A
  changed reading on a later ECU day is therefore a new day once the next
  reading confirms it; a reading back at the old level was stray.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
import math
from typing import Any, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

STORAGE_VERSION = 1
SAVE_DELAY = 30
"""Seconds to collect changes before writing; also written on unload and stop."""
MAX_DAILY_WH = 50_000
"""No microinverter produces 50 kWh in a day; a higher counter is a bad reading."""
MAX_STEP_WH = 2000
"""Largest rise between two polls taken without confirmation (2 kWh)."""
APPLY_START_AFTER_POLLS = 12
"""Apply a start value once every inverter was read, or after this many polls."""
ECU_DAY_START = timedelta(hours=4)
"""The ECU restarts its daily counters around 02:15; its day counts from 04:00."""


class _StoredLedger(TypedDict, total=False):
    counters: dict[str, dict[str, Any]]
    offset: float
    peak_power: float
    reported: float
    calibrate_to: float | None


@dataclass
class _Counter:
    """Energy of one inverter, in Wh."""

    base: float = 0.0
    """Energy of all finished days."""
    last: float | None = None
    """Last accepted daily counter."""
    pending: float | None = None
    """A reading that waits for the next poll: a drop or a large jump."""
    day: str | None = None
    """ECU day (ISO) on which ``last`` was set; ``None`` for older data."""

    @property
    def total(self) -> float:
        return self.base + (self.last or 0.0)


def ecu_day(now: datetime) -> date:
    """Return the day of the ECU's daily counters at local time ``now``."""
    return (now - ECU_DAY_START).date()


async def async_stored_lifetime(hass: HomeAssistant, ecu_key: str) -> float | None:
    """Return the stored lifetime energy of an ECU in Wh, if any."""
    ledger = EnergyLedger(hass, ecu_key)
    await ledger.async_load()
    return ledger.lifetime if ledger.has_data else None


async def async_request_start(
    hass: HomeAssistant, ecu_key: str, lifetime_wh: float | None
) -> None:
    """Ask the next setup to start the lifetime total at a given value.

    ``None`` clears what was stored, so counting starts over. The value is
    applied after the first poll, so today's production is not counted twice.
    """
    ledger = EnergyLedger(hass, ecu_key)
    await ledger.async_load()
    ledger.request_calibration(lifetime_wh)
    await ledger.async_save()


class EnergyLedger:
    """Lifetime total and peak power of one ECU, persisted in ``.storage``."""

    def __init__(self, hass: HomeAssistant, ecu_key: str) -> None:
        """Prepare the ledger for the ECU identified by ``ecu_key``."""
        self._store: Store[_StoredLedger] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{ecu_key}"
        )
        self._saved: _StoredLedger | None = None
        """What was last loaded or written, so an unchanged state is not."""
        self._counters: dict[str, _Counter] = {}
        self._offset = 0.0
        self._reported = 0.0
        self._calibrate_to: float | None = None
        self._polls_waiting = 0
        self.peak_power = 0.0

    @property
    def has_data(self) -> bool:
        """Whether anything was counted or set yet."""
        return bool(self._counters) or self._reported > 0 or self.peak_power > 0

    @property
    def lifetime(self) -> float:
        """Lifetime energy in Wh; never lower than reported before."""
        return self._reported

    def lifetime_of(self, serial: str) -> float | None:
        """Lifetime energy of one inverter in Wh, without the start offset."""
        counter = self._counters.get(serial)
        return None if counter is None or counter.last is None else counter.total

    async def async_load(self) -> None:
        """Read the stored state."""
        data = await self._store.async_load() or {}
        self._saved = data
        self._counters = {
            serial: _Counter(
                base=values.get("base") or 0.0,
                last=values.get("last"),
                pending=values.get("pending"),
                day=values.get("day"),
            )
            for serial, values in data.get("counters", {}).items()
        }
        self._offset = data.get("offset", 0.0)
        self._reported = data.get("reported", 0.0)
        self.peak_power = data.get("peak_power", 0.0)
        self._calibrate_to = data.get("calibrate_to")

    async def async_save(self) -> None:
        """Write the state now."""
        self._saved = self._as_data()
        await self._store.async_save(self._saved)

    def schedule_save(self) -> None:
        """Write the state soon if it changed, bundling several polls.

        At night nothing changes, so nothing is written to disk.
        """
        data = self._as_data()
        if data == self._saved:
            return
        self._saved = data
        self._store.async_delay_save(lambda: data, SAVE_DELAY)

    def record_daily(
        self, serial: str, watt_hours: float, day: date | None = None
    ) -> None:
        """Take a daily counter read from one inverter on ECU day ``day``."""
        if not math.isfinite(watt_hours) or not 0 <= watt_hours <= MAX_DAILY_WH:
            return
        today = (day or ecu_day(dt_util.now())).isoformat()
        counter = self._counters.setdefault(serial, _Counter())
        last = counter.last
        if last is not None and counter.day is not None and today > counter.day:
            _take_after_a_night(counter, last, watt_hours, today)
            return
        _take_same_day(counter, watt_hours)
        if counter.last != last or counter.day is None:
            counter.day = today

    def daily_of(self, serial: str, day: date) -> float | None:
        """Return the counter of one inverter if it was set on ECU day ``day``.

        Not while a drop waits for confirmation: the ECU may already show the
        restarted counter.
        """
        counter = self._counters.get(serial)
        if counter is None or counter.day != day.isoformat():
            return None
        last, pending = counter.last, counter.pending
        if last is not None and pending is not None and pending < last:
            return None
        return last

    def record_power(self, watts: float) -> None:
        """Keep the highest total power seen."""
        if math.isfinite(watts) and watts > self.peak_power:
            self.peak_power = watts

    def finish_poll(self, *, complete: bool) -> None:
        """Update the reported total after a poll and apply a requested start.

        The start value includes today's production of every inverter, so it
        waits for a poll in which all of them were read.
        """
        counted = sum(counter.total for counter in self._counters.values())
        if self._calibrate_to is not None and self._counters:
            self._polls_waiting += 1
            if complete or self._polls_waiting >= APPLY_START_AFTER_POLLS:
                self._offset = self._calibrate_to - counted
                self._reported = self._calibrate_to
                self._calibrate_to = None
                return
        self._reported = max(self._reported, self._offset + counted)

    def calibrate(self, lifetime_wh: float) -> None:
        """Continue counting from ``lifetime_wh`` from now on."""
        counted = sum(counter.total for counter in self._counters.values())
        self._offset = lifetime_wh - counted
        self._reported = lifetime_wh

    def set_peak_power(self, watts: float) -> None:
        """Set the peak power, for example to reset it."""
        self.peak_power = max(watts, 0.0)

    def request_calibration(self, lifetime_wh: float | None) -> None:
        """Start at ``lifetime_wh`` after the next poll; ``None`` starts over.

        Starting over clears the lifetime total; the peak power is kept.
        """
        if lifetime_wh is None:
            self._counters = {}
            self._offset = 0.0
            self._reported = 0.0
            self._calibrate_to = None
            return
        self._calibrate_to = lifetime_wh

    def _as_data(self) -> _StoredLedger:
        return {
            "counters": {
                serial: asdict(counter) for serial, counter in self._counters.items()
            },
            "offset": self._offset,
            "reported": self._reported,
            "peak_power": self.peak_power,
            "calibrate_to": self._calibrate_to,
        }


def _take_same_day(counter: _Counter, watt_hours: float) -> None:
    """Take a reading on the date the last counter was set."""
    last, pending = counter.last, counter.pending
    counter.pending = None
    if last is None:
        counter.last = watt_hours
    elif pending is None:
        if last <= watt_hours <= last + MAX_STEP_WH:
            counter.last = watt_hours
        else:
            counter.pending = watt_hours
    elif pending > last:
        # A large jump: real if the counter kept going from there.
        if watt_hours >= pending:
            counter.last = watt_hours
        else:
            _take_same_day(counter, watt_hours)
    elif watt_hours >= last:
        # Back at the old level: the drop was a stray reading.
        _take_same_day(counter, watt_hours)
    elif watt_hours > pending:
        # Running up again from the low value: a new day has started.
        counter.base += last
        counter.last = watt_hours
    else:
        counter.pending = watt_hours


def _take_after_a_night(
    counter: _Counter, last: float, watt_hours: float, today: str
) -> None:
    """Take a reading on a later ECU day than the last counter was set.

    An ECU that restarts its counters late still reports yesterday's counter,
    so the same value is no news. Any other value is a new day once the next
    reading confirms it by not going down; one stray reading is dropped again.
    """
    pending = counter.pending
    counter.pending = None
    if watt_hours == last:
        return
    if pending is not None and watt_hours >= pending:
        counter.base += last
        counter.last = watt_hours
        counter.day = today
        return
    counter.pending = watt_hours
