"""Config flow: find the ECU, find its inverters, confirm."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
import logging
import re
from typing import Any

from modbus_connection import (
    ModbusConnectionError,
    ModbusError,
    ModbusExceptionError,
    ModbusTcpParams,
    ModbusTimeoutError,
    ModbusUnit,
)
import probatio

from homeassistant.components.modbus import async_get_temporary_unit
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    config_validation as cv,
    issue_registry as ir,
    selector,
)
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from .const import (
    CONF_INSTALLATION_POWER,
    CONF_INVERTERS,
    CONF_LIFETIME_ENERGY,
    CONF_MODEL,
    CONF_PEAK_POWER,
    CONF_SERIAL_NUMBER,
    CONF_UNIT_ID,
    CONF_UNIT_IDS,
    DEFAULT_PORT,
    DOMAIN,
    MAX_POWER_PER_INVERTER,
    MIN_POWER_PER_INVERTER,
)
from .coordinator import new_address_issue_id
from .ecu import (
    SUNSPEC_MARKER,
    Identity,
    UnsupportedLayoutError,
    async_check_controls,
)
from .lifetime import async_request_start, async_stored_lifetime

_LOGGER = logging.getLogger(__name__)

PROBE_TIMEOUT = 5.0
"""Seconds to wait while searching; an address without an inverter never answers."""
ATTEMPTS = 2
"""Tries for an address that may be empty; an empty one never answers."""
READ_ATTEMPTS = 3
"""Tries for reads of an inverter that answered: the ECU sometimes needs more
than one probe timeout while it polls its inverters. The timeout itself stays
the same during the search, so the connection is never rebuilt halfway."""
SCAN_START = 1
SCAN_LIMIT = 100
"""Highest address searched: an ECU-R serves up to 100 inverters."""
SCAN_STOP_AFTER_MISSES = 2
MIN_UNIT_ID = 1
MAX_UNIT_ID = 247

_RANGE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")

_KWH_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0,
        step="any",
        unit_of_measurement="kWh",
        mode=selector.NumberSelectorMode.BOX,
    )
)
_WATT_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0, step=1, unit_of_measurement="W", mode=selector.NumberSelectorMode.BOX
    )
)
_KW_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0, step=0.1, unit_of_measurement="kW", mode=selector.NumberSelectorMode.BOX
    )
)
DS3_L_KW = 0.73
DS3_KW = 0.88
"""Output of a DS3-L and a DS3. The ECU reports both as DS3."""
COMMA_LANGUAGES = ("nl", "de", "fr")
"""Languages of the shipped translations that write 5,8 instead of 5.8."""


def _installation_power_error(kw: float | None, inverters: int) -> str | None:
    """Return an error key when the power does not fit the number of inverters.

    Catches the most likely mistake, watts typed into the kW field.
    """
    if kw is None or not inverters:
        return None
    if MIN_POWER_PER_INVERTER <= kw / inverters <= MAX_POWER_PER_INVERTER:
        return None
    return "invalid_installation_power"


def _power_placeholders(hass: HomeAssistant, inverters: int) -> dict[str, str]:
    """Return the example totals for both DS3 types.

    Home Assistant only uses a translation with the same placeholders as the
    English text, so the decimal mark is chosen here, from its language.
    """
    mark = "," if hass.config.language.startswith(COMMA_LANGUAGES) else "."
    return {
        "count": str(inverters),
        "ds3_l_kw": f"{inverters * DS3_L_KW:.1f}".replace(".", mark),
        "ds3_kw": f"{inverters * DS3_KW:.1f}".replace(".", mark),
    }


@dataclass(frozen=True)
class FoundInverter:
    """An inverter answering on one Modbus address."""

    unit_id: int
    serial_number: str
    model: str

    def as_config(self) -> dict[str, Any]:
        """Return the shape stored in the config entry."""
        return {
            CONF_UNIT_ID: self.unit_id,
            CONF_SERIAL_NUMBER: self.serial_number,
            CONF_MODEL: self.model,
        }


class ProbeError(Exception):
    """The ECU could not be set up; ``reason`` is a translated error key."""

    def __init__(self, reason: str) -> None:
        """Remember why probing failed."""
        super().__init__(reason)
        self.reason = reason


def parse_unit_ids(text: str) -> list[int]:
    """Turn ``1-8`` or ``1, 2, 5`` into a sorted list of unit ids."""
    unit_ids: set[int] = set()
    for part in text.split(","):
        match = _RANGE.match(part)
        if not match:
            raise ValueError(part)
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if not MIN_UNIT_ID <= start <= end <= MAX_UNIT_ID:
            raise ValueError(part)
        unit_ids.update(range(start, end + 1))
    if not unit_ids:
        raise ValueError(text)
    return sorted(unit_ids)


def _is_refused(err: BaseException) -> bool:
    """Whether a connection error was an active refusal (Modbus switched off)."""
    cause: BaseException | None = err
    while cause is not None:
        if isinstance(cause, ConnectionRefusedError) or "refused" in str(cause).lower():
            return True
        cause = cause.__cause__ or cause.__context__
    return False


async def _marker(unit: ModbusUnit) -> str | None:
    """Return the text at 40000-40001, or ``None`` if the address stays silent.

    An address without an inverter never answers on the ECU; a SunSpec device
    answers ``"SunS"``.
    """
    for attempt in range(ATTEMPTS):
        try:
            values = await unit.read_holding_registers(40000, 2)
            break
        except ModbusExceptionError:
            return None
        except ModbusTimeoutError:
            if attempt == ATTEMPTS - 1:
                return None
            _LOGGER.debug("No answer on 40000, trying once more")
    return b"".join(value.to_bytes(2, "big") for value in values).decode(
        "ascii", errors="replace"
    )


async def _patiently[T](read: Callable[[], Awaitable[T]], what: str) -> T:
    """Run a read, trying again while it times out."""
    for _ in range(READ_ATTEMPTS - 1):
        try:
            return await read()
        except ModbusTimeoutError:
            _LOGGER.debug("%s: no answer yet, trying again", what)
    return await read()


def _as_found(identity: Identity, unit_id: int) -> FoundInverter:
    """Check the layout of an identity read and turn it into a found inverter."""
    identity.check_layout()
    return FoundInverter(
        unit_id=unit_id,
        serial_number=identity.serial_number or "",
        model=identity.model or "",
    )


async def _read_identity(unit: ModbusUnit, unit_id: int) -> FoundInverter:
    """Read model 1 of an address the user gave, patiently."""
    identity = Identity(unit)
    await _patiently(identity.async_update, f"Unit {unit_id}")
    return _as_found(identity, unit_id)


async def _try_identity(unit: ModbusUnit, attempts: int) -> Identity | None:
    """Read model 1 of an address that may be empty; ``None`` if it stays silent.

    One read both tells whether an inverter is there and what it is.
    """
    identity = Identity(unit)
    for attempt in range(attempts):
        try:
            await identity.async_update()
        except ModbusExceptionError:
            return None
        except ModbusTimeoutError:
            if attempt < attempts - 1:
                _LOGGER.debug("No answer, trying once more")
            continue
        return identity
    return None


class ApsystemsModbusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Two screens: connection, then confirmation of what was found."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with nothing known."""
        self._host = ""
        self._port = DEFAULT_PORT
        self._unit_ids: list[int] | None = None
        self._search_until = 0
        """Highest address the search always reaches, past silent ones."""
        self._mac: str | None = None
        self._found: list[FoundInverter] = []
        self._probe_task: asyncio.Task[list[FoundInverter]] | None = None
        self._probe_error: str | None = None
        self._show_confirm = False
        self._stored_lifetime: float | None = None
        """Lifetime energy in kWh kept from an earlier setup of this ECU."""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ApsystemsOptionsFlow:
        """Return the options flow."""
        return ApsystemsOptionsFlow()

    # ---- entry points ----------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the connection details."""
        errors: dict[str, str] = {}
        if user_input is not None and self._take_connection_input(user_input, errors):
            self._async_abort_entries_match(
                {CONF_HOST: self._host, CONF_PORT: self._port}
            )
            return await self.async_step_scan()
        return self.async_show_form(
            step_id="user",
            # Home Assistant re-runs this step without input when the dialog is
            # reopened, so fall back to what is already known.
            data_schema=self._connection_schema(user_input or self._known_connection()),
            errors=errors,
        )

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Offer an ECU that appeared on the network, once it proves to be one."""
        mac = format_mac(discovery_info.macaddress)
        for entry in self._async_current_entries(include_ignore=True):
            if (
                entry.data.get(CONF_MAC) == mac
                or entry.data.get(CONF_HOST) == discovery_info.ip
            ):
                if entry.data.get(CONF_HOST) != discovery_info.ip:
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, CONF_HOST: discovery_info.ip}
                    )
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()

        self._host = discovery_info.ip
        self._mac = mac
        params = ModbusTcpParams(host=self._host, port=self._port)
        try:
            async with async_get_temporary_unit(self.hass, params, SCAN_START) as unit:
                unit.require_timeout(PROBE_TIMEOUT)
                marker = await _marker(unit)
        except ModbusError:
            return self.async_abort(reason="cannot_connect")
        if marker is None:
            return self.async_abort(reason="cannot_connect")
        if marker != SUNSPEC_MARKER:
            return self.async_abort(reason="not_apsystems")

        self.context["title_placeholders"] = {CONF_HOST: self._host}
        return await self.async_step_dhcp_confirm()

    async def async_step_dhcp_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user accept the discovered ECU before anything is searched."""
        if user_input is not None:
            return await self.async_step_scan()
        return self.async_show_form(
            step_id="dhcp_confirm",
            description_placeholders={CONF_HOST: self._host},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change host, port or addresses of an existing entry."""
        entry = self._get_reconfigure_entry()
        # Do not miss a known inverter behind a gap of silent addresses.
        self._search_until = max(
            item[CONF_UNIT_ID] for item in entry.data[CONF_INVERTERS]
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            if self._take_connection_input(user_input, errors):
                return await self.async_step_scan()
            current: Mapping[str, Any] = user_input
        else:
            # After an address change in EMA Manager the old addresses are
            # wrong, so the search starts afresh.
            moved = ir.async_get(self.hass).async_get_issue(
                DOMAIN, new_address_issue_id(entry.entry_id)
            )
            current = {
                CONF_HOST: entry.data[CONF_HOST],
                CONF_PORT: entry.data[CONF_PORT],
                CONF_UNIT_IDS: ""
                if moved is not None
                else ", ".join(
                    str(item[CONF_UNIT_ID]) for item in entry.data[CONF_INVERTERS]
                ),
            }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._connection_schema(current),
            errors=errors,
        )

    def _take_connection_input(
        self, user_input: Mapping[str, Any], errors: dict[str, str]
    ) -> bool:
        """Store the connection form; ``False`` with an error if it is invalid."""
        self._host = user_input[CONF_HOST].strip()
        self._port = user_input[CONF_PORT]
        text = user_input.get(CONF_UNIT_IDS, "").strip()
        try:
            self._unit_ids = parse_unit_ids(text) if text else None
        except ValueError:
            errors[CONF_UNIT_IDS] = "invalid_unit_ids"
            return False
        return True

    # ---- probing ---------------------------------------------------------

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Find the inverters while showing progress, then move on."""
        if self._probe_task is None:
            self._probe_error = None
            self._probe_task = self.hass.async_create_task(self._async_probe())
        if not self._probe_task.done():
            return self.async_show_progress(
                step_id="scan",
                progress_action="scan",
                progress_task=self._probe_task,
            )
        try:
            self._found = self._probe_task.result()
        except ProbeError as err:
            self._probe_error = err.reason
        except Exception:
            _LOGGER.exception("Unexpected error while searching for inverters")
            self._probe_error = "unknown"
        finally:
            self._probe_task = None
        if self._probe_error:
            return self.async_show_progress_done(next_step_id="retry")
        # Home Assistant re-sends the previous step's input to the next step
        # after progress; the confirm form must be shown first regardless.
        self._show_confirm = True
        return self.async_show_progress_done(next_step_id="confirm")

    async def async_step_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show why probing failed on the screen the search started from."""
        errors = {"base": self._probe_error or "unknown"}
        self._probe_error = None
        # Also a discovered ECU gets the form with the address field, so the
        # addresses can be entered when the search finds nothing.
        step_id = "reconfigure" if self.source == SOURCE_RECONFIGURE else "user"
        return self.async_show_form(
            step_id=step_id,
            data_schema=self._connection_schema(self._known_connection()),
            errors=errors,
        )

    def _known_connection(self) -> dict[str, Any]:
        """Return the connection details entered or discovered so far."""
        return {
            CONF_HOST: self._host,
            CONF_PORT: self._port,
            CONF_UNIT_IDS: ", ".join(str(unit_id) for unit_id in self._unit_ids or []),
        }

    async def _async_probe(self) -> list[FoundInverter]:
        """Search the given or default addresses and read each inverter's identity."""
        params = ModbusTcpParams(host=self._host, port=self._port)
        try:
            # Hold the first address for the whole search so the connection
            # stays open; the other addresses share it.
            async with AsyncExitStack() as stack:
                first = (self._unit_ids or [SCAN_START])[0]
                anchor = await stack.enter_async_context(
                    async_get_temporary_unit(self.hass, params, first)
                )
                anchor.require_timeout(PROBE_TIMEOUT)
                if self._unit_ids is None:
                    answered = await self._scan(stack, params, anchor, first)
                else:
                    answered = await self._read_listed(stack, params, anchor, first)
                if answered:
                    # All units share one layout: check the controls block once.
                    unit = answered[0][1]
                    await _patiently(
                        lambda: async_check_controls(unit), "Controls block"
                    )
        except UnsupportedLayoutError as err:
            _LOGGER.debug("Search stopped: %s", err)
            raise ProbeError("unsupported_layout") from err
        except ModbusConnectionError as err:
            _LOGGER.debug(
                "Search stopped by a connection error: %s", type(err).__name__
            )
            reason = "modbus_disabled" if _is_refused(err) else "cannot_connect"
            raise ProbeError(reason) from err
        except ModbusTimeoutError as err:
            _LOGGER.debug("Search stopped: no answer from the ECU")
            raise ProbeError("cannot_connect") from err
        if not answered:
            raise ProbeError("no_inverters")
        return [item for item, _ in answered]

    async def _unit(
        self,
        stack: AsyncExitStack,
        params: ModbusTcpParams,
        anchor: ModbusUnit,
        first: int,
        unit_id: int,
    ) -> ModbusUnit:
        """Return the unit for an address; the first one is the anchor."""
        if unit_id == first:
            return anchor
        unit = await stack.enter_async_context(
            async_get_temporary_unit(self.hass, params, unit_id)
        )
        unit.require_timeout(PROBE_TIMEOUT)
        return unit

    async def _read_listed(
        self,
        stack: AsyncExitStack,
        params: ModbusTcpParams,
        anchor: ModbusUnit,
        first: int,
    ) -> list[tuple[FoundInverter, ModbusUnit]]:
        """Read the identity of each address the user gave."""
        answered: list[tuple[FoundInverter, ModbusUnit]] = []
        silent = 0
        for unit_id in self._unit_ids or [first]:
            unit = await self._unit(stack, params, anchor, first, unit_id)
            try:
                answered.append((await _read_identity(unit, unit_id), unit))
            except ModbusTimeoutError:
                _LOGGER.debug("Unit %s: no answer to the identity read", unit_id)
                if answered:
                    raise ProbeError("ecu_busy") from None
                silent += 1
                if silent >= SCAN_STOP_AFTER_MISSES:
                    # Do not wait out a long list on a silent ECU.
                    raise ProbeError("no_inverters") from None
        return answered

    async def _scan(
        self,
        stack: AsyncExitStack,
        params: ModbusTcpParams,
        anchor: ModbusUnit,
        first: int,
    ) -> list[tuple[FoundInverter, ModbusUnit]]:
        """Walk the addresses from 1 until two in a row stay silent.

        Each address gets one identity read, which both finds an inverter and
        tells what it is. The first silent address after an answer gets two
        tries, so a slow inverter is not missed; the next one gets one try,
        which keeps the end of the search short. Silent addresses between
        inverters are tried once more when a later address answers.
        """
        answered: dict[int, tuple[FoundInverter, ModbusUnit]] = {}
        silent: dict[int, ModbusUnit] = {}
        for unit_id in range(first, SCAN_LIMIT + 1):
            unit = await self._unit(stack, params, anchor, first, unit_id)
            identity = await _try_identity(unit, attempts=1 if silent else ATTEMPTS)
            _LOGGER.debug(
                "Address %s: %s",
                unit_id,
                identity.marker if identity is not None else "no answer",
            )
            if identity is not None and identity.marker == SUNSPEC_MARKER:
                for gap_id, gap_unit in silent.items():
                    gap = await _try_identity(gap_unit, attempts=1)
                    if gap is not None and gap.marker == SUNSPEC_MARKER:
                        answered[gap_id] = (_as_found(gap, gap_id), gap_unit)
                silent.clear()
                answered[unit_id] = (_as_found(identity, unit_id), unit)
                continue
            if identity is not None and not answered:
                # Something answers, but it is not a SunSpec device.
                raise ProbeError("not_apsystems")
            silent[unit_id] = unit
            if len(silent) >= SCAN_STOP_AFTER_MISSES and unit_id >= self._search_until:
                if not answered:
                    # Nothing on the first addresses: the ECU is reachable but
                    # no inverter has an address yet.
                    raise ProbeError("no_inverters")
                break
        return [answered[unit_id] for unit_id in sorted(answered)]

    # ---- confirmation ----------------------------------------------------

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show what was found and ask for the optional ECU id."""
        data: dict[str, Any] = {
            CONF_HOST: self._host,
            CONF_PORT: self._port,
            CONF_INVERTERS: [item.as_config() for item in self._found],
        }
        if self._mac:
            data[CONF_MAC] = self._mac

        if self.source == SOURCE_RECONFIGURE:
            entry = self._get_reconfigure_entry()
            known = {item[CONF_SERIAL_NUMBER] for item in entry.data[CONF_INVERTERS]}
            if not known & {item.serial_number for item in self._found}:
                return self.async_abort(reason="different_ecu")
            if user_input is not None and not self._show_confirm:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, **data}
                )
            # Confirming removes these devices and their history, so say so.
            missing = sorted(known - {item.serial_number for item in self._found})
            return self._show_found(
                probatio.Schema({}),
                {"base": "missing_inverters"} if missing else None,
                ", ".join(missing),
            )

        errors: dict[str, str] = {}
        if user_input is not None and not self._show_confirm:
            if error := _installation_power_error(
                user_input.get(CONF_INSTALLATION_POWER), len(self._found)
            ):
                errors[CONF_INSTALLATION_POWER] = error
            else:
                return await self._async_create(data, user_input)
        return await self._show_confirm_form(user_input, errors)

    async def _async_create(
        self, data: dict[str, Any], user_input: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Create the entry; the ECU is identified by its first inverter."""
        unique_id = self._found[0].serial_number
        await self.async_set_unique_id(unique_id, raise_on_progress=False)
        # A known ECU on a new address: follow it instead of adding it twice.
        moved = {CONF_HOST: self._host, CONF_PORT: self._port}
        if self._mac:
            moved[CONF_MAC] = self._mac
        self._abort_if_unique_id_configured(updates=moved)
        await self._apply_start_value(unique_id, user_input.get(CONF_LIFETIME_ENERGY))
        options = {}
        if (power := user_input.get(CONF_INSTALLATION_POWER)) is not None:
            options[CONF_INSTALLATION_POWER] = round(power, 1)
        return self.async_create_entry(
            title="APsystems ECU", data=data, options=options
        )

    async def _show_confirm_form(
        self, user_input: Mapping[str, Any] | None, errors: dict[str, str]
    ) -> ConfigFlowResult:
        """Show what was found and ask for the power and lifetime energy."""
        if self._stored_lifetime is None:
            stored = await async_stored_lifetime(
                self.hass, self._found[0].serial_number
            )
            self._stored_lifetime = None if stored is None else round(stored / 1000, 1)
        schema = self.add_suggested_values_to_schema(
            probatio.Schema(
                {
                    probatio.Optional(CONF_INSTALLATION_POWER): _KW_SELECTOR,
                    probatio.Optional(CONF_LIFETIME_ENERGY): _KWH_SELECTOR,
                }
            ),
            {CONF_LIFETIME_ENERGY: self._stored_lifetime}
            if user_input is None or self._show_confirm
            else dict(user_input),
        )
        return self._show_found(schema, errors)

    def _show_found(
        self,
        schema: probatio.Schema,
        errors: dict[str, str] | None = None,
        missing: str = "",
    ) -> ConfigFlowResult:
        # Table rows in the order and columns of EMA Manager (ID, address),
        # plus the model the ECU reports.
        rows = "\n".join(
            f"| {item.serial_number} | {item.unit_id} | {item.model} |"
            for item in self._found
        )
        self._show_confirm = False
        return self.async_show_form(
            step_id="confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "inverters": rows,
                "missing": missing,
                **_power_placeholders(self.hass, len(self._found)),
            },
        )

    async def _apply_start_value(self, ecu_key: str, kwh: float | None) -> None:
        """Pass the energy produced so far on to the first poll.

        Unchanged from what was kept: nothing to do. Cleared while a stored
        value was shown: start counting over. Otherwise: start at the value.
        """
        if kwh is None:
            if self._stored_lifetime is not None:
                await async_request_start(self.hass, ecu_key, None)
            return
        if (
            self._stored_lifetime is not None
            and abs(kwh - self._stored_lifetime) < 0.001
        ):
            return
        await async_request_start(self.hass, ecu_key, kwh * 1000)

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _connection_schema(defaults: Mapping[str, Any]) -> probatio.Schema:
        return probatio.Schema(
            {
                probatio.Required(
                    CONF_HOST, default=defaults.get(CONF_HOST, "")
                ): cv.string,
                probatio.Required(
                    CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)
                ): probatio.Port(),
                probatio.Optional(
                    CONF_UNIT_IDS, default=defaults.get(CONF_UNIT_IDS, "")
                ): cv.string,
            }
        )


