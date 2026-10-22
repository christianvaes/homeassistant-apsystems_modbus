"""The power limit of the ECU: one setting that applies to every inverter.

The ECU keeps a single limit for the whole installation. It is served through
each inverter's unit, but a write through any one unit applies to all. The
limit is a percentage with an on/off flag; a percentage without the flag does
nothing, and 100 % releases the inverters. This module presents both as one
number: 100 means no limit, anything lower means limited.

What makes this harder than it sounds is that the ECU is not a reliable
messenger. It does not always read back correctly: at dusk it occasionally
reports 30 % for one poll, and after a restart it may report its factory
values. It does not always reach every inverter either: an inverter can miss
a change, the ECU never sends it again, and a restart of the ECU clears the
limit on every inverter while it keeps reporting the old value. Over Modbus
only the last one partly shows, as a failed poll. So:

* The value shown to the user is what the ECU reads back, but a change is
  only adopted once two polls in a row report the same other value, so a
  one-poll glitch does not move the control.
* The value the user chose is stored, so it survives a restart of Home
  Assistant and a reload.
* While a choice exists, the ECU is made to follow it: once two polls in a
  row read something else, the choice is written again, at most once per
  ten minutes and at most ``MAX_REWRITES`` times; then it waits for a new
  choice. A failed user write is retried after the next healthy poll, also
  at most once per ten minutes. Before the limit was read once, a choice
  waits for that read, so nothing is written blind.
* The ECU sometimes falls back to its factory table (30 % with the flag as a
  raw 1; written values read back as ASCII). The inverters do not follow it,
  so writing the choice back only repairs the table and plans no refreshes.
* The choice is sent again ("refreshed") 15 and 75 minutes after it was set,
  once after Home Assistant starts, and, below 100, after the connection to
  the ECU broke, which is how a restart of the ECU shows. The ECU only
  passes a changed percentage on to the inverters, so a refresh writes one
  percent less (or more, at 0), waits at least one ECU round and then writes
  the chosen value again. The in-between value is not shown.
* With the installation power known, an inverter that delivers clearly more
  than the limit allows has missed it, and the limit is refreshed at once.
  Below 100 a missed limit then shows as soon as it matters, so the timed
  refreshes above are left out: they would only add writes to an ECU that
  is easily overloaded.
  If two such refreshes do not help, the power is probably entered wrong:
  the user is told instead of the ECU being written over and over.
* Only a register that differs from what the ECU holds is written. Setting
  the flag again while it was on made the whole ECU stop answering, twice,
  until it was switched off and on.

Lock order: ``PowerLimit._lock`` first, then the coordinator's ``write_lock``
for each register write. The coordinator holds ``write_lock`` only while it
reads one block and releases it in between, and calls ``async_after_poll``
between two reads without ``write_lock``. Calling it while holding
``write_lock`` would deadlock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import math
from typing import TYPE_CHECKING, Any

from modbus_connection import ModbusError, ModbusTimeoutError

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import CONF_INSTALLATION_POWER, DOMAIN, MAX_INVERTER_POWER
from .ecu import (
    EXPORT_LIMIT_ADDRESS,
    EXPORT_LIMIT_ENABLED_ADDRESS,
    MAX_EXPORT_LIMIT,
    Inverter,
    Measurements,
)

if TYPE_CHECKING:
    from .coordinator import EcuCoordinator, EcuData

_LOGGER = logging.getLogger(__name__)

UNANSWERED = (ModbusTimeoutError, asyncio.CancelledError)
"""A write that ended this way may still have reached the ECU."""

NO_LIMIT = MAX_EXPORT_LIMIT
"""The percentage that means "not limited"."""
CONFIRM_POLLS = 2
"""Polls in a row that must agree before a changed readback is adopted."""
REWRITE_INTERVAL = timedelta(minutes=10)
"""Least time between two corrective writes after a deviating readback."""
MAX_REWRITES = 3
"""Corrective writes for one choice before waiting for the user instead."""
REFRESH_DELAY = timedelta(minutes=15)
"""Time from a change to its first refresh; a change can take ~12 min to arrive."""
REFRESH_REPEAT = timedelta(hours=1)
"""Time between the refreshes that follow one change."""
REFRESHES_PER_CHANGE = 2
"""Refreshes after each change: an inverter may miss more than one round."""
STEP_HOLD = timedelta(minutes=6)
"""How long the in-between value stays, so the ECU passes it on at least once.

