"""Poll the ECU's inverters without overloading it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
import math
from time import monotonic

from modbus_connection import ModbusConnectionError, ModbusError, ModbusTimeoutError
from modbus_connection.model import UpdateReport

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_INVERTERS,
    CONF_SERIAL_NUMBER,
    CONF_UNIT_ID,
    DOMAIN,
    MAX_INVERTER_POWER,
    MIN_POLL_GAP,
    POLL_BUDGET,
    ROUND_PERIOD,
    SETTLE_BASE,
    SETTLE_MAX,
    SETTLE_PER_INVERTER,
    SLOW_POLL_PER_UNIT,
    VALUE_MAX_AGE,
)
from .ecu import REQUEST_TIMEOUT, Inverter
from .inverter_output import InverterOutput, output_store_for
from .lifetime import MAX_DAILY_WH, EnergyLedger, ecu_day
from .power_limit import PowerLimit, store_for

_LOGGER = logging.getLogger(__name__)

type ApsystemsConfigEntry = ConfigEntry[EcuCoordinator]


def new_address_issue_id(entry_id: str) -> str:
    """Return the repair issue id telling that an inverter got a new address."""
    return f"addresses_changed_{entry_id}"


def silent_issue_id(entry_id: str) -> str:
    """Return the repair issue id telling that the ECU stopped answering."""
    return f"ecu_not_answering_{entry_id}"


SILENT_NOTICE = timedelta(hours=2)
"""How long the ECU may stay silent before the user is told."""


def settle_time(inverters: int) -> int:
    """Seconds after the start of a round when every inverter is refreshed."""
    return min(SETTLE_BASE + SETTLE_PER_INVERTER * inverters, SETTLE_MAX)


def until_next_poll(now: datetime, interval: int, settle: int) -> timedelta:
    """Time from ``now`` to the next poll moment, just after an ECU round.

    The ECU starts its rounds on the clock, so the moments are counted from
    the Unix epoch. A moment closer than ``MIN_POLL_GAP`` is skipped.
    """
    wait = interval - (now.timestamp() - settle) % interval
    if wait < MIN_POLL_GAP:
        wait += interval
    return timedelta(seconds=wait)


@dataclass
class EcuData:
    """What the last poll cycle produced."""

    available: set[int] = field(default_factory=set)
    """Unit ids whose measurements were refreshed this cycle."""
    recent: set[int] = field(default_factory=set)
    """Unit ids whose last values are at most ``VALUE_MAX_AGE`` old, this
    cycle's included."""
    power: float = 0.0
    """Sum of the AC power of the recent inverters, in W."""
    energy_today: float | None = None
    """Sum of the daily counters as the ECU reports them, in Wh."""
    online: int = 0
    """Recent inverters that are awake and on the grid, even at 0 W at dusk."""
    last_change: datetime | None = None
    """When the ECU last reported different values."""
    cut_short: bool = False
    """Whether the cycle stopped before every inverter had its turn."""
    ended_in_timeout: bool = False
    """Whether the cycle ended with an unanswered request or a broken
    connection, so the ECU may still be busy."""
    link_lost: bool = False
    """Whether the connection broke and could not be opened again."""


