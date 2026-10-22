"""Tests for the diagnostics download."""

from __future__ import annotations

import json

from modbus_connection.mock import MockModbusConnection

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.apsystems_modbus.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_are_redacted(
    hass: HomeAssistant,
    mock_ecu: MockModbusConnection,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    text = json.dumps(diagnostics, default=str)

    assert "192.0.2.10" not in text
    assert "704000000001" not in text
    assert diagnostics["coordinator"]["available"] == [1, 2]
    assert set(diagnostics["coordinator"]["value_age_seconds"]) == {1, 2}
    polling = diagnostics["polling"]
    assert polling["settle_s"] == 38
    assert polling["poll_healthy"] is True
    assert polling["seconds_per_inverter"] >= 0
    assert diagnostics["inverter_output"] == {
        "enabled": True,
        "catch_up": False,
        "last_write": None,
        "last_result": None,
    }
    assert diagnostics["inverters"]["1"]["measurements"]["ac_power"] == 204.0
    assert diagnostics["power_limit"] == {
        "control_unit": 1,
        "desired": None,
        "pending": False,
        "shown": 100,
        "deviations": 0,
        "last_rewrite": None,
        "refreshing": False,
        "next_refresh": None,
        "rewrites": 0,
        "installation_power_kw": None,
        "overshoot_refreshes": 0,
    }
