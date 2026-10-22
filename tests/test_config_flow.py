"""Tests for the config, discovery, reconfigure and options flows."""

from __future__ import annotations

from unittest.mock import patch

from modbus_connection import ModbusConnectionError, ModbusTimeoutError
from modbus_connection.mock import MockModbusConnection
import pytest

from homeassistant.config_entries import SOURCE_DHCP, SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.apsystems_modbus.config_flow import parse_unit_ids
from custom_components.apsystems_modbus.const import (
    CONF_INVERTERS,
    CONF_MODEL,
    CONF_SERIAL_NUMBER,
    CONF_UNIT_ID,
    CONF_UNIT_IDS,
    DOMAIN,
)
from custom_components.apsystems_modbus.coordinator import new_address_issue_id
from custom_components.apsystems_modbus.lifetime import (
    EnergyLedger,
    async_request_start,
)

from .conftest import INVERTERS, add_inverter

HOST = "192.0.2.10"
USER_INPUT = {CONF_HOST: HOST, CONF_PORT: 502, CONF_UNIT_IDS: ""}


async def start_user_flow(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def submit_and_wait(hass: HomeAssistant, flow_id: str, user_input: dict) -> dict:
    """Submit a form that starts the search and return the step after it."""
    result = await hass.config_entries.flow.async_configure(flow_id, user_input)
    if result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done(wait_background_tasks=True)
        result = await hass.config_entries.flow.async_configure(flow_id)
    return result


async def test_user_flow_with_search(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["count"] == "2"
    # One table row per inverter: ID, address, model, as in EMA Manager.
    assert result["description_placeholders"]["inverters"].splitlines() == [
        "| 704000000001 | 1 | DS3 |",
        "| 703000000002 | 2 | DS3 |",
    ]

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "APsystems ECU"
    assert result["result"].unique_id == "704000000001"
    assert result["data"] == {
        CONF_HOST: HOST,
        CONF_PORT: 502,
        CONF_INVERTERS: INVERTERS,
    }
    assert result["options"] == {}


async def test_user_flow_with_addresses(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    result = await start_user_flow(hass)

    result = await submit_and_wait(
        hass, result["flow_id"], {**USER_INPUT, CONF_UNIT_IDS: "2"}
    )
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["count"] == "1"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "APsystems ECU"
    # The first inverter identifies the ECU.
    assert result["result"].unique_id == "703000000002"
    assert [item["unit_id"] for item in result["data"][CONF_INVERTERS]] == [2]


async def finish(hass: HomeAssistant, flow_id: str, user_input: dict) -> None:
    """Submit the form again now that the cause is fixed and create the entry."""
    result = await submit_and_wait(hass, flow_id, user_input)
    assert result["step_id"] == "confirm", result
    result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_invalid_addresses(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    result = await start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_UNIT_IDS: "1-300"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_UNIT_IDS: "invalid_unit_ids"}

    await finish(hass, result["flow_id"], USER_INPUT)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ModbusConnectionError("Connection refused"), "modbus_disabled"),
        (ModbusConnectionError("No route to host"), "cannot_connect"),
    ],
)
async def test_connection_errors(
    hass: HomeAssistant, mock_ecu: MockModbusConnection, error: Exception, expected: str
) -> None:
    mock_ecu.for_unit(1).fail_requests(error)
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected}

    # The user plugs in the cable or switches Modbus on and tries again.
    mock_ecu.for_unit(1).fail_requests(None)
    await finish(hass, result["flow_id"], USER_INPUT)


