"""Switch platform for Weishaupt WTC integration.

Exposes the Warmwasser on/off object (MI 03 / OX 0x2520) as a Home Assistant
switch, and a switch that pauses all requests to the device.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import WeishauptRequestBlockedError
from .const import CONF_SUSPENDED
from .coordinator import WeishauptDataUpdateCoordinator
from .sensor import DEVICE_GROUP_MODELS, DEVICE_GROUP_NAMES, _device_identifier, _is_group_active, _via_device_id
from .sensors import ALL_SENSORS, WeishauptDeviceGroup, WeishauptSensorDefinition

_LOGGER = logging.getLogger(__name__)

# One write at a time; the client also allows only one per minute.
PARALLEL_UPDATES = 1

SWITCH_KEY = "sg_betriebsart_warmwasser"
WW_EIN = 1  # Betriebsart Warmwasser values: 1 = Ein, 2 = Aus
WW_AUS = 2


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Weishaupt Switch entities from a config entry."""
    coordinator = entry.runtime_data

    entities: list[SwitchEntity] = [WeishauptPauseSwitch(coordinator, entry)]
    for sensor_def in ALL_SENSORS:
        if sensor_def.key == SWITCH_KEY and _is_group_active(
            coordinator, sensor_def.group
        ):
            entities.append(
                WeishauptWarmwasserSwitch(
                    coordinator=coordinator, sensor_def=sensor_def, entry=entry
                )
            )

    async_add_entities(entities)


class WeishauptWarmwasserSwitch(CoordinatorEntity, SwitchEntity):
    """Warmwasser on/off (Betriebsart Ein/Aus) as a switch."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        sensor_def: WeishauptSensorDefinition,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, context=frozenset({sensor_def.key}))
        self._sensor_def = sensor_def
        self._entry = entry

        self._attr_unique_id = f"{entry.entry_id}_{sensor_def.key}"
        self._attr_translation_key = sensor_def.key

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info so the entity is attached to the correct device."""
        group = self._sensor_def.group
        info = DeviceInfo(
            identifiers={_device_identifier(self._entry.entry_id, group)},
            name=f"Weishaupt {DEVICE_GROUP_NAMES.get(group, group.value)}",
            manufacturer="Weishaupt",
            model=DEVICE_GROUP_MODELS.get(group, "Unknown"),
        )
        if group is not WeishauptDeviceGroup.SG:
            via_device_id = _via_device_id(self.hass, self._entry.entry_id)
            if via_device_id is not None:
                info["via_device_id"] = via_device_id
        return info

    @property
    def is_on(self) -> bool | None:
        """Return True for Ein, False for Aus, None for any other value."""
        data = (self.coordinator.data or {}).get(self._sensor_def.key)
        if not data:
            return None
        return {WW_EIN: True, WW_AUS: False}.get(data["value_int"])

    async def _write_betriebsart(self, value: int) -> None:
        try:
            await self.coordinator.client.write_parameter(
                mi=self._sensor_def.mi,
                mx=self._sensor_def.mx,
                ox=self._sensor_def.ox,
                os_val=self._sensor_def.os,
                vs=self._sensor_def.vs,
                value_int=value,
            )
        except WeishauptRequestBlockedError as err:
            raise HomeAssistantError(str(err)) from err  # nothing was sent
        except Exception as err:  # catch client errors
            _LOGGER.error("Failed to write %s=%s: %s", self._sensor_def.key, value, err)
        # Read back even after a failed write: a write can apply despite an empty reply.
        # After an empty reply the client pauses, so the read-back waits for the pause.
        self.coordinator.invalidate({self._sensor_def.key})
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn Warmwasser on (Betriebsart Ein)."""
        await self._write_betriebsart(WW_EIN)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn Warmwasser off (Betriebsart Aus)."""
        await self._write_betriebsart(WW_AUS)


class WeishauptPauseSwitch(
    CoordinatorEntity[WeishauptDataUpdateCoordinator], SwitchEntity
):
    """Stop all requests to the device, e.g. while the WEM app or a technician uses it.

    The WEM handles only one client at a time. While on, the last values stay
    and switching is refused. Stored in the config entry, so it already holds
    for the first refresh after a restart.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "abfragen_pausieren"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: WeishauptDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the pause switch."""
        super().__init__(coordinator, context=frozenset())  # reads no register
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_abfragen_pausieren"

    @property
    def device_info(self) -> DeviceInfo:
        """Attach to the Systemgerät."""
        return DeviceInfo(
            identifiers={_device_identifier(self._entry.entry_id, WeishauptDeviceGroup.SG)}
        )

    @property
    def available(self) -> bool:
        """Stay available, so the pause can be lifted while the device fails."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True while requests are paused."""
        return self.coordinator.client.suspended

    async def _set(self, suspended: bool) -> None:
        self.coordinator.client.suspended = suspended
        self.hass.config_entries.async_update_entry(
            self._entry, data={**self._entry.data, CONF_SUSPENDED: suspended}
        )
        if suspended:
            self.coordinator.async_update_listeners()  # the connection sensor shows it
        else:
            await self.coordinator.async_request_refresh()
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Pause all requests."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Resume requests and read right away."""
        await self._set(False)
