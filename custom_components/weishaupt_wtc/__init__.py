"""The Weishaupt WTC integration."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WeishauptApiClient
from .const import (
    CONF_DETECTED_GROUPS,
    CONF_SCAN_INTERVAL,
    CONF_SUSPENDED,
    DEFAULT_SCAN_INTERVAL,
)
from .coordinator import WeishauptConfigEntry, WeishauptDataUpdateCoordinator
from .sensors import WeishauptDeviceGroup

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.BUTTON,
    Platform.SWITCH,
]


def _store_detected_groups(
    hass: HomeAssistant, entry: WeishauptConfigEntry, groups: set[WeishauptDeviceGroup]
) -> None:
    """Persist detected optional groups so restarts and reloads don't probe again."""
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_DETECTED_GROUPS: sorted(group.value for group in groups)},
    )


async def async_setup_entry(hass: HomeAssistant, entry: WeishauptConfigEntry) -> bool:
    """Set up Weishaupt WTC from a config entry."""
    host = entry.data[CONF_HOST]
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    # The options flow overrides the scan interval chosen during setup.
    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )

    session = async_get_clientsession(hass)
    client = WeishauptApiClient(
        host=host,
        username=username,
        password=password,
        session=session,
    )

    # Restored before the first refresh, so a paused integration sends nothing on startup.
    client.suspended = entry.data.get(CONF_SUSPENDED, False)

    stored = entry.data.get(CONF_DETECTED_GROUPS)
    coordinator = WeishauptDataUpdateCoordinator(
        hass=hass,
        client=client,
        scan_interval=scan_interval,
        active_groups=None
        if stored is None
        else {WeishauptDeviceGroup(value) for value in stored},
        on_groups_detected=lambda groups: _store_detected_groups(hass, entry, groups),
        config_entry=entry,
    )

    # shortcut: a failed first refresh is retried by HA's setup retry with a fresh client, so the backoff pause does not carry over (one request per retry), upgrade to reusing the client across setup retries if the WEM locks up during startup
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: WeishauptConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # A reload or removal starts fresh; issues that still apply come back.
        entry.runtime_data.delete_issues()
    return unload_ok
