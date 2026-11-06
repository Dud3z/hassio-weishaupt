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
from homeassistant.helpers import selector
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
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
    CONF_LAST_MAINTENANCE,
    CONF_MAINTENANCE_INTERVAL,
    CONF_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

CONF_REDETECT_MODULES = "redetect_modules"

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
        """Change the device address and/or re-detect the optional modules."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            data = dict(entry.data)
            if host != entry.data[CONF_HOST]:
                # A new address keeps entities, history and automations; check it first.
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()
                if error := await _validate_credentials(
                    self, host, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
                ):
                    errors["base"] = error
                data[CONF_HOST] = host
            if user_input[CONF_REDETECT_MODULES]:
                # Dropping the stored result makes the next setup probe again.
                data.pop(CONF_DETECTED_GROUPS, None)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=host,
                    title=f"Weishaupt WTC ({host})",
                    data=data,
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str,
                    vol.Required(CONF_REDETECT_MODULES, default=False): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Offer setup when a Systemgerät (hostname wem-sg*) joins the network."""
        host = discovery_info.ip
        # Entries store whatever the user typed: the IP address or the hostname.
        for known in (host, discovery_info.hostname):
            self._async_abort_entries_match({CONF_HOST: known})
        await self.async_set_unique_id(host)
        self._abort_if_unique_id_configured()
        self._discovered_host = host
        self.context["title_placeholders"] = {"host": host}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the credentials of a discovered Systemgerät."""
        host = self._discovered_host
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := await _validate_credentials(
                self, host, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            ):
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title=f"Weishaupt WTC ({host})",
                    data={CONF_HOST: host, CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL, **user_input},
                )
        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
                    vol.Required(CONF_PASSWORD, default=DEFAULT_PASSWORD): str,
                }
            ),
            description_placeholders={"host": host},
            errors=errors,
        )

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
        options = self.config_entry.options
        current = options.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): SCAN_INTERVAL_VALIDATOR,
                    # 0 turns the maintenance reminder off.
                    vol.Required(
                        CONF_MAINTENANCE_INTERVAL,
                        default=options.get(CONF_MAINTENANCE_INTERVAL, 0),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=60)),
                    vol.Optional(
                        CONF_LAST_MAINTENANCE,
                        description={"suggested_value": options.get(CONF_LAST_MAINTENANCE)},
                    ): selector.DateSelector(),
                }
            ),
        )
