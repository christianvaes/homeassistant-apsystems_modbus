"""Turn the output of all inverters off and on again through register 40188.

Measured on an ECU-R (firmware 1.3.23) with DS3 inverters:

* Writing 0 to Conn (40188) turns every inverter off within half a minute.
  The ECU keeps reporting 1, so only the power shows that they are off.
* The ECU turns them on again at its next round, about a minute after it
  starts, unless 0 was written again just before that round. Rounds start
  about 10 s past every fifth minute of the clock, so while the output is off
  0 is written on every fifth minute.
* The revert time in 40187 makes no difference and is never written.
* This ECU needs no 1 to turn them on again. Other ECUs keep 0 until 1 is
  written, so turning the output on writes 1 once.

* Now and then one inverter starts anyway at a round. The poll right after
  that round shows it delivering, so 0 is written again at once instead of
  waiting for the next fifth minute.

Writes that can wait go in ``async_after_poll`` after a healthy poll, like
those of the power limit. Turning off writes at once, because off only works
when it reaches the ECU before its round.

A write is skipped rather than queued while the ECU struggles: a late write
lands after the round, where it does nothing but keep a busy ECU busy. After
such a gap, and after a restart or the night, the output is turned off again
right after the next healthy poll, then on the clock as before.

Lock order: ``InverterOutput._lock`` first, then the coordinator's
``write_lock`` for the write itself, as for the power limit.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from time import monotonic
from typing import TYPE_CHECKING, Any

from modbus_connection import ModbusError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import sun
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MAX_INVERTER_POWER
from .ecu import GRID_CONNECTION_ADDRESS, Inverter

if TYPE_CHECKING:
    from .coordinator import EcuCoordinator, EcuData

_LOGGER = logging.getLogger(__name__)

WRITE_LATE = timedelta(seconds=5)
"""A clock write later than this would land in or after the ECU round."""
COVERED = timedelta(seconds=10)
"""A write this shortly before a clock write already covers that round."""
LEAK_POWER = 10.0
"""An inverter above this many W while off has started again; off ones read 0-3 W."""
STORAGE_VERSION = 1


def output_store_for(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Return the store that keeps the output choice of one config entry."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.inverter_output.{entry_id}")


def stored_off(stored: dict[str, Any] | None) -> bool:
    """Return whether the store says the output was turned off."""
    return (stored or {}).get("enabled") is False


class InverterOutput:
    """Keeps the inverters off while the user wants that, and on otherwise."""

    def __init__(
        self,
        coordinator: EcuCoordinator,
        control: Inverter,
        store: Store[dict[str, Any]],
    ) -> None:
        """Bind to the inverter whose unit carries the writes."""
        self._coordinator = coordinator
        self.control = control
        self._store = store
        self.enabled = True
        """The user's choice: True lets the inverters produce."""
        self.last_write: datetime | None = None
        """When the last write to Conn was answered."""
        self.last_result: str | None = None
        """How the last write ended: "ok" or the error type."""
        self.catch_up = False
        """An off write is due right after the next healthy poll."""
        self._lock = asyncio.Lock()
        self._unsub_clock: CALLBACK_TYPE | None = None

    async def async_load(self) -> None:
        """Take over the choice stored before Home Assistant (re)started."""
        if stored_off(await self._store.async_load()):
            self.enabled = False
            self.catch_up = True

    @callback
    def async_start(self, entry: ConfigEntry) -> None:
        """Follow the clock while off; stop with the entry."""
        entry.async_on_unload(self._stop_clock)
        if not self.enabled:
            self._start_clock()

    async def async_save(self) -> None:
        """Write the choice now, for an unload or reload."""
        await self._store.async_save(self._stored())

    async def async_set(self, enabled: bool) -> None:
        """Apply the user's choice.

        Off writes 0 at once, unless it is night or the ECU struggles; then
        the next healthy poll sends it, without an error that would stop an
        automation. On writes 1 once.
        """
        if enabled == self.enabled:
            return
        self.enabled = enabled
        self._store.async_delay_save(self._stored, 0)
        if enabled:
            self._stop_clock()
            self.catch_up = False
            await self._async_send_on()
            return
        self._start_clock()
        self.catch_up = True
        if not sun.is_up(self._coordinator.hass):
            _LOGGER.debug("Inverter output off at night: sent once it is light")
            return
        async with self._lock:
            if self.enabled:
                return  # Turned on again in the meantime.
            if not self._coordinator.poll_healthy:
                _LOGGER.warning(
                    "Inverter output off is sent once the ECU answers normally"
                )
                return
            try:
                await self._async_write(connected=False)
            except ModbusError as err:
                _LOGGER.warning(
                    "Inverter output off did not reach the ECU (%s); it is sent "
                    "again once the ECU answers normally",
                    type(err).__name__,
                )
                return
            self.catch_up = False

    async def _async_send_on(self) -> None:
        """Write 1 once; this ECU turns them on by itself if it fails."""
        async with self._lock:
            if not self.enabled:
                return  # Turned off again in the meantime.
            try:
                await self._async_write(connected=True)
            except ModbusError as err:
                _LOGGER.warning(
                    "Could not send inverter output on (%s). An ECU-R turns the "
                    "inverters on by itself at its next round",
                    type(err).__name__,
                )

    async def async_after_poll(self, data: EcuData) -> None:
        """Send off after a healthy poll if it is due or an inverter delivers."""
        healthy = self._coordinator.poll_healthy
        if self.enabled or not healthy or not sun.is_up(self._coordinator.hass):
            return
        if not self.catch_up:
            if not (delivering := self._delivering(data)):
                return
            _LOGGER.debug(
                "Inverter output: units %s deliver while off, writing off again",
                ", ".join(
                    f"{unit_id} ({power:.0f} W)" for unit_id, power in delivering
                ),
            )
            self.catch_up = True
        await self._async_send_off(due=None)

    def _delivering(self, data: EcuData) -> list[tuple[int, float]]:
        """Return the inverters read in this poll that deliver power."""
        inverters = self._coordinator.inverters
        return [
            (unit_id, power)
            for unit_id in sorted(data.available)
            if LEAK_POWER
            < (power := inverters[unit_id].measurements.ac_power or 0.0)
            <= MAX_INVERTER_POWER
        ]

    async def _async_clock(self, now: datetime) -> None:
        """Send off just before an ECU round.

        ``now`` is when Home Assistant ran this, which may be later than the
        fifth minute it was planned for; lateness counts from that minute.
        """
        due = dt_util.as_utc(now).replace(second=0, microsecond=0)
        await self._async_send_off(due=due - timedelta(minutes=due.minute % 5))

    async def _async_send_off(self, due: datetime | None) -> None:
        """Write 0 once, or skip it and catch up after the next healthy poll.

        ``due`` is the clock moment of a write before a round; None means
        right away after a poll.
        """
        async with self._lock:
            if self.enabled:
                return
            if due is None and not self.catch_up:
                return  # Another write went first.
            if (
                due is not None
                and self.last_write is not None
                and timedelta(0) <= due - self.last_write <= COVERED
            ):
                _LOGGER.debug("Inverter output: off sent just before, not again")
                return
            reason = None
            if not sun.is_up(self._coordinator.hass):
                reason = "night"
            elif not self._coordinator.poll_healthy:
                reason = "the last poll failed or was slow"
            if reason is not None:
                _LOGGER.debug("Inverter output: off not sent (%s)", reason)
                self.catch_up = True
                return
            try:
                written = await self._async_write(connected=False, due=due)
            except ModbusError as err:
                _LOGGER.debug(
                    "Inverter output: writing off failed: %s", type(err).__name__
                )
                self.catch_up = True
                return
            self.catch_up = not written

    async def _async_write(
        self, *, connected: bool, due: datetime | None = None
    ) -> bool:
        """Write Conn on a free link; False when it came too late to write."""
        async with self._coordinator.ready_link():
            if due is not None and dt_util.utcnow() - due > WRITE_LATE:
                _LOGGER.debug("Inverter output: too late for the ECU round, skipped")
                return False
            value = int(connected)
            _LOGGER.debug(
                "Writing grid connection register %s = %s",
                GRID_CONNECTION_ADDRESS,
                value,
            )
            started = monotonic()
            try:
                await self.control.async_set_grid_connection(connected)
            except ModbusError as err:
                self.last_result = type(err).__name__
                raise
            self.last_write = dt_util.utcnow()
            self.last_result = "ok"
            _LOGGER.debug(
                "Grid connection register answered after %.1f s",
                monotonic() - started,
            )
        return True

    def _start_clock(self) -> None:
        """Write on every fifth minute of the clock, just before each round."""
        if self._unsub_clock is None:
            self._unsub_clock = async_track_time_change(
                self._coordinator.hass, self._async_clock, minute="/5", second=0
            )

    @callback
    def _stop_clock(self) -> None:
        if self._unsub_clock is not None:
            self._unsub_clock()
            self._unsub_clock = None

    def _stored(self) -> dict[str, Any]:
        return {"enabled": self.enabled}
