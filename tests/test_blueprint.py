"""Tests for the blueprint that steers the ECU on the electricity price.

The blueprint runs against the real integration with the mock ECU. Its own
sensors (power and last data update) are set directly before each measurement,
so a test controls exactly what the blueprint sees.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from pathlib import Path
import shutil
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from modbus_connection.mock import MockModbusConnection
import pytest

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    selector,
)
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.apsystems_modbus.const import CONF_INVERTERS, DOMAIN

from .conftest import INVERTERS, set_float32

BLUEPRINT = (
    Path(__file__).parents[1]
    / "blueprints"
    / "automation"
    / "apsystems_modbus"
    / "dynamic_tariffs.yaml"
)
LIMIT = "number.apsystems_ecu_power_limit"
SWITCH = "switch.apsystems_ecu_inverter_output"
POWER = "sensor.apsystems_ecu_power"
DATA = "sensor.apsystems_ecu_last_data_update"
IMPORT_PRICE = "sensor.import_price"
EXPORT_PRICE = "sensor.export_price"
GRID = "sensor.grid"
DEMAND = "sensor.power_demand"
NOTIFY = "notify.phone"
AC_POWER = 40144
START = datetime(2026, 4, 25, 10, 0, 50, tzinfo=dt_util.UTC)


class Rig:
    """The ECU, the prices and the clock as the blueprint sees them."""

    def __init__(self, hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
        self.hass = hass
        self.freezer = freezer
        self.power = 3000.0
        self.data_age = timedelta(0)
        self.messages: list[ServiceCall] = []

    def show(self) -> None:
        """Put the chosen ECU power and data age in the integration's sensors."""
        for entity, value in (
            (POWER, str(self.power)),
            (DATA, (dt_util.utcnow() - self.data_age).isoformat()),
        ):
            state = self.hass.states.get(entity)
            assert state is not None
            self.hass.states.async_set(entity, value, state.attributes)

    async def settle(self) -> None:
        """Let the event loop finish what it can.

        Not async_block_till_done: that would also wait for an automation
        that sits in a delay, which only ends when the clock is moved on.
        """
        for _ in range(100):
            await asyncio.sleep(0)

    async def tick_to(self, when: datetime) -> None:
        self.freezer.move_to(when)
        async_fire_time_changed(self.hass, when)
        await self.settle()
        self.show()
        await self.settle()

    async def wait(self, minutes: float) -> None:
        await self.tick_to(dt_util.utcnow() + timedelta(minutes=minutes))

    async def limit_moment(self, between: Callable[[], None] | None = None) -> None:
        """Run the next limit moment: trigger, measure, wait a minute, measure.

        ``between`` changes something after the first measurement.
        """
        moment = dt_util.utcnow().replace(second=0, microsecond=0)
        moment += timedelta(minutes=1)
        while moment.minute % 5 != 2:
            moment += timedelta(minutes=1)
        await self.tick_to(moment)
        await self.tick_to(moment + timedelta(seconds=30))
        if between is not None:
            between()
            await self.settle()
        await self.tick_to(moment + timedelta(seconds=90))

    def price(self, entity: str, value: float | str, unit: str = "EUR/kWh") -> None:
        self.hass.states.async_set(entity, str(value), {"unit_of_measurement": unit})

    def meter(self, entity: str, value: float, unit: str = "W") -> None:
        self.hass.states.async_set(
            entity, str(value), {"unit_of_measurement": unit, "device_class": "power"}
        )

    @property
    def desired(self) -> int:
        state = self.hass.states.get(LIMIT)
        assert state is not None
        desired = state.attributes["desired"]
        return 100 if desired is None else int(desired)

    @property
    def output(self) -> str:
        state = self.hass.states.get(SWITCH)
        assert state is not None
        return state.state


