"""Config flow for Grocery List Workflow."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

import json

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AI_CLASSIFICATION_ENABLED,
    CONF_AI_ENTITY_ID,
    CONF_ROUTE_PROFILE,
    CONF_SOURCE_ENTITY,
    CONF_TARGET_ENTITY,
    DEFAULT_AI_ENTITY_ID,
    DOMAIN,
)
from .sorter import DEFAULT_ROUTE_PROFILE, parse_route_profile


def _valid_todo_entity(hass, entity_id: str) -> bool:
    """Confirm a configured entity currently exists and is a to-do list."""
    return entity_id.startswith("todo.") and hass.states.get(entity_id) is not None


class GroceryListWorkflowConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure source and destination to-do entities."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the private route-profile options flow."""
        return GroceryListWorkflowOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            source = user_input[CONF_SOURCE_ENTITY]
            target = user_input[CONF_TARGET_ENTITY]
            if source == target:
                errors["base"] = "same_entity"
            elif not _valid_todo_entity(self.hass, source) or not _valid_todo_entity(
                self.hass, target
            ):
                errors["base"] = "entity_not_found"
            else:
                await self.async_set_unique_id(f"{source}_{target}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"{source} to {target}", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SOURCE_ENTITY): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="todo")
                    ),
                    vol.Required(CONF_TARGET_ENTITY): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="todo")
                    ),
                }
            ),
            errors=errors,
        )


class GroceryListWorkflowOptionsFlow(OptionsFlow):
    """Edit the private route profile stored by Home Assistant."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit route JSON without placing it in the public repository."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                route_profile = json.loads(user_input[CONF_ROUTE_PROFILE])
                parse_route_profile(route_profile)
            except (json.JSONDecodeError, ValueError, TypeError):
                errors["base"] = "invalid_route_profile"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_ROUTE_PROFILE: route_profile,
                        CONF_AI_CLASSIFICATION_ENABLED: user_input[
                            CONF_AI_CLASSIFICATION_ENABLED
                        ],
                        CONF_AI_ENTITY_ID: user_input[CONF_AI_ENTITY_ID],
                    },
                )

        current = self.config_entry.options.get(CONF_ROUTE_PROFILE, DEFAULT_ROUTE_PROFILE)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ROUTE_PROFILE,
                        default=json.dumps(current, indent=2),
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(multiline=True)
                    ),
                    vol.Optional(
                        CONF_AI_CLASSIFICATION_ENABLED,
                        default=self.config_entry.options.get(
                            CONF_AI_CLASSIFICATION_ENABLED, False
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_AI_ENTITY_ID,
                        default=self.config_entry.options.get(
                            CONF_AI_ENTITY_ID, DEFAULT_AI_ENTITY_ID
                        ),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="ai_task")
                    ),
                }
            ),
            errors=errors,
        )
