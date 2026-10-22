"""Diagnostics: the last poll per inverter, without addresses or serial numbers."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_MAC
from homeassistant.core import HomeAssistant

from .const import CONF_ECU_ID, CONF_SERIAL_NUMBER
from .coordinator import ApsystemsConfigEntry
from .ecu import Inverter

# CONF_ECU_ID: entries made before the field was removed may still hold it.
TO_REDACT = {CONF_HOST, CONF_MAC, CONF_ECU_ID, CONF_SERIAL_NUMBER}


def _component_values(inverter: Inverter) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in ("measurements", "status"):
        component = getattr(inverter, name)
        values[name] = {
            field: getattr(component, field) for field in component.declared_fields
        }
    return values


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ApsystemsConfigEntry
) -> dict[str, Any]:
    """Return what the integration knows, redacted."""
    coordinator = entry.runtime_data
    data = coordinator.data
    output = coordinator.inverter_output
    limit = coordinator.power_limit
    return {
        "entry": {
            # The title can hold the ECU id, so it is left out.
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval),
            "available": sorted(data.available) if data else None,
            "recent": sorted(data.recent) if data else None,
            "value_age_seconds": coordinator.value_ages(),
            "power": data.power if data else None,
            "energy_today": data.energy_today if data else None,
            "online": data.online if data else None,
            "last_change": data.last_change.isoformat()
            if data and data.last_change
            else None,
        },
        "polling": {
            "settle_s": coordinator.settle,
            "poll_healthy": coordinator.poll_healthy,
            "seconds_per_inverter": coordinator.seconds_per_inverter,
        },
        "inverter_output": {
            "enabled": output.enabled,
            "catch_up": output.catch_up,
            "last_write": output.last_write.isoformat() if output.last_write else None,
            "last_result": output.last_result,
        },
        "power_limit": {
            "control_unit": limit.control.unit_id,
            "desired": limit.desired,
            "pending": limit.pending,
            "shown": limit.shown,
            "deviations": limit.deviations,
            "last_rewrite": limit.last_rewrite.isoformat()
            if limit.last_rewrite
            else None,
            "refreshing": limit.refreshing,
            "next_refresh": limit.next_refresh.isoformat()
            if limit.next_refresh
            else None,
            "rewrites": limit.rewrites,
            "installation_power_kw": limit.installation_power,
            "overshoot_refreshes": limit.overshoot_refreshes,
        },
        "lifetime": {
            "lifetime_energy_wh": coordinator.ledger.lifetime,
            "peak_power_w": coordinator.ledger.peak_power,
        },
        "inverters": {
            str(unit_id): async_redact_data(_component_values(inverter), TO_REDACT)
            for unit_id, inverter in sorted(coordinator.inverters.items())
        },
    }
