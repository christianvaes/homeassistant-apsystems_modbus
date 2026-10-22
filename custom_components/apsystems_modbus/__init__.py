"""The APsystems Modbus integration."""

from __future__ import annotations

import logging

from modbus_connection import ModbusError, ModbusTcpParams, ModbusTimeoutError

from homeassistant.components import persistent_notification
from homeassistant.components.modbus import async_get_temporary_unit, async_get_unit
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.translation import async_get_translations

from .const import CONF_INVERTERS, CONF_SERIAL_NUMBER, CONF_UNIT_ID, DOMAIN
from .coordinator import (
    ApsystemsConfigEntry,
    EcuCoordinator,
    new_address_issue_id,
    silent_issue_id,
)
from .ecu import Inverter
from .entity import ecu_device_info, ecu_identifier
from .inverter_output import output_store_for, stored_off
from .lifetime import EnergyLedger
from .power_limit import (
    NO_LIMIT,
    effective_limit,
    issue_id_for,
    store_for,
    stored_choice,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]
RELEASE_ATTEMPTS = 2
"""Inverters tried on removal, so one silent inverter does not block it."""


async def async_setup_entry(hass: HomeAssistant, entry: ApsystemsConfigEntry) -> bool:
    """Set up polling without waiting for the ECU.

    The register layout was checked when the ECU was added and does not
    change. Asking for it again at every start only made a slow ECU hold up
    or fail the start. The first poll runs in the background and shows any
    problem as unavailable entities with a message in the log.
    """
    params = ModbusTcpParams(host=entry.data[CONF_HOST], port=entry.data[CONF_PORT])
    inverters: dict[int, Inverter] = {}
    try:
        for item in entry.data[CONF_INVERTERS]:
            unit_id = item[CONF_UNIT_ID]
            unit = async_get_unit(hass, entry, params, unit_id)
            inverters[unit_id] = Inverter(unit, unit_id)
    except HomeAssistantError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="link_in_use",
            translation_placeholders={"host": entry.data[CONF_HOST]},
        ) from err
    # The ECU-wide values such as the power limit are read through one unit.
    lead = inverters[min(inverters)]

    # The stored totals belong to the ECU, not to this entry, so they survive
    # removing and re-adding the integration.
    ledger = EnergyLedger(hass, entry.unique_id or entry.entry_id)
    await ledger.async_load()
    coordinator = EcuCoordinator(hass, entry, inverters, lead, ledger)
    entry.runtime_data = coordinator
    await coordinator.power_limit.async_load()
    await coordinator.inverter_output.async_load()
    # Notices are checked afresh after a change or a reconfigure.
    ir.async_delete_issue(hass, DOMAIN, issue_id_for(entry.entry_id))
    ir.async_delete_issue(hass, DOMAIN, new_address_issue_id(entry.entry_id))
    ir.async_delete_issue(hass, DOMAIN, silent_issue_id(entry.entry_id))
    # The inverter devices hang below the ECU device, so register that first.
    registry = dr.async_get(hass)
    ecu_device = registry.async_get_or_create(
        config_entry_id=entry.entry_id, **ecu_device_info(entry)
    )
    coordinator.ecu_device_id = ecu_device.id
    _remove_stale_devices(registry, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.inverter_output.async_start(entry)
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), f"{DOMAIN} first poll"
    )
    return True


def _remove_stale_devices(
    registry: dr.DeviceRegistry, entry: ApsystemsConfigEntry
) -> None:
    """Drop inverters that are no longer configured, e.g. after a replacement."""
    current = {ecu_identifier(entry)} | {
        (DOMAIN, item[CONF_SERIAL_NUMBER]) for item in entry.data[CONF_INVERTERS]
    }
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if not device.identifiers & current:
            registry.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )


async def async_unload_entry(hass: HomeAssistant, entry: ApsystemsConfigEntry) -> bool:
    """Stop polling and store the totals; the modbus integration closes the link."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.ledger.async_save()
        await entry.runtime_data.power_limit.async_save()
        await entry.runtime_data.inverter_output.async_save()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ApsystemsConfigEntry) -> None:
    """Undo what this integration set, so the inverters are not left off or limited."""
    await _async_restore_output(hass, entry)
    await _async_remove_limit(hass, entry)


async def _async_remove_limit(hass: HomeAssistant, entry: ApsystemsConfigEntry) -> None:
    """Release a power limit this integration set, so the inverters are not left limited.

    Without a choice of its own the ECU is not contacted at all, so a limit
    set by someone else, such as the installer, is then left alone. At 100 the
    ECU is still checked, since a change to 100 may not have arrived yet. If a
    limit below 100 could not be checked or released, the user is told,
    because nothing else would release it any more. The stored choice goes
    too, so adding the ECU again starts without a limit.
    """
    store = store_for(hass, entry.entry_id)
    desired = stored_choice(await store.async_load())
    await store.async_remove()
    ir.async_delete_issue(hass, DOMAIN, issue_id_for(entry.entry_id))
    ir.async_delete_issue(hass, DOMAIN, new_address_issue_id(entry.entry_id))
    ir.async_delete_issue(hass, DOMAIN, silent_issue_id(entry.entry_id))
    if desired is None:
        return
    try:
        released = await _async_release_limit(hass, entry)
    except (ModbusError, HomeAssistantError) as err:
        _LOGGER.warning(
            "Could not release the power limit on removal (%s)", type(err).__name__
        )
        released = False
    if released or desired >= NO_LIMIT:
        return
    await _async_notify(
        hass,
        "limit_maybe_active",
        "The APsystems ECU could not be checked while the integration was "
        "removed. If a power limit was set, the inverters may still be "
        "limited. Add the integration again and set the power limit to 100 %.",
        f"{DOMAIN}_power_limit_{entry.entry_id}",
    )


async def _async_restore_output(
    hass: HomeAssistant, entry: ApsystemsConfigEntry
) -> None:
    """Turn the inverter output on again if this integration left it off.

    An ECU-R turns the inverters on by itself once the off writes stop, but
    other ECUs keep them off until told otherwise. Only written, never read:
    the ECU-R reports the output as on whatever it does.
    """
    store = output_store_for(hass, entry.entry_id)
    was_off = stored_off(await store.async_load())
    await store.async_remove()
    if not was_off:
        return
    params = ModbusTcpParams(host=entry.data[CONF_HOST], port=entry.data[CONF_PORT])
    unit_ids = sorted(item[CONF_UNIT_ID] for item in entry.data[CONF_INVERTERS])
    for unit_id in unit_ids[:RELEASE_ATTEMPTS]:
        try:
            async with async_get_temporary_unit(hass, params, unit_id) as unit:
                await Inverter(unit, unit_id).async_set_grid_connection(True)
        except ModbusTimeoutError:
            continue
        except (ModbusError, HomeAssistantError) as err:
            _LOGGER.warning(
                "Could not turn the inverter output on before removal (%s)",
                type(err).__name__,
            )
            break
        else:
            _LOGGER.info("Turned the inverter output on before removing the ECU")
            return
    await _async_notify(
        hass,
        "output_maybe_off",
        "The APsystems ECU could not be reached while the integration was "
        "removed. Inverter output was off. An ECU-R turns the inverters on by "
        "itself within ten minutes. If they stay off, turn the ECU off and on.",
        f"{DOMAIN}_inverter_output_{entry.entry_id}",
    )


async def _async_notify(
    hass: HomeAssistant, key: str, fallback: str, notification_id: str
) -> None:
    """Leave a notification in the user's language.

    Notifications have no translation keys of their own, so the text comes
    from this integration's translations.
    """
    texts = await async_get_translations(
        hass, hass.config.language, "exceptions", [DOMAIN]
    )
    persistent_notification.async_create(
        hass,
        texts.get(f"component.{DOMAIN}.exceptions.{key}.message", fallback),
        title="APsystems Modbus",
        notification_id=notification_id,
    )


async def _async_release_limit(
    hass: HomeAssistant, entry: ApsystemsConfigEntry
) -> bool:
    """Read the limit through the first inverter that answers and release it if set.

    The entry is already unloaded, so a short-lived connection is used. Nothing
    is written when the readback cannot be interpreted. Returns whether the
    limit is known to be released.
    """
    params = ModbusTcpParams(host=entry.data[CONF_HOST], port=entry.data[CONF_PORT])
    unit_ids = sorted(item[CONF_UNIT_ID] for item in entry.data[CONF_INVERTERS])
    for unit_id in unit_ids[:RELEASE_ATTEMPTS]:
        async with async_get_temporary_unit(hass, params, unit_id) as unit:
            inverter = Inverter(unit, unit_id)
            try:
                await inverter.measurements.async_update()
            except ModbusTimeoutError:
                continue
            reported = effective_limit(inverter.measurements)
            if reported is None:
                _LOGGER.warning("Unit %s reports an unreadable power limit", unit_id)
                return False
            if reported != NO_LIMIT:
                await inverter.async_set_export_limit(NO_LIMIT)
                await inverter.async_set_export_limit_enabled(False)
                _LOGGER.info("Released the power limit before removing the ECU")
            return True
    _LOGGER.warning(
        "Could not release the power limit on removal: no inverter answered"
    )
    return False
