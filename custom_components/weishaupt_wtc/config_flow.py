"""Config flow for Weishaupt WTC integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    WeishauptApiClient,
    WeishauptAuthError,
    WeishauptConnectionError,
)
from .const import (
    DEFAULT_PASSWORD,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USERNAME,
    DOMAIN,
    CONF_DETECTED_GROUPS,
    CONF_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL_VALIDATOR = vol.All(vol.Coerce(int), vol.Range(min=10, max=300))

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Optional(CONF_PASSWORD, default=DEFAULT_PASSWORD): str,
        vol.Optional(
            CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
        ): SCAN_INTERVAL_VALIDATOR,
    }
)


async def _validate_credentials(
    flow: ConfigFlow, host: str, username: str, password: str
) -> str | None:
    """Return an error key for the form, or None when the device accepts the login."""
    client = WeishauptApiClient(
        host=host,
        username=username,
        password=password,
        session=async_get_clientsession(flow.hass),
    )
    try:
        if not await client.test_connection():
            return "cannot_connect"
    except WeishauptAuthError:
        return "invalid_auth"
    except WeishauptConnectionError:
        return "cannot_connect"
    except Exception:
        _LOGGER.exception("Unexpected exception")
        return "unknown"
    return None


class WeishauptWemConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Weishaupt WTC."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]

            # Check if already configured
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

            if error := await _validate_credentials(self, host, username, password):
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title=f"Weishaupt WTC ({host})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-detect the optional modules on explicit request."""
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            # Dropping the stored result makes the next setup probe again.
            data = {k: v for k, v in entry.data.items() if k != CONF_DETECTED_GROUPS}
            return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(step_id="reconfigure")

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start when the device rejects the stored credentials (HTTP 401)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for new credentials and check them before storing."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := await _validate_credentials(
                self,
                entry.data[CONF_HOST],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            ):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=entry.data.get(CONF_USERNAME, DEFAULT_USERNAME)
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={"host": entry.data[CONF_HOST]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> WeishauptOptionsFlow:
        """Return the options flow."""
        return WeishauptOptionsFlow()


class WeishauptOptionsFlow(OptionsFlowWithReload):
    """Change the scan interval after setup; the entry reloads when it changes."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the scan interval."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Required(CONF_SCAN_INTERVAL, default=current): SCAN_INTERVAL_VALIDATOR}
            ),
        )