class ApsystemsOptionsFlow(OptionsFlowWithReload):
    """Installation power, plus corrections of lifetime energy and peak power.

    The energy and power fields are not options: they correct the stored totals
    once and are shown with the current values, so leaving them alone changes
    nothing.
    """

    def __init__(self) -> None:
        """Start without shown values."""
        self._shown: dict[str, float] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the options."""
        loaded = self.config_entry.state is ConfigEntryState.LOADED
        inverters = len(self.config_entry.data[CONF_INVERTERS])
        errors: dict[str, str] = {}
        if user_input is not None:
            power = user_input.get(CONF_INSTALLATION_POWER)
            if error := _installation_power_error(power, inverters):
                errors[CONF_INSTALLATION_POWER] = error
            else:
                if loaded:
                    await self._apply_corrections(user_input)
                options: dict[str, Any] = {}
                if power is not None:
                    options[CONF_INSTALLATION_POWER] = round(power, 1)
                return self.async_create_entry(data=options)
        fields: dict[Any, Any] = {
            probatio.Optional(CONF_INSTALLATION_POWER): _KW_SELECTOR,
        }
        if loaded:
            ledger = self.config_entry.runtime_data.ledger
            self._shown = {
                CONF_LIFETIME_ENERGY: round(ledger.lifetime / 1000, 1),
                CONF_PEAK_POWER: round(ledger.peak_power),
            }
            fields[probatio.Optional(CONF_LIFETIME_ENERGY)] = _KWH_SELECTOR
            fields[probatio.Optional(CONF_PEAK_POWER)] = _WATT_SELECTOR
        suggested = (
            dict(user_input)
            if user_input is not None
            else {
                **self._shown,
                CONF_INSTALLATION_POWER: self.config_entry.options.get(
                    CONF_INSTALLATION_POWER
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                probatio.Schema(fields), suggested
            ),
            errors=errors,
            description_placeholders=_power_placeholders(self.hass, inverters),
        )

    async def _apply_corrections(self, user_input: Mapping[str, Any]) -> None:
        """Correct the stored totals where the user changed a shown value."""
        ledger = self.config_entry.runtime_data.ledger
        kwh = user_input.get(CONF_LIFETIME_ENERGY)
        if kwh is not None and abs(kwh - self._shown[CONF_LIFETIME_ENERGY]) >= 0.001:
            ledger.calibrate(kwh * 1000)
        watts = user_input.get(CONF_PEAK_POWER)
        if watts is not None and watts != self._shown[CONF_PEAK_POWER]:
            ledger.set_peak_power(watts)
        await ledger.async_save()
        # Show the corrected values now, also when nothing else changed.
        self.config_entry.runtime_data.async_update_listeners()