The ECU talks to the inverters in rounds of about five minutes.
"""
OVERSHOOT_MARGIN = 0.05
OVERSHOOT_MARGIN_W = 20.0
"""An inverter may exceed its limit by 5 % of its power plus 20 W.

Measured on DS3-L: at 0 % they still deliver 15-31 W, at 20 % up to 17 W
more than the nominal limit.
"""
FACTORY_LIMIT = 30
FACTORY_FLAG_RAW = 1
MAX_OVERSHOOT_REFRESHES = 2
"""Refreshes for an inverter above the limit before the user is told instead."""
STORAGE_VERSION = 1


def store_for(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Return the store that keeps the chosen limit of one config entry."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.power_limit.{entry_id}")


def stored_choice(stored: dict[str, Any] | None) -> int | None:
    """Return the limit kept in the store, or None if there is none."""
    desired = (stored or {}).get("desired")
    if type(desired) is not int or not 0 <= desired <= NO_LIMIT:
        return None
    return desired


def issue_id_for(entry_id: str) -> str:
    """Return the repair issue id asking to check the installation power."""
    return f"check_installation_power_{entry_id}"


@dataclass(slots=True)
class _Refresh:
    """When and how the chosen limit is sent again."""

    next_at: datetime | None = None
    """When the next refresh is due; None when nothing is planned."""
    left: int = 0
    """Refreshes still to come for the current change."""
    step_written: datetime | None = None
    """When the in-between value of the refresh under way was written."""
    last_started: datetime | None = None


def effective_limit(measurements: Measurements) -> int | None:
    """Return the limit the ECU reports as one number, or None if unreadable.

    Enabled gives the percentage, disabled gives 100 whatever the percentage
    register holds, because a percentage without the flag does nothing.
    """
    enabled = measurements.export_limit_enabled
    percent = measurements.export_limit
    if enabled is None or percent is None or not 0 <= percent <= MAX_EXPORT_LIMIT:
        return None
    if not enabled:
        return NO_LIMIT
    return round(percent)


def factory_table(measurements: Measurements) -> bool:
    """Whether the ECU reports its factory table rather than a written limit."""
    return (
        measurements.export_limit == FACTORY_LIMIT
        and measurements.export_limit_enabled_raw == FACTORY_FLAG_RAW
    )


def _step_for(percent: int) -> int:
    """Return the in-between value for a refresh of ``percent``."""
    return percent - 1 if percent > 0 else 1


class PowerLimit:
    """Writes the limit, shows what the ECU reports and keeps the two aligned."""

    def __init__(
        self,
        coordinator: EcuCoordinator,
        control: Inverter,
        store: Store[dict[str, Any]],
    ) -> None:
        """Bind to the inverter whose unit carries the writes and the readback."""
        self._coordinator = coordinator
        self.control = control
        self._store = store
        self.desired: int | None = None
        """The limit the user chose; None until a choice is made or stored."""
        self.pending = False
        """Whether the chosen limit still has to reach the ECU: a write failed,
        or the limit was not read yet."""
        self.shown: int | None = None
        """The limit presented to the user."""
        self.deviations = 0
        """Polls in a row that read the same value other than ``shown``."""
        self.last_rewrite: datetime | None = None
        self.rewrites = 0
        """Corrective writes since the ECU last reported the chosen limit."""
        self.overshoot_refreshes = 0
        """Refreshes since the last change because an inverter was above the limit."""
        self._deviating: int | None = None
        self._refresh = _Refresh()
        self._last_written: datetime | None = None
        """When the chosen value last reached the ECU."""
        self._written: dict[int, float | bool] = {}
        """Per register the value written since the last read of ``control``."""
        self._over_last_poll: set[int] = set()
        self._ecu_was_silent = False
        self._limit_read = False
        """Whether the limit registers were read through ``control`` once."""
        self._lock = asyncio.Lock()

    @property
    def next_refresh(self) -> datetime | None:
        """When the chosen limit is sent again; None when nothing is planned."""
        return self._refresh.next_at

    @property
    def refreshing(self) -> bool:
        """Whether a refresh has written its in-between value."""
        return self._refresh.step_written is not None

    @property
    def installation_power(self) -> float | None:
        """Output of all inverters together in kW, if the user entered it."""
        power = self._coordinator.config_entry.options.get(CONF_INSTALLATION_POWER)
        return float(power) if power else None

    def _watched(self) -> bool:
        """Whether a missed limit shows in the power of the inverters.

        Then an inverter above the limit is refreshed at once, and no timed
        refresh is needed. A missed release to 100 shows as less power,
        which a cloud does too, so 100 is never watched.
        """
        return (
            self.installation_power is not None
            and self.desired is not None
            and self.desired < NO_LIMIT
        )

    async def async_load(self) -> None:
        """Take over the choice stored before Home Assistant (re)started.

        Below 100 the ECU may have restarted in the meantime and cleared the
        limit, so the choice is sent again at the first usable poll, unless
        the power shows a missed limit. At 100 there is nothing a restart can
        have undone.
        """
        desired = stored_choice(await self._store.async_load())
        if desired is None:
            return
        self.desired = desired
        if desired < NO_LIMIT:
            self._plan_refreshes(dt_util.utcnow(), delay=timedelta(0), count=1)

    def note_silent_ecu(self) -> None:
        """Record that the connection to the ECU broke.

        A restart of the ECU clears the limit on the inverters and breaks
        the open connection. The limit is then sent again once the ECU
        answers, unless the power shows a missed limit.
        """
        self._ecu_was_silent = True

    def note_read(self) -> None:
        """Record that the limit registers were just read through ``control``.

        From now on that read says what the ECU holds, not earlier writes.
        """
        self._written.clear()
        self._limit_read = True

    async def async_set(self, value: float) -> None:
        """Apply a limit chosen by the user.

        Remembers the choice first, then writes it. When several calls arrive
        at once, only the latest value is written. A failed write leaves the
        choice pending without an error, so an automation goes on.
        Before the limit was read once, the choice waits for that read.
        """
        if not math.isfinite(value):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="power_limit_invalid",
                translation_placeholders={"value": str(value)},
            )
        percent = min(max(round(value), 0), MAX_EXPORT_LIMIT)
        # The same value again changes nothing, unless the ECU does not show
        # it: then it is a request to try once more.
        if percent == self.desired and not self.pending and self.shown == percent:
            return
        self.desired = percent
        self._store.async_delay_save(self._stored, 0)
        async with self._lock:
            if self.desired != percent:
                return  # A later call took over.
            now = dt_util.utcnow()
            # A new choice gets fresh chances before the user is told.
            self._reset_overshoot()
            self.rewrites = 0
            if not self._limit_read:
                # Not read yet, so a write could repeat the flag. The first
                # healthy poll writes only what differs.
                self.pending = True
                _LOGGER.debug("Power limit %s %% is sent after the first read", percent)
                return
            try:
                await self._async_write(percent)
            except ModbusError as err:
                self.pending = True
                # A refresh under way would otherwise hold up the retry.
                self._refresh.step_written = None
                # The choice is kept and sent later, so no error that would
                # stop an automation.
                _LOGGER.warning(
                    "Power limit %s %% did not reach the ECU (%s); "
                    "it is written again once the ECU answers normally",
                    percent,
                    type(err).__name__,
                )
                return
            self.pending = False
            self._last_written = now
            self._plan_refreshes(now)

    async def _async_write(self, percent: int, *, show: bool = True) -> None:
        """Write the percentage, then the flag; a half write never limits more.

        Below 100 the percentage goes first and the flag switches it on; at
        100 the percentage is reset and the flag switched off. So a failed
        second write never limits more than asked; the next poll completes
        it. ``show`` makes the written value the shown one; the in-between
        value of a refresh is not shown. Nothing is written while the ECU may
        still work on an unanswered request: it would only queue up behind it.

        A register is only written when the ECU holds another value. Setting
        the flag again while it was on made the ECU stop answering altogether,
        and a change of the percentage alone needs no flag.
        """
        measurements = self.control.measurements
        await self._async_put(
            EXPORT_LIMIT_ADDRESS,
            percent,
            measurements.export_limit,
            self.control.async_set_export_limit,
        )
        await self._async_put(
            EXPORT_LIMIT_ENABLED_ADDRESS,
            percent < NO_LIMIT,
            measurements.export_limit_enabled,
            self.control.async_set_export_limit_enabled,
        )
        # What the inverters delivered before this write says nothing any more.
        self._over_last_poll = set()
        if show:
            self._adopt(percent)

    async def _async_put(
        self,
        address: int,
        value: float | bool,
        reported: float | bool | None,
        write: Callable[[Any], Awaitable[None]],
    ) -> None:
        """Write one register, unless the ECU holds ``value`` already.

        What the ECU holds is the last read, or a write made after it. An
        unknown value is always written. A write without an answer, or one
        given up on, may still have arrived, so it is not sent again before
        the next read.
        """
        if self._written.get(address, reported) == value:
            _LOGGER.debug(
                "Power limit register %s already holds %s, not written",
                address,
                int(value),
            )
            return
        async with self._coordinator.ready_link():
            _LOGGER.debug("Writing power limit register %s = %s", address, int(value))
            try:
                await write(value)
            except UNANSWERED:
                self._written[address] = value
                raise
        self._written[address] = value

    async def async_after_poll(self, data: EcuData) -> None:
        """Update the shown value and make the ECU follow the chosen one.

        Called by the coordinator once per poll, also a failed one, outside its
        ``write_lock``: after the reads, or before the inverters that went
        unanswered last time. Only this poll's reads count.
        """
        if self.control.unit_id not in data.available:
            return
        reported = effective_limit(self.control.measurements)
        if reported is None:
            _LOGGER.debug(
                "Unit %s reports an unreadable limit (%s, %s)",
                self.control.unit_id,
                self.control.measurements.export_limit,
                self.control.measurements.export_limit_enabled_raw,
            )
            return
        now = dt_util.utcnow()

        # Writes that can wait only go after a healthy poll.
        busy = not self._coordinator.poll_healthy
        if (written := self._refresh.step_written) is not None:
            # A refresh is under way and the ECU reports its in-between value.
            # Inverters that went unanswered before do not hold the finish up.
            if now - written >= STEP_HOLD and not busy:
                await self._async_finish_refresh(now)
            return

        self._follow(reported)
        if not busy:
            await self._async_keep_aligned(data, reported, now)

    def _follow(self, reported: int) -> None:
        """Update the shown value from the readback of one poll."""
        if reported == self.desired:
            self._adopt(reported)
            self.pending = False
            self.rewrites = 0
        elif self.shown is None or reported == self.shown:
            self._adopt(reported)
        elif reported == self._deviating:
            # Follow the ECU once two polls in a row report the same value.
            self.deviations += 1
            if self.deviations >= CONFIRM_POLLS:
                self.shown = reported
        else:
            self._deviating = reported
            self.deviations = 1

    async def _async_keep_aligned(
        self, data: EcuData, reported: int, now: datetime
    ) -> None:
        """Write whatever is needed to keep the ECU on the chosen limit."""
        if self._lock.locked():
            # A change of the user is being written. This poll read the value
            # before it, so the next poll judges again.
            return
        if self._ecu_was_silent:
            self._ecu_was_silent = False
            # A restart only ever clears limits, so at 100 there is nothing
            # to put back. At most one such refresh an hour, in case a
            # network drops the connection between every poll; a later one
            # is postponed, never dropped.
            if (
                self.desired is not None
                and self.desired < NO_LIMIT
                and self.installation_power is None
            ):
                started = self._refresh.last_started
                due = now if started is None else max(now, started + REFRESH_REPEAT)
                self._plan_refresh_by(due)
        if self.desired is None:
            return

        # Retry a failed write, or write again once the ECU has stably
        # reported another value. An ECU that keeps reporting another value
        # is not written over and over; a new choice tries again.
        if reported != self.desired and (self.pending or self.shown == reported):
            if self.rewrites >= MAX_REWRITES:
                return
            if not self._rewrote_recently(now):
                await self._async_rewrite(reported, now)
                return

        self._check_overshoot(data, now)

        # A pending choice goes through the rewrite above, not a refresh.
        refresh_due = self._refresh.next_at
        if self.pending or refresh_due is None or now < refresh_due:
            return
        # In the morning the weakest inverters wake up last. An inverter that
        # stays dark all day (a broken panel) must not hold the others up.
        # Judged on this cycle's reads only, never on older values.
        inverters = self._coordinator.inverters
        online = sum(inverters[unit].measurements.is_online for unit in data.available)
        if online == len(data.available) or (
            online > 0 and now >= refresh_due + REFRESH_REPEAT
        ):
            await self._async_start_refresh(now)

    def _check_overshoot(self, data: EcuData, now: datetime) -> None:
        """Refresh at once when an inverter clearly delivers above the limit.

        Only an excess is a sure sign: delivering less can be a cloud. Two
        polls in a row must show it, and the last write must be old enough to
        have arrived. The count of such refreshes starts over with a new
        choice, a restart and each night, so a wrong installation power costs
        at most two refreshes a day, not one every time the sun comes out.
        """
        inverters = self._coordinator.inverters
        if data.available and not any(
            inverters[unit].measurements.is_online for unit in data.available
        ):
            # An ECU that restarted overnight clears the limit on the
            # inverters without showing it, so each day gets fresh chances.
            self.overshoot_refreshes = 0
        if self.pending:
            return  # The chosen value has not reached the ECU yet.
        over = self._overshooting(data)
        confirmed = over & self._over_last_poll
        self._over_last_poll = over
        # Without a write since the start, the last write counts as long ago.
        written = self._last_written
        if not confirmed or (written is not None and now - written < REFRESH_DELAY):
            return
        due = self._refresh.next_at
        if due is not None and now >= due:
            return  # The planned refresh comes now anyway.
        if self.overshoot_refreshes >= MAX_OVERSHOOT_REFRESHES:
            self._report_overshoot(confirmed)
            return
        self.overshoot_refreshes += 1
        _LOGGER.debug("Units %s deliver above the limit; refreshing", confirmed)
        self._plan_refresh_by(now)

    def _overshooting(self, data: EcuData) -> set[int]:
        """Return the units delivering clearly more than the chosen limit."""
        total = self.installation_power
        if total is None or self.desired is None or self.desired >= NO_LIMIT:
            return set()
        per_inverter = total * 1000 / len(self._coordinator.inverters)
        allowed = per_inverter * (self.desired / 100 + OVERSHOOT_MARGIN)
        allowed += OVERSHOOT_MARGIN_W
        over = set()
        for unit_id in data.available:
            power = self._coordinator.inverters[unit_id].measurements.ac_power
            if power is not None and allowed < power <= MAX_INVERTER_POWER:
                over.add(unit_id)
        return over

    def _report_overshoot(self, units: set[int]) -> None:
        """Tell the user once that refreshing does not bring the power down."""
        entry = self._coordinator.config_entry
        issue_id = issue_id_for(entry.entry_id)
        hass = self._coordinator.hass
        if ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None:
            return
        _LOGGER.warning(
            "Units %s deliver above the power limit of %s %% after %s refreshes; "
            "check the installation power in the options",
            sorted(units),
            self.desired,
            MAX_OVERSHOOT_REFRESHES,
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="check_installation_power",
            translation_placeholders={"limit": str(self.desired)},
        )

    def _reset_overshoot(self) -> None:
        """Forget earlier refreshes for an excess, and any notice about it.

        The notice can outlive the count, which starts over each night.
        """
        self.overshoot_refreshes = 0
        ir.async_delete_issue(
            self._coordinator.hass,
            DOMAIN,
            issue_id_for(self._coordinator.config_entry.entry_id),
        )

    def _adopt(self, percent: int) -> None:
        """Show ``percent`` and start counting deviations from it afresh."""
        self.shown = percent
        self.deviations = 0
        self._deviating = None

    async def async_save(self) -> None:
        """Write the chosen limit now, for an unload or reload."""
        if self.desired is not None:
            await self._store.async_save(self._stored())

    def _stored(self) -> dict[str, Any]:
        return {"desired": self.desired}

    def _plan_refreshes(
        self,
        now: datetime,
        *,
        delay: timedelta = REFRESH_DELAY,
        count: int = REFRESHES_PER_CHANGE,
    ) -> None:
        """Plan ``count`` refreshes, the first one after ``delay``.

        Any refresh under way is dropped: the value just written takes over.
        Nothing is planned while the power shows a missed limit.
        """
        self._refresh.step_written = None
        if self._watched():
            self._refresh.next_at = None
            self._refresh.left = 0
            return
        self._refresh.next_at = now + delay
        self._refresh.left = count

    def _plan_refresh_by(self, due: datetime) -> None:
        """Make sure a refresh comes no later than ``due``."""
        if self._refresh.next_at is None or due < self._refresh.next_at:
            self._refresh.next_at = due
        self._refresh.left = max(self._refresh.left, 1)

    def _rewrote_recently(self, now: datetime) -> bool:
        """Whether a corrective write happened within the last interval."""
        return (
            self.last_rewrite is not None and now - self.last_rewrite < REWRITE_INTERVAL
        )

    async def _async_rewrite(self, reported: int, now: datetime) -> None:
        """Write the chosen limit again because the ECU reports another one.

        Only a refusal counts towards ``MAX_REWRITES``; an unanswered write
        means a busy ECU and is tried again after a healthy poll.
        """
        self.last_rewrite = now
        measurements = self.control.measurements
        factory = factory_table(measurements)
        flag_raw = measurements.export_limit_enabled_raw
        written = False
        async with self._lock:
            if (desired := self.desired) is None:
                return
            delivers_choice = self.pending
            try:
                await self._async_write(desired)
            except ModbusError as err:
                _LOGGER.debug(
                    "Rewriting power limit %s %% failed: %s",
                    desired,
                    type(err).__name__,
                )
                if isinstance(err, ModbusTimeoutError):
                    return
            else:
                written = True
                self.pending = False
                self._last_written = now
                if delivers_choice or not factory:
                    self._plan_refreshes(now)
            self.rewrites += 1
        if self.rewrites >= MAX_REWRITES:
            _LOGGER.warning(
                "The ECU keeps reporting a power limit of %s %% instead of %s %%; "
                "no more writes until the limit is chosen again",
                reported,
                desired,
            )
            return
        if not written:
            return
        _LOGGER.info(
            "The ECU reported a power limit of %s %% (flag register %s%s); "
            "set it back to %s %%",
            reported,
            flag_raw,
            ", its factory table" if factory else "",
            desired,
        )

    async def _async_start_refresh(self, now: datetime) -> None:
        """Write the in-between value, so the next write is a change again."""
        async with self._lock:
            # A change made while this poll waited for the lock planned its
            # own refreshes; check again.
            due = self._refresh.next_at
            if (
                (desired := self.desired) is None
                or self.pending
                or self._refresh.step_written is not None
                or due is None
                or now < due
            ):
                return
            step = _step_for(desired)
            try:
                await self._async_write(step, show=False)
            except ModbusError as err:
                # Nothing changed; the next usable poll tries again.
                _LOGGER.debug(
                    "Refreshing power limit %s %% failed: %s",
                    desired,
                    type(err).__name__,
                )
                return
            self._refresh.step_written = dt_util.utcnow()
            self._refresh.last_started = now
        _LOGGER.debug("Refreshing power limit %s %% via %s %%", desired, step)

    async def _async_finish_refresh(self, now: datetime) -> None:
        """Write the chosen value back after the in-between value."""
        async with self._lock:
            refresh = self._refresh
            if refresh.step_written is None or (desired := self.desired) is None:
                return
            try:
                await self._async_write(desired)
            except ModbusError as err:
                # The ECU stays on the in-between value until the next poll.
                _LOGGER.debug(
                    "Finishing power limit refresh failed: %s", type(err).__name__
                )
                return
            refresh.step_written = None
            self._last_written = now
            refresh.left -= 1
            if refresh.left > 0 and refresh.last_started is not None:
                refresh.next_at = refresh.last_started + REFRESH_REPEAT
            else:
                refresh.next_at = None
