"""Set up Grocery List Workflow."""

from __future__ import annotations

from dataclasses import dataclass

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_AI_CLASSIFICATION_ENABLED,
    CONF_AI_ENTITY_ID,
    CONF_ROUTE_PROFILE,
    CONF_SOURCE_ENTITY,
    CONF_TARGET_ENTITY,
    DEFAULT_AI_ENTITY_ID,
    DOMAIN,
)
from .sorter import GroceryRouteSorter, parse_route_profile
from .sync import TodoSynchronizer


@dataclass
class GroceryWorkflowData:
    """Runtime state for a configured workflow."""

    synchronizer: TodoSynchronizer
    sorter: GroceryRouteSorter


type GroceryWorkflowConfigEntry = ConfigEntry[GroceryWorkflowData]


async def _entry(hass: HomeAssistant, entry_id: str) -> GroceryWorkflowConfigEntry:
    entry = hass.config_entries.async_get_entry(entry_id)
    if not entry or entry.domain != DOMAIN or not entry.runtime_data:
        raise ValueError("Unknown or unloaded Grocery List Workflow entry")
    return entry


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register immediate workflow services."""
    async def sync_now(call) -> None:
        entry = await _entry(hass, call.data["entry_id"])
        await entry.runtime_data.synchronizer.async_sync()

    async def sort_now(call) -> None:
        entry = await _entry(hass, call.data["entry_id"])
        await entry.runtime_data.sorter.async_sort()

    async def sync_and_sort(call) -> None:
        entry = await _entry(hass, call.data["entry_id"])
        await entry.runtime_data.synchronizer.async_sync()
        await entry.runtime_data.sorter.async_sort()

    async def set_route_profile(call) -> None:
        entry = await _entry(hass, call.data["entry_id"])
        route_profile = call.data[CONF_ROUTE_PROFILE]
        parse_route_profile(route_profile)
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, CONF_ROUTE_PROFILE: route_profile},
        )
        await hass.config_entries.async_reload(entry.entry_id)

    async def set_ai_classification(call) -> None:
        entry = await _entry(hass, call.data["entry_id"])
        options = {
            **entry.options,
            CONF_AI_CLASSIFICATION_ENABLED: call.data[CONF_AI_CLASSIFICATION_ENABLED],
        }
        if CONF_AI_ENTITY_ID in call.data:
            options[CONF_AI_ENTITY_ID] = call.data[CONF_AI_ENTITY_ID]
        hass.config_entries.async_update_entry(entry, options=options)
        await hass.config_entries.async_reload(entry.entry_id)

    schema = vol.Schema({vol.Required("entry_id"): str})
    hass.services.async_register(DOMAIN, "sync_now", sync_now, schema=schema)
    hass.services.async_register(DOMAIN, "sort_now", sort_now, schema=schema)
    hass.services.async_register(DOMAIN, "sync_and_sort", sync_and_sort, schema=schema)
    hass.services.async_register(
        DOMAIN,
        "set_route_profile",
        set_route_profile,
        schema=vol.Schema(
            {
                vol.Required("entry_id"): str,
                vol.Required(CONF_ROUTE_PROFILE): dict,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "set_ai_classification",
        set_ai_classification,
        schema=vol.Schema(
            {
                vol.Required("entry_id"): str,
                vol.Required(CONF_AI_CLASSIFICATION_ENABLED): bool,
                vol.Optional(CONF_AI_ENTITY_ID): str,
            }
        ),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GroceryWorkflowConfigEntry) -> bool:
    """Set up a pair of native HA to-do entities."""
    source = entry.data[CONF_SOURCE_ENTITY]
    target = entry.data[CONF_TARGET_ENTITY]
    sorter = GroceryRouteSorter(
        hass,
        source,
        target,
        entry.options.get(CONF_ROUTE_PROFILE),
        ai_entity_id=(
            entry.options.get(CONF_AI_ENTITY_ID, DEFAULT_AI_ENTITY_ID)
            if entry.options.get(CONF_AI_CLASSIFICATION_ENABLED, False)
            else None
        ),
        cache_key=entry.entry_id,
    )
    synchronizer = TodoSynchronizer(
        hass,
        source,
        target,
        f"{DOMAIN}.{entry.entry_id}.sync",
        on_content_change=sorter.async_sort,
    )
    entry.runtime_data = GroceryWorkflowData(synchronizer, sorter)
    await synchronizer.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GroceryWorkflowConfigEntry) -> bool:
    """Unload a configured workflow."""
    await entry.runtime_data.synchronizer.async_stop()
    return True