async def test_silent_ecu_with_addresses(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    mock_ecu.for_unit(1).fail_requests(ModbusTimeoutError())
    result = await start_user_flow(hass)

    result = await submit_and_wait(
        hass, result["flow_id"], {**USER_INPUT, CONF_UNIT_IDS: "1"}
    )
    # The ECU answered the connection but nothing lives on that address.
    assert result["errors"] == {"base": "no_inverters"}

    mock_ecu.for_unit(1).fail_requests(None)
    await finish(hass, result["flow_id"], {**USER_INPUT, CONF_UNIT_IDS: "1"})


async def test_no_inverters_found(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    # The ECU is reachable, but no address answers.
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "no_inverters"}

    # Addresses assigned in EMA Manager: the search finds them.
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(None)
    await finish(hass, result["flow_id"], USER_INPUT)


async def test_unsupported_layout(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    mock_ecu.for_unit(1).holding[40070] = 103
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "unsupported_layout"}

    mock_ecu.for_unit(1).holding[40070] = 101
    await finish(hass, result["flow_id"], USER_INPUT)


async def test_not_apsystems(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    mock_ecu.for_unit(1).holding[40000] = 0x1234
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "not_apsystems"}

    # The right address this time.
    mock_ecu.for_unit(1).holding[40000] = 21365
    await finish(hass, result["flow_id"], USER_INPUT)


async def test_unexpected_error_during_search(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    result = await start_user_flow(hass)
    with patch(
        "custom_components.apsystems_modbus.config_flow.ApsystemsModbusConfigFlow._async_probe",
        side_effect=RuntimeError("boom"),
    ):
        result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "unknown"}

    await finish(hass, result["flow_id"], USER_INPUT)


async def test_dhcp_discovery_of_a_silent_device_is_aborted(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    mock_ecu.for_unit(1).fail_requests(ModbusTimeoutError())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DISCOVERY
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_already_configured(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


DISCOVERY = DhcpServiceInfo(ip=HOST, hostname="", macaddress="80971b000001")


async def test_dhcp_discovery(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DISCOVERY
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "dhcp_confirm"

    result = await submit_and_wait(hass, result["flow_id"], {})
    assert result["step_id"] == "confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == HOST
    assert result["data"][CONF_MAC] == "80:97:1b:00:00:01"
    assert result["result"].unique_id == "704000000001"


async def test_dhcp_updates_host_of_known_ecu(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="704000000001",
        data={
            CONF_HOST: "192.0.2.99",
            CONF_PORT: 502,
            CONF_MAC: "80:97:1b:00:00:01",
            CONF_INVERTERS: INVERTERS,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DISCOVERY
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == HOST
    # The entry reloads on the new address; let it settle and stop again.
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("silent", "reason"), [(True, "cannot_connect"), (False, "not_apsystems")]
)
async def test_dhcp_ignores_devices_that_are_not_an_ecu(
    hass: HomeAssistant,
    mock_modbus_connection: MockModbusConnection,
    silent: bool,
    reason: str,
) -> None:
    if silent:
        mock_modbus_connection.for_unit(1).fail_requests(ModbusTimeoutError())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DISCOVERY
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


async def test_reconfigure(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    assert result["data_schema"]({})[CONF_UNIT_IDS] == "1, 2"

    result = await submit_and_wait(
        hass,
        result["flow_id"],
        {CONF_HOST: "192.0.2.11", CONF_PORT: 502, CONF_UNIT_IDS: "1"},
    )
    assert result["step_id"] == "confirm"
    # Leaving out unit 2 removes it, so the screen says so first.
    assert result["errors"] == {"base": "missing_inverters"}
    assert result["description_placeholders"]["missing"] == "703000000002"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_config_entry.data[CONF_HOST] == "192.0.2.11"
    assert [item["unit_id"] for item in mock_config_entry.data[CONF_INVERTERS]] == [1]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1-8", list(range(1, 9))),
        ("1, 2, 5", [1, 2, 5]),
        ("3", [3]),
        ("2-3,1", [1, 2, 3]),
    ],
)
def test_parse_unit_ids(text: str, expected: list[int]) -> None:
    assert parse_unit_ids(text) == expected


@pytest.mark.parametrize("text", ["0", "248", "8-1", "a", "", "1-"])
def test_parse_unit_ids_rejects(text: str) -> None:
    with pytest.raises(ValueError, match=r".*"):
        parse_unit_ids(text)


def timeout_once(value: int):
    """Return a register source that times out on the first read only."""
    calls = 0

    def read() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ModbusTimeoutError
        return value

    return read


async def test_slow_inverter_during_search_is_retried(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    # The mock evaluates every register source on each read of a unit, so one
    # slow register makes the next read of unit 2 time out once.
    unit = mock_ecu.for_unit(2)
    unit.holding[40000] = timeout_once(unit.holding[40000])
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["count"] == "2"


async def test_search_keeps_one_timeout(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    result = await start_user_flow(hass)

    await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    # Changing the timeout halfway would rebuild the connection mid-search.
    assert mock_ecu.for_unit(1).required_timeout == 5.0


async def test_reopened_form_keeps_connection(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    mock_ecu.for_unit(2).fail_read(40004, ModbusTimeoutError())
    result = await start_user_flow(hass)
    result = await submit_and_wait(
        hass, result["flow_id"], {**USER_INPUT, CONF_UNIT_IDS: "1-2"}
    )
    assert result["errors"] == {"base": "ecu_busy"}

    # Reopening the dialog re-runs the step without input.
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["step_id"] == "user"
    defaults = result["data_schema"]({})
    assert defaults[CONF_HOST] == HOST
    assert defaults[CONF_PORT] == 502


async def test_dhcp_failure_offers_the_address_field(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DISCOVERY
    )
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())

    result = await submit_and_wait(hass, result["flow_id"], {})
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "no_inverters"}
    assert result["data_schema"]({})[CONF_HOST] == HOST

    # Reopening the dialog shows the same form.
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["step_id"] == "user"

    # With the addresses entered the ECU is added, still with its MAC.
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).fail_requests(None)
    result = await submit_and_wait(
        hass, result["flow_id"], {**USER_INPUT, CONF_UNIT_IDS: "1-2"}
    )
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["count"] == "2"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MAC] == "80:97:1b:00:00:01"


async def test_known_ecu_on_new_address_is_followed(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="704000000001",
        data={CONF_HOST: "192.0.2.99", CONF_PORT: 502, CONF_INVERTERS: INVERTERS},
    )
    entry.add_to_hass(hass)
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == HOST
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


def suggested(result: dict, key: str) -> object:
    """Return the suggested value of a form field."""
    for marker in result["data_schema"].schema:
        if marker == key:
            return (marker.description or {}).get("suggested_value")
    raise KeyError(key)


async def test_start_value_at_setup(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    result = await start_user_flow(hass)
    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert suggested(result, "lifetime_energy") is None

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"lifetime_energy": 21971.04}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get("sensor.apsystems_ecu_lifetime_energy")
    assert float(state.state) == pytest.approx(21971.04, abs=0.001)


async def test_stored_value_is_offered_when_adding_again(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    await async_request_start(hass, "704000000001", None)
    ledger = EnergyLedger(hass, "704000000001")
    ledger.record_daily("704000000001", 1000)
    ledger.finish_poll(complete=True)
    ledger.calibrate(30_000_000)
    await ledger.async_save()

    result = await start_user_flow(hass)
    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert suggested(result, "lifetime_energy") == 30000.0

    # Keeping the offered value continues counting without a new start value.
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {"lifetime_energy": 30000.0}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get("sensor.apsystems_ecu_lifetime_energy")
    assert float(state.state) >= 30000.0


async def test_clearing_the_offered_value_starts_over(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    ledger = EnergyLedger(hass, "704000000001")
    ledger.calibrate(30_000_000)
    await ledger.async_save()

    result = await start_user_flow(hass)
    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get("sensor.apsystems_ecu_lifetime_energy")
    assert float(state.state) == pytest.approx(1.096, abs=0.001)


async def test_options_correct_lifetime_and_peak(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert suggested(result, "lifetime_energy") == 1.1
    assert suggested(result, "peak_power") == 710

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"lifetime_energy": 25000.0, "peak_power": 0},
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {}
    ledger = mock_config_entry.runtime_data.ledger
    assert ledger.lifetime == pytest.approx(25_000_000, abs=1)
    # Peak was reset to 0; the next poll sets it again.
    assert ledger.peak_power == 0
    await mock_config_entry.runtime_data.async_refresh()
    assert ledger.peak_power == 710


async def test_options_unchanged_values_change_nothing(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    before = mock_config_entry.runtime_data.ledger.lifetime

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "lifetime_energy": suggested(result, "lifetime_energy"),
            "peak_power": suggested(result, "peak_power"),
        },
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_config_entry.runtime_data.ledger.lifetime == pytest.approx(
        before, abs=1
    )


async def test_reconfigure_keeps_the_name(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Solar ECU",
        unique_id="704000000001",
        data={CONF_HOST: HOST, CONF_PORT: 502, CONF_INVERTERS: INVERTERS},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["reason"] == "reconfigure_successful"
    assert entry.title == "Solar ECU"


async def test_reconfigure_to_another_ecu_is_refused(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    other = [{**INVERTERS[0], "serial_number": "704000009999"}]
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="704000009999",
        data={CONF_HOST: "192.0.2.50", CONF_PORT: 502, CONF_INVERTERS: other},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "different_ecu"
    assert entry.data[CONF_HOST] == "192.0.2.50"


async def test_silent_address_list_stops_early(
    hass: HomeAssistant, mock_modbus_connection: MockModbusConnection
) -> None:
    for unit_id in range(1, 9):
        mock_modbus_connection.for_unit(unit_id).fail_requests(ModbusTimeoutError())
    result = await start_user_flow(hass)

    result = await submit_and_wait(
        hass, result["flow_id"], {**USER_INPUT, CONF_UNIT_IDS: "1-8"}
    )
    assert result["errors"] == {"base": "no_inverters"}
    asked = [u for u in range(1, 9) if mock_modbus_connection.for_unit(u).read_events]
    assert asked == [1, 2]


async def test_search_is_short_after_the_last_inverter(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    for unit_id in (3, 4):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["description_placeholders"]["count"] == "2"
    # The first silent address gets two tries, the next one a single try.
    assert len(mock_ecu.for_unit(3).read_events) == 2
    assert len(mock_ecu.for_unit(4).read_events) == 1


async def test_search_finds_more_than_32_inverters(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    # An ECU-R serves up to 100 inverters; the search goes on while they answer.
    for unit_id in range(3, 41):
        add_inverter(mock_ecu, unit_id, f"703{unit_id:09d}")
    for unit_id in (41, 42):
        mock_ecu.for_unit(unit_id).fail_requests(ModbusTimeoutError())
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["description_placeholders"]["count"] == "40"


@pytest.mark.parametrize("unit_ids", ["", "1-2"], ids=["search", "addresses"])
async def test_identity_is_one_read_per_inverter(
    hass: HomeAssistant, mock_ecu: MockModbusConnection, unit_ids: str
) -> None:
    result = await start_user_flow(hass)
    result = await submit_and_wait(
        hass, result["flow_id"], {**USER_INPUT, CONF_UNIT_IDS: unit_ids}
    )
    assert result["description_placeholders"]["count"] == "2"

    # One read finds the inverter and tells what it is.
    reads_2 = [(e.address, e.count) for e in mock_ecu.for_unit(2).read_events]
    assert len(reads_2) == 1
    assert reads_2[0][0] == 40000
    # The controls header is checked once, on the first inverter.
    assert (40184, 1) in [
        (e.address, e.count) for e in mock_ecu.for_unit(1).read_events
    ]


async def test_unsupported_controls_block(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    mock_ecu.for_unit(1).holding[40184] = 160
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "unsupported_layout"}


def timeout_twice(value: int):
    """Return a register source that times out on the first two reads."""
    calls = 0

    def read() -> int:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise ModbusTimeoutError
        return value

    return read


async def test_slow_identity_gets_three_tries(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    unit = mock_ecu.for_unit(2)
    unit.holding[40004] = timeout_twice(unit.holding[40004])
    result = await start_user_flow(hass)

    result = await submit_and_wait(
        hass, result["flow_id"], {**USER_INPUT, CONF_UNIT_IDS: "1-2"}
    )
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["count"] == "2"


async def test_silent_inverter_during_search_is_left_out(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    # An address that keeps timing out is an empty address to the search.
    mock_ecu.for_unit(2).fail_requests(ModbusTimeoutError())
    result = await start_user_flow(hass)

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["count"] == "1"


async def test_installation_power_at_setup(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    result = await start_user_flow(hass)
    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    # The example under the field uses the number of inverters found.
    placeholders = result["description_placeholders"]
    assert placeholders["ds3_l_kw"] == "1.5"
    assert placeholders["ds3_kw"] == "1.8"

    # Watts typed into the kW field are caught.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"installation_power": 1460}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"installation_power": "invalid_installation_power"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"installation_power": 1.46}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {"installation_power": 1.5}


async def test_options_installation_power(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert suggested(result, "installation_power") is None
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"installation_power": 0.1}
    )
    assert result["errors"] == {"installation_power": "invalid_installation_power"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"installation_power": 1.4}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_config_entry.options == {"installation_power": 1.4}

    # Leaving it empty removes it again.
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert suggested(result, "installation_power") == 1.4
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_config_entry.options == {}


@pytest.mark.parametrize("language", ["nl", "de", "fr"])
async def test_example_uses_a_decimal_comma(
    hass: HomeAssistant, mock_ecu: MockModbusConnection, language: str
) -> None:
    await hass.config.async_update(language=language)
    result = await start_user_flow(hass)
    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["description_placeholders"]["ds3_l_kw"] == "1,5"


async def test_reconfigure_after_a_new_address_searches_afresh(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        new_address_issue_id(mock_config_entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="addresses_changed",
    )
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["data_schema"]({})[CONF_UNIT_IDS] == ""

    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["step_id"] == "confirm"
    assert result["errors"] is None


async def test_reconfigure_searches_past_a_gap(
    hass: HomeAssistant, mock_ecu: MockModbusConnection
) -> None:
    add_inverter(mock_ecu, 5, "703000000005")
    fifth = {CONF_UNIT_ID: 5, CONF_SERIAL_NUMBER: "703000000005", CONF_MODEL: "DS3"}
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="704000000001",
        data={CONF_HOST: HOST, CONF_PORT: 502, CONF_INVERTERS: [*INVERTERS, fifth]},
    )

    # A new setup stops after two silent addresses.
    result = await start_user_flow(hass)
    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["description_placeholders"]["count"] == "2"
    hass.config_entries.flow.async_abort(result["flow_id"])

    entry.add_to_hass(hass)
    # A reconfigure keeps going until the highest known address.
    result = await entry.start_reconfigure_flow(hass)
    result = await submit_and_wait(hass, result["flow_id"], USER_INPUT)
    assert result["description_placeholders"]["count"] == "3"
    assert result["errors"] is None
