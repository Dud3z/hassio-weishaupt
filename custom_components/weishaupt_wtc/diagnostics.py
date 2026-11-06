"""Diagnostics download for Weishaupt WTC: raw register values and connection state."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .coordinator import WeishauptConfigEntry

TO_REDACT = {CONF_HOST, CONF_PASSWORD, CONF_USERNAME}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WeishauptConfigEntry
) -> dict[str, Any]:
    """Return what helps with a bug report, without credentials or the address."""
    coordinator = entry.runtime_data
    active_groups = coordinator.active_groups
    error = coordinator.last_exception
    # Error messages name the device address, which is redacted from the data too.
    last_error = None if error is None else str(error).replace(entry.data[CONF_HOST], "**REDACTED**")
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "active_groups": None
        if active_groups is None
        else sorted(group.value for group in active_groups),
        "connection": {
            "last_update_success": coordinator.last_update_success,
            "last_error": last_error,
            "pause_remaining_s": round(coordinator.client.pause_remaining),
        },
        # Raw replies, so a wrong scale or byte order can be checked against them.
        "registers": {
            key: {"value_hex": value.get("value_hex"), "value_int": value.get("value_int")}
            for key, value in sorted((coordinator.data or {}).items())
        },
    }