class EcuCoordinator(DataUpdateCoordinator[EcuData]):
    """One poll cycle reads every inverter in turn, with a budget.

    The ECU is a small device that overloads easily, and it handles one
    request at a time. Each cycle reads the inverters one after another, the
    one whose turn is longest ago first. An unanswered inverter ends the
    cycle, because the ECU is still busy with it and every further request
    would only queue up behind it. From then on that inverter comes last,
    after the writes that can wait, so a broken one never holds the others or
    a write up. After ``POLL_BUDGET`` no further inverter is started. A
    pending write is let through between two reads via ``write_lock``.

    The ECU fetches new values from the inverters every five minutes, on the
    clock. Every cycle plans the next one just after the coming round, so new
    values reach Home Assistant within about a minute and a half.
    """

    config_entry: ApsystemsConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ApsystemsConfigEntry,
        inverters: dict[int, Inverter],
        lead: Inverter,
        ledger: EnergyLedger,
    ) -> None:
        """Set up polling for the given inverters, keyed by unit id.

        ``lead`` is the inverter the ECU-wide values such as the grid
        connection and the power limit are read from.
        """
        self.settle = settle_time(len(inverters))
        """Seconds into a round before polling."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=ROUND_PERIOD),
        )
        self.inverters = inverters
        self.ledger = ledger
        self.lead = lead
        self.write_lock = asyncio.Lock()
        self.link_busy = False
        """The last request went unanswered, so the ECU may still work on it.

        Cleared when the link is opened fresh. Until then a write is refused.
        """
        self.poll_healthy = False
        """Verdict of ``_judge`` on the last poll; writes that can wait need it."""
        self.seconds_per_inverter: float | None = None
        """Read time per inverter in the last poll, for diagnostics."""
        self.ecu_device_id: str | None = None
        """Device registry id of the ECU, set once the device is registered."""
        self.serials: dict[int, str] = {
            item[CONF_UNIT_ID]: item[CONF_SERIAL_NUMBER]
            for item in entry.data[CONF_INVERTERS]
        }
        """Configured serial number per unit id."""
        self.daily: dict[int, tuple[date, float]] = {}
        """Per unit id: the ECU day and the last valid daily counter in Wh."""
        # After a restart today's counters come from storage, so Energy today
        # is complete before every inverter was read again.
        today = ecu_day(dt_util.now())
        for unit_id, serial in self.serials.items():
            if (counter := ledger.daily_of(serial, today)) is not None:
                self.daily[unit_id] = (today, counter)
        self.power_limit = PowerLimit(self, lead, store_for(hass, entry.entry_id))
        """The ECU-wide power limit, written and read back through ``lead``."""
        self.inverter_output = InverterOutput(
            self, lead, output_store_for(hass, entry.entry_id)
        )
        """Turns the output of all inverters off and on, through ``lead``."""
        self._degraded = False
        """The ECU struggled in the last cycle: read mostly measurements."""
        self._silent = False
        """Nothing answered in the last cycle."""
        self._silent_since: datetime | None = None
        self._reconnected = False
        self._turns = 0
        self._last_turn: dict[int, int] = {}
        """Per unit id: the number of the last turn it got, to share them fairly."""
        self._stuck: set[int] = set()
        """Units whose last request went unanswered."""
        self._read_at: dict[int, datetime] = {}
        """Per unit id: when its measurements were last refreshed."""
        self._status_at: dict[int, datetime] = {}
        """Per unit id: when its status block was last refreshed."""
        self._reported: set[int] = set()
        self._snapshot: dict[int, tuple[float | None, float | None]] = {}
        self._last_change: datetime | None = None

    async def _async_update_data(self) -> EcuData:
        try:
            return await self._async_read_cycle()
        except BaseException:
            self.poll_healthy = False
            raise

    # Reads the inverters in turn, within the budget, and finishes the cycle.
    async def _async_read_cycle(self) -> EcuData:
        data = EcuData()
        started = monotonic()
        # A probe of the config flow on the same link may have lowered the
        # timeout. The ECU needs the full time.
        self.lead.modbus_unit.require_timeout(REQUEST_TIMEOUT)
        if self.link_busy:
            # A write went unanswered since the last poll: start afresh.
            async with self.write_lock:
                await self._async_drop_link()
        self._reconnected = False
        writes_done = False
        stuck_before = set(self._stuck)
        lead = self.lead.unit_id
        waiting = self._poll_order()
        while waiting:
            unit_id = waiting[0]
            if unit_id in self._stuck and lead not in waiting and not writes_done:
                # Writes go before the inverters that went unanswered last
                # time, so they never queue up behind one of them. The lead
                # has had its turn by then, since every write goes through it.
                await self._async_after_reads(data, started, waiting)
                writes_done = True
            if self.link_busy:
                # A write went unanswered: the ECU still works on it.
                data.ended_in_timeout = True
                _LOGGER.debug("A write did not answer; units %s wait", waiting)
                break
            if monotonic() - started >= POLL_BUDGET:
                _LOGGER.debug("Poll budget used up; units %s wait", waiting)
                break
            waiting.pop(0)
            if await self._async_read_unit(data, unit_id, waiting):
                break

        # Inverters that went unanswered before do not make a cycle short.
        data.cut_short = any(unit not in self._stuck for unit in waiting)
        if not data.available:
            # Only a poll in which the ECU answered can mark one inverter silent.
            self._stuck = stuck_before
        if not writes_done:
            await self._async_after_reads(data, started, waiting)
        return await self._async_finish_cycle(data)

    async def _async_after_reads(
        self, data: EcuData, started: float, waiting: list[int]
    ) -> None:
        """Judge the poll, then make the writes that can wait.

        An unanswered write makes the poll unhealthy after all.
        """
        elapsed = monotonic() - started
        self.seconds_per_inverter = (
            round(elapsed / len(data.available), 1) if data.available else None
        )
        self.poll_healthy = self._judge(data, elapsed, waiting)
        await self.power_limit.async_after_poll(data)
        await self.inverter_output.async_after_poll(data)
        if self.link_busy:
            self.poll_healthy = False

    async def _async_read_unit(
        self, data: EcuData, unit_id: int, waiting: list[int]
    ) -> bool:
        """Read one inverter; return whether the cycle has to stop."""
        inverter = self.inverters[unit_id]
        self._turns += 1
        self._last_turn[unit_id] = self._turns
        try:
            report = await self._async_read(inverter, self._blocks_for(unit_id))
        except ModbusConnectionError as err:
            # What was read so far counts; the next cycle opens a new link.
            data.ended_in_timeout = data.link_lost = True
            _LOGGER.debug(
                "Unit %s: connection lost (%s); units %s wait",
                unit_id,
                type(err).__name__,
                waiting,
            )
            return True
        except ModbusTimeoutError:
            data.ended_in_timeout = True
            _LOGGER.debug("Unit %s did not answer; units %s wait", unit_id, waiting)
        except ModbusError as err:
            _LOGGER.debug("Unit %s: poll failed: %s", unit_id, type(err).__name__)
            self._stuck.discard(unit_id)
            return False
        else:
            self._take(data, unit_id, inverter, report)
            # A late status block holds the ECU up just as much.
            data.ended_in_timeout = any(
                isinstance(err, ModbusTimeoutError) for err in report.failed.values()
            )
            if data.ended_in_timeout:
                _LOGGER.debug(
                    "Unit %s: measurements read, status did not answer; units %s wait",
                    unit_id,
                    waiting,
                )
        if data.ended_in_timeout:
            self._stuck.add(unit_id)
            return True
        self._stuck.discard(unit_id)
        return False

    def _judge(self, data: EcuData, elapsed: float, waiting: list[int]) -> bool:
        """Whether the poll so far was healthy; units silent last time do not count.

        Every write goes through the lead, so it must have been read.
        """
        return (
            self.lead.unit_id in data.available
            and not data.ended_in_timeout
            and not any(unit not in self._stuck for unit in waiting)
            and elapsed <= SLOW_POLL_PER_UNIT * len(data.available)
        )

    @asynccontextmanager
    async def ready_link(self) -> AsyncIterator[None]:
        """Hold ``write_lock`` for one write on a link that is not busy.

        A write that goes unanswered leaves the link busy, like a read, so
        the next poll starts on a fresh link and no write queues up behind it.
        """
        async with self.write_lock:
            if self.link_busy:
                raise ModbusTimeoutError("the ECU is busy with an earlier request")
            try:
                yield
            except ModbusTimeoutError:
                self.link_busy = True
                raise

    async def _async_read(
        self, inverter: Inverter, names: tuple[str, ...]
    ) -> UpdateReport:
        """Read the blocks of one inverter, taking ``write_lock`` per block.

        A write then waits for one read at most. An unanswered first block
        raises ``ModbusTimeoutError``; a later one lands in the report.
        """
        report = UpdateReport()
        for name in names:
            async with self.write_lock:
                try:
                    await inverter.async_poll((name,), report)
                except ModbusTimeoutError:
                    self.link_busy = True
                    raise
                except ModbusConnectionError as err:
                    # A restart of the ECU leaves the open connection dead,
                    # so the first request after it fails even when the ECU
                    # answers again. One fresh connection usually sorts that
                    # out at once.
                    self.power_limit.note_silent_ecu()
                    if self._reconnected:
                        raise
                    self._reconnected = True
                    _LOGGER.debug(
                        "Connection lost (%s); reconnecting", type(err).__name__
                    )
                    await self._async_drop_link()
                    try:
                        await inverter.async_poll((name,), report)
                    except ModbusTimeoutError:
                        self.link_busy = True
                        raise
                if isinstance(report.failed.get(name), ModbusTimeoutError):
                    self.link_busy = True
                # The limit is in the measurements block. Still under
                # write_lock, so no write slips in between.
                if (
                    inverter is self.lead
                    and name == "measurements"
                    and name in report.updated
                ):
                    self.power_limit.note_read()
        return report

    def _blocks_for(self, unit_id: int) -> tuple[str, ...]:
        """Both blocks, or while the ECU struggles the status only when old."""
        if not self._degraded:
            return Inverter.POLLED
        read = self._status_at.get(unit_id)
        if read is None or dt_util.utcnow() - read >= timedelta(seconds=VALUE_MAX_AGE):
            return Inverter.POLLED
        return Inverter.MEASUREMENTS_ONLY

    def _take(
        self, data: EcuData, unit_id: int, inverter: Inverter, report: UpdateReport
    ) -> None:
        """Record what one inverter delivered this cycle."""
        now = dt_util.utcnow()
        if "status" in report.updated:
            self._status_at[unit_id] = now
        if "measurements" in report.updated and self._serial_matches(unit_id, inverter):
            data.available.add(unit_id)
            self._read_at[unit_id] = now

    async def _async_finish_cycle(self, data: EcuData) -> EcuData:
        """Settle the outcome of a cycle and compute what the entities show."""
        oldest = dt_util.utcnow() - timedelta(seconds=VALUE_MAX_AGE)
        data.recent = {unit for unit, at in self._read_at.items() if at >= oldest}
        _LOGGER.debug("Read units %s; recent values of %s", data.available, data.recent)
        if not data.available:
            await self._async_note_silence()
            return self._fail_or_keep(data)
        if self._silent:
            self._silent = False
            self._silent_since = None
            ir.async_delete_issue(
                self.hass, DOMAIN, silent_issue_id(self.config_entry.entry_id)
            )
            _LOGGER.info("The ECU is answering again")
        if data.cut_short:
            self._degraded = True
        else:
            self._recover()
        self._aggregate(data)
        if data.ended_in_timeout:
            # The ECU stays busy with an unanswered request and its next
            # cycle tends to get nothing on the same link; a fresh one works.
            # Under write_lock, so a write that is under way is not cut off.
            async with self.write_lock:
                await self._async_drop_link()
        self._plan_next_poll()
        return data

    def _poll_order(self) -> list[int]:
        """Return the unit ids, the one whose turn is longest ago first.

        Units whose last request went unanswered come after all others, in
        turn, so each of them is tried again. A unit that a cycle did not
        reach comes early. After a complete cycle the order is plain again.
        """
        return sorted(
            self.inverters,
            key=lambda unit: (unit in self._stuck, self._last_turn.get(unit, 0), unit),
        )

    def _serial_matches(self, unit_id: int, inverter: Inverter) -> bool:
        """Tell whether the values on this address belong to its device.

        An unknown serial number is a replaced inverter: its values simply
        continue under the device of this address. The serial number of
        another configured address means the addresses were changed in EMA
        Manager. Those values are held back until the user reconfigures, so
        two inverters never mix.
        """
        expected = self.serials[unit_id]
        actual = inverter.status.serial_number
        # Unknown when the status read failed this cycle; nothing to compare.
        if actual is None or actual == expected:
            return True
        moved_from = next(
            (other for other, serial in self.serials.items() if serial == actual),
            None,
        )
        if unit_id not in self._reported:
            self._reported.add(unit_id)
            if moved_from is None:
                _LOGGER.warning(
                    "Unit %s: a new inverter answers, its values continue "
                    "under the existing device",
                    unit_id,
                )
            else:
                _LOGGER.warning(
                    "Unit %s: the inverter of unit %s answers here, "
                    "reconfigure the integration",
                    unit_id,
                    moved_from,
                )
                self._report_new_address(actual, moved_from, unit_id)
        return moved_from is None

    def _report_new_address(self, serial: str, old: int, new: int) -> None:
        """Ask the user once to reconfigure after an address change."""
        issue_id = new_address_issue_id(self.config_entry.entry_id)
        if ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id) is not None:
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="addresses_changed",
            translation_placeholders={"id": serial, "old": str(old), "new": str(new)},
        )

    def _fail_or_keep(self, data: EcuData) -> EcuData:
        """Finish a cycle without answers: keep showing recent values, if any."""
        if not data.recent:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="connection_lost"
                if data.link_lost
                else "no_inverter_answered",
            )
        self._aggregate(data)
        return data

    async def _async_note_silence(self) -> None:
        """Try again after the next round until the ECU answers again."""
        self._degraded = True
        now = dt_util.utcnow()
        if self._silent_since is None:
            self._silent_since = now
        elif now - self._silent_since >= SILENT_NOTICE:
            # A short silence of the ECU is normal; hours point to a setting.
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                silent_issue_id(self.config_entry.entry_id),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="ecu_not_answering",
            )
        if not self._silent:
            self._silent = True
            _LOGGER.warning(
                "The ECU did not answer. Trying again after every round of "
                "the ECU until it responds. If this lasts, check the IP address "
                "and that Modbus is on in EMA Manager (Local Access)"
            )
        self._plan_next_poll()
        # A link that stopped answering is opened fresh next time. Under
        # write_lock, so a write that is under way is not cut off.
        async with self.write_lock:
            await self._async_drop_link()

    def _plan_next_poll(self) -> None:
        """Poll next just after the coming ECU round."""
        self.update_interval = until_next_poll(
            dt_util.utcnow(), ROUND_PERIOD, self.settle
        )

    def _recover(self) -> None:
        """Return to the rhythm of the ECU once a cycle completes again.

        A cycle that was not cut short means the ECU answers, even if an
        inverter stays silent (for example a broken one). The completed cycle
        plans the next poll itself, in the plain order again.
        """
        self._degraded = False
        for unit in list(self._last_turn):
            if unit not in self._stuck:
                del self._last_turn[unit]

    def daily_today(self, unit_id: int) -> float | None:
        """Return the daily counter of one inverter if it is of today."""
        entry = self.daily.get(unit_id)
        if entry is None or entry[0] != ecu_day(dt_util.now()):
            return None
        return entry[1]

    def value_ages(self) -> dict[int, int]:
        """Seconds since the measurements of each unit were last refreshed."""
        now = dt_util.utcnow()
        return {
            unit: round((now - at).total_seconds())
            for unit, at in sorted(self._read_at.items())
        }

    async def _async_drop_link(self) -> None:
        """Close the shared link; the next request opens a new one."""
        self.link_busy = False
        try:
            await self.lead.modbus_unit.disconnect()
        except ModbusError as err:
            _LOGGER.debug("Closing the link failed: %s", type(err).__name__)

    def _aggregate(self, data: EcuData) -> None:
        """Compute the ECU totals from the recent values of every inverter.

        An inverter that a slow cycle did not reach counts with its values of
        an earlier cycle, so the totals do not dip. The counters and the
        changes are taken from this cycle's reads only.
        """
        now = dt_util.now()
        today = ecu_day(now)
        for unit_id in data.recent:
            measurements = self.inverters[unit_id].measurements
            power = measurements.ac_power or 0.0
            # A bad reading would otherwise end up in the stored peak power.
            if 0 <= power <= MAX_INVERTER_POWER:
                data.power += power
            if measurements.is_online:
                data.online += 1

        snapshot: dict[int, tuple[float | None, float | None]] = {}
        # Values from different cycles could add up to a peak that never was,
        # so the peak only looks at this cycle's reads.
        fresh_power = 0.0
        # The ECU restarts its counters between midnight and 04:00, when its
        # day is still the calendar day before.
        night = today != now.date()
        restarted = False
        taken: set[int] = set()
        for unit_id in data.available:
            measurements = self.inverters[unit_id].measurements
            power = measurements.ac_power or 0.0
            if 0 <= power <= MAX_INVERTER_POWER:
                fresh_power += power
            daily = measurements.energy_today
            if daily is not None:
                # The ledger guards the lifetime total against stray readings.
                self.ledger.record_daily(self.serials[unit_id], daily, today)
                # Energy today simply follows the ECU, including its nightly
                # reset; only impossible values are left out.
                if math.isfinite(daily) and 0 <= daily <= MAX_DAILY_WH:
                    last_day, last = self.daily.get(unit_id, (None, 0.0))
                    if night and daily == 0 and last > 0 and last_day == today:
                        restarted = True
                    self.daily[unit_id] = (today, daily)
                    taken.add(unit_id)
            snapshot[unit_id] = (daily, measurements.ac_power)

        if restarted:
            # Inverters not read since the restart would still count with
            # yesterday's value, so they wait until they are read again.
            self.daily = {
                unit: entry for unit, entry in self.daily.items() if unit in taken
            }

        # An inverter that did not answer counts with its last value of the
        # same ECU day; a value from yesterday no longer counts.
        data.energy_today = (
            sum(value for day, value in self.daily.values() if day == today)
            if self.daily
            else None
        )

        self.ledger.record_power(fresh_power)
        self.ledger.finish_poll(complete=len(data.available) == len(self.inverters))
        self.ledger.schedule_save()

        fresh = {
            unit
            for unit, value in snapshot.items()
            if self._snapshot.get(unit) != value
        }
        if fresh or self._last_change is None:
            self._last_change = dt_util.utcnow()
        self._snapshot.update(snapshot)
        data.last_change = self._last_change