async def start(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_ecu: MockModbusConnection,
    tmp_path: Path,
    *,
    installation_power: float | None = 6.0,
    **inputs: Any,
) -> Rig:
    """Set up the integration and one automation from the blueprint."""
    freezer.move_to(START)
    for unit_id in (1, 2):
        # Low real power, so the integration's own overshoot check stays quiet.
        set_float32(mock_ecu.for_unit(unit_id), AC_POWER, 10.0)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="APsystems ECU",
        unique_id="704000000001",
        data={"host": "192.0.2.10", "port": 502, CONF_INVERTERS: INVERTERS},
        options={"installation_power": installation_power}
        if installation_power
        else {},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    rig = Rig(hass, freezer)
    rig.messages = async_mock_service(hass, "notify", "send_message")
    hass.states.async_set("sun.sun", "above_horizon")
    rig.price(IMPORT_PRICE, 0.20)
    if EXPORT_PRICE in inputs.values():
        rig.price(EXPORT_PRICE, 0.10)

    folder = tmp_path / "blueprints" / "automation" / "apsystems_modbus"
    folder.mkdir(parents=True)
    shutil.copy(BLUEPRINT, folder / BLUEPRINT.name)
    hass.config.config_dir = str(tmp_path)
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "ecu-704000000001"), entry.entry_id
    )
    assert device is not None
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "price",
                "alias": "Price",
                "use_blueprint": {
                    "path": f"apsystems_modbus/{BLUEPRINT.name}",
                    "input": {"ecu": device.id, "import_price": IMPORT_PRICE, **inputs},
                },
            }
        },
    )
    await hass.async_block_till_done()
    assert hass.states.get("automation.price").state == STATE_ON
    # Past the minimum time since the limit last changed at setup.
    await rig.wait(31)
    return rig


@pytest.fixture
async def rig_factory(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_ecu: MockModbusConnection,
    tmp_path: Path,
) -> AsyncIterator[Callable[..., Any]]:
    async def make(**inputs: Any) -> Rig:
        return await start(hass, freezer, mock_ecu, tmp_path, **inputs)

    yield make
    # Stop the automation, so its clock listeners do not outlive the test.
    if hass.states.get("automation.price") is not None:
        await hass.services.async_call(
            "automation", "turn_off", {"entity_id": "automation.price"}, blocking=True
        )
    for entry in hass.config_entries.async_entries(DOMAIN):
        await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


GRID_SETUP = {"export_price": EXPORT_PRICE, "grid_power": GRID, "notify": NOTIFY}


async def test_normal_writes_nothing(rig_factory: Callable[..., Any]) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.meter(GRID, -2000)
    await rig.limit_moment()
    assert rig.desired == 100
    assert rig.output == STATE_ON
    assert rig.messages == []


async def test_negative_import_price_turns_the_output_off_at_once(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.price(IMPORT_PRICE, -0.05)
    await rig.settle()
    assert rig.output == STATE_OFF
    assert rig.desired == 100, "off is only the switch"
    assert [call.data["title"] for call in rig.messages] == ["Inverter output off"]
    assert rig.messages[-1].data["message"].startswith("The import price is below 0.")

    rig.price(IMPORT_PRICE, 0.05)
    await rig.settle()
    assert rig.output == STATE_ON
    assert rig.messages[-1].data["title"] == "Inverter output on"
    assert rig.messages[-1].data["message"].startswith("Back to normal.")


async def test_output_on_while_the_export_price_stays_low(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.price(EXPORT_PRICE, -0.02)
    rig.price(IMPORT_PRICE, -0.05)
    await rig.settle()
    assert rig.output == STATE_OFF

    rig.price(IMPORT_PRICE, 0.05)
    await rig.settle()
    assert rig.output == STATE_ON
    assert (
        rig.messages[-1]
        .data["message"]
        .startswith("The import price is 0 or higher again.")
    )


async def test_off_wins_over_the_export_price(rig_factory: Callable[..., Any]) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.meter(GRID, -2000)
    rig.price(EXPORT_PRICE, -0.10)
    rig.price(IMPORT_PRICE, -0.05)
    await rig.limit_moment()
    assert rig.output == STATE_OFF
    assert rig.desired == 100


async def test_export_below_threshold_limits_to_what_the_house_uses(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    # 3 kW solar, 2 kW to the grid: the house uses 1 kW, plus 0.5 kW margin.
    rig.power = 3000
    rig.meter(GRID, -2000)
    rig.price(EXPORT_PRICE, -0.02)
    await rig.limit_moment()
    assert rig.desired == 25  # 1.5 kW of 6 kW
    assert rig.output == STATE_ON
    assert [call.data["title"] for call in rig.messages] == ["Power limit 25 %"]
    assert (
        rig.messages[-1]
        .data["message"]
        .startswith("Exporting to the grid costs money now.")
    )


async def test_threshold_is_respected(rig_factory: Callable[..., Any]) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP, export_threshold=-0.03)
    rig.meter(GRID, -2000)
    rig.price(EXPORT_PRICE, -0.02)
    await rig.limit_moment()
    assert rig.desired == 100


async def test_export_price_in_cents_uses_the_threshold_per_kwh(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.meter(GRID, -2000)
    # -0.5 ct/kWh is half a cent: above the default threshold of -0.01 per kWh.
    rig.price(EXPORT_PRICE, -0.5, "ct/kWh")
    await rig.limit_moment()
    assert rig.desired == 100

    rig.price(EXPORT_PRICE, -2, "ct/kWh")
    await rig.limit_moment()
    assert rig.desired == 25


async def test_export_price_per_mwh_uses_the_threshold_per_kwh(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.meter(GRID, -2000)
    rig.price(EXPORT_PRICE, -5, "€/MWh")
    await rig.limit_moment()
    assert rig.desired == 100

    rig.price(EXPORT_PRICE, -20, "€/MWh")
    await rig.limit_moment()
    assert rig.desired == 25


async def test_no_limit_while_the_house_uses_all_solar_power(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.power = 1000
    rig.meter(GRID, 200)
    rig.price(EXPORT_PRICE, -0.02)
    await rig.limit_moment()
    assert rig.desired == 100


@pytest.mark.parametrize(("value", "unit"), [(1500, "W"), (1.5, "kW")])
async def test_own_sensor_for_the_power_demand(
    rig_factory: Callable[..., Any], value: float, unit: str
) -> None:
    rig: Rig = await rig_factory(
        export_price=EXPORT_PRICE, grid_power=GRID, power_demand=DEMAND
    )
    rig.meter(GRID, -9000)  # Ignored: the own sensor goes first.
    rig.meter(DEMAND, value, unit)
    rig.price(EXPORT_PRICE, -0.02)
    await rig.limit_moment()
    assert rig.desired == 25


async def test_grid_power_in_kw(rig_factory: Callable[..., Any]) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.meter(GRID, -2.0, "kW")
    rig.price(EXPORT_PRICE, -0.02)
    await rig.limit_moment()
    assert rig.desired == 25


async def test_ecu_power_shown_in_kw(rig_factory: Callable[..., Any]) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    er.async_get(rig.hass).async_update_entity_options(
        POWER, "sensor", {"unit_of_measurement": "kW"}
    )
    await rig.settle()
    state = rig.hass.states.get(POWER)
    assert state is not None
    assert state.attributes["unit_of_measurement"] == "kW"
    rig.power = 3.0
    rig.meter(GRID, -2000)
    rig.price(EXPORT_PRICE, -0.02)
    await rig.limit_moment()
    assert rig.desired == 25


async def test_no_meter_and_no_own_sensor_skips_the_limit(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(export_price=EXPORT_PRICE)
    rig.price(EXPORT_PRICE, -0.02)
    await rig.limit_moment()
    assert rig.desired == 100


async def test_unknown_power_demand_writes_nothing(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(export_price=EXPORT_PRICE, power_demand=DEMAND)
    rig.hass.states.async_set(DEMAND, STATE_UNKNOWN, {"unit_of_measurement": "kW"})
    rig.price(EXPORT_PRICE, -0.02)
    await rig.limit_moment()
    assert rig.desired == 100


async def test_without_installation_power_the_limit_is_skipped(
    rig_factory: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    rig: Rig = await rig_factory(installation_power=None, **GRID_SETUP)
    rig.meter(GRID, -2000)
    rig.price(EXPORT_PRICE, -0.02)
    await rig.limit_moment()
    assert rig.desired == 100
    assert "Error" not in caplog.text


@pytest.mark.parametrize(("first", "second"), [(-2000, 2100), (2100, -2000)])
async def test_both_measurements_must_show_the_need(
    rig_factory: Callable[..., Any], first: float, second: float
) -> None:
    """A kettle in either measurement keeps the limit off."""
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.meter(GRID, first)
    rig.price(EXPORT_PRICE, -0.02)

    def kettle_switches() -> None:
        rig.meter(GRID, second)

    await rig.limit_moment(between=kettle_switches)
    assert rig.desired == 100


async def test_adjusts_only_by_ten_points_or_more(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.meter(GRID, -2000)
    rig.price(EXPORT_PRICE, -0.02)
    await rig.limit_moment()
    assert rig.desired == 25

    # Now limited to 1.5 kW: the house takes 0.4 kW more, under 10 points.
    rig.power = 1500
    rig.meter(GRID, -100)
    await rig.wait(31)
    await rig.limit_moment()
    assert rig.desired == 25

    # 0.7 kW more is 11.7 points: adjusted.
    rig.meter(GRID, 600)
    await rig.wait(5)
    await rig.limit_moment()
    assert rig.desired == 44  # (1.5 + 0.6 + 0.5) / 6 = 43.3 %


async def test_back_to_100_when_the_house_can_use_everything(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.meter(GRID, -2000)
    rig.price(EXPORT_PRICE, -0.02)
    await rig.limit_moment()
    assert rig.desired == 25

    rig.power = 1500
    rig.meter(GRID, 5000)
    await rig.wait(31)
    await rig.limit_moment()
    assert rig.desired == 100


async def test_waits_the_minimum_time_between_changes(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.meter(GRID, -2000)
    rig.price(EXPORT_PRICE, -0.02)
    await rig.limit_moment()
    assert rig.desired == 25

    # The block ends 10 minutes later: the limit stays until 30 minutes passed.
    rig.price(EXPORT_PRICE, 0.05)
    await rig.wait(10)
    await rig.limit_moment()
    assert rig.desired == 25
    await rig.wait(10)
    await rig.limit_moment()
    assert rig.desired == 100
    assert rig.messages[-1].data["title"] == "Power limit 100 %"
    assert rig.messages[-1].data["message"].startswith("Back to normal.")


def test_price_fields_only_offer_price_sensors() -> None:
    inputs = load_yaml_dict(str(BLUEPRINT))["blueprint"]["input"]
    for field in (
        inputs["import_price"],
        inputs["limit_output"]["input"]["export_price"],
    ):
        selector.validate_selector(field["selector"])
        filters = field["selector"]["entity"]["filter"]
        assert [f["domain"] for f in filters] == ["sensor"]
        units = filters[0]["unit_of_measurement"]
        assert {"€/kWh", "EUR/kWh", "ct/kWh"} <= set(units)


def test_inputs_for_the_limit_and_the_options_are_folded_away() -> None:
    inputs = load_yaml_dict(str(BLUEPRINT))["blueprint"]["input"]
    assert list(inputs) == ["ecu", "import_price", "limit_output", "options"]
    follow = inputs["limit_output"]
    assert follow["collapsed"] is True
    assert list(follow["input"]) == [
        "export_price",
        "export_threshold",
        "grid_power",
        "power_demand",
    ]
    options = inputs["options"]
    assert options["collapsed"] is True
    assert list(options["input"]) == ["min_minutes", "notify"]
    assert options["input"]["min_minutes"]["default"] == 30


async def test_price_change_between_measurements_moves_the_limit_on(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.meter(GRID, -2000)
    rig.price(EXPORT_PRICE, -0.02)

    def import_price_drops() -> None:
        rig.price(IMPORT_PRICE, -0.05)

    await rig.limit_moment(between=import_price_drops)
    assert rig.output == STATE_OFF
    assert rig.desired == 100


async def test_limit_stays_when_going_from_off_back_to_export(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.meter(GRID, -2000)
    rig.price(EXPORT_PRICE, -0.02)
    await rig.limit_moment()
    assert rig.desired == 25

    rig.price(IMPORT_PRICE, -0.05)
    await rig.wait(1)
    assert rig.output == STATE_OFF
    rig.price(IMPORT_PRICE, 0.05)
    await rig.wait(1)
    assert rig.output == STATE_ON
    assert rig.desired == 25


async def test_stale_ecu_data_sets_no_new_limit(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.data_age = timedelta(minutes=11)
    rig.meter(GRID, -2000)
    rig.price(EXPORT_PRICE, -0.02)
    await rig.limit_moment()
    assert rig.desired == 100


async def test_stale_ecu_data_still_releases_the_limit(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.meter(GRID, -2000)
    rig.price(EXPORT_PRICE, -0.02)
    await rig.limit_moment()
    assert rig.desired == 25

    rig.data_age = timedelta(minutes=11)
    rig.price(EXPORT_PRICE, 0.05)
    await rig.wait(30)
    await rig.limit_moment()
    assert rig.desired == 100


async def test_unknown_price_changes_nothing_for_an_hour(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.price(IMPORT_PRICE, -0.05)
    await rig.settle()
    assert rig.output == STATE_OFF

    rig.price(IMPORT_PRICE, STATE_UNKNOWN)
    await rig.wait(30)
    await rig.limit_moment()
    assert rig.output == STATE_OFF

    await rig.wait(30)
    assert rig.output == STATE_ON


async def test_sun_down_is_normal(rig_factory: Callable[..., Any]) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.price(IMPORT_PRICE, -0.05)
    await rig.settle()
    assert rig.output == STATE_OFF

    rig.hass.states.async_set("sun.sun", "below_horizon")
    await rig.limit_moment()
    assert rig.output == STATE_ON


async def test_without_export_price_only_off_is_used(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(grid_power=GRID)
    rig.meter(GRID, -2000)
    rig.price(IMPORT_PRICE, 0.01)
    await rig.limit_moment()
    assert rig.desired == 100

    rig.price(IMPORT_PRICE, -0.01)
    await rig.limit_moment()
    assert rig.output == STATE_OFF
    assert rig.desired == 100


async def test_without_export_price_a_high_threshold_limits_nothing(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(grid_power=GRID, export_threshold=0.05)
    rig.meter(GRID, -2000)
    rig.price(IMPORT_PRICE, 0.03)
    await rig.limit_moment()
    assert rig.desired == 100
    assert rig.output == STATE_ON


async def test_unloaded_integration_gets_no_commands_or_messages(
    hass: HomeAssistant, rig_factory: Callable[..., Any]
) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    calls = async_mock_service(hass, "switch", "turn_on")
    for entry in hass.config_entries.async_entries(DOMAIN):
        await hass.config_entries.async_unload(entry.entry_id)
    await rig.settle()
    assert hass.states.get(SWITCH).state == "unavailable"

    await rig.limit_moment()
    assert calls == []
    assert rig.messages == []


async def test_same_choice_writes_nothing(
    rig_factory: Callable[..., Any], mock_ecu: MockModbusConnection
) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.meter(GRID, -2000)
    rig.price(EXPORT_PRICE, -0.02)
    await rig.limit_moment()
    assert rig.desired == 25

    writes: list[int] = []
    for unit_id in (1, 2):
        mock_ecu.for_unit(unit_id).on_write(lambda event: writes.append(event.address))
    rig.power = 1500
    rig.meter(GRID, 0)
    await rig.wait(31)
    await rig.limit_moment()
    assert rig.desired == 25
    assert writes == []


async def test_export_price_change_between_measurements_moves_the_limit_on(
    rig_factory: Callable[..., Any],
) -> None:
    rig: Rig = await rig_factory(**GRID_SETUP)
    rig.meter(GRID, -2000)
    rig.price(EXPORT_PRICE, -0.02)
    await rig.limit_moment()
    assert rig.desired == 25

    # The house takes more, then the export price rises before the second
    # measurement: the release waits for the next limit moment.
    rig.power = 1500
    rig.meter(GRID, 1000)
    await rig.wait(30)

    def export_price_rises() -> None:
        rig.price(EXPORT_PRICE, 0.05)

    await rig.limit_moment(between=export_price_rises)
    assert rig.desired == 25
    await rig.limit_moment()
    assert rig.desired == 100
