"""Button platform for Weishaupt WTC integration.

Exposes one-shot writable registers as press buttons in Home Assistant.
"""

from __future__ import annotations

from datetime import datetime
import logging
from zoneinfo import ZoneInfo

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import WeishauptRequestBlockedError
from .const import CONF_LAST_MAINTENANCE, CONF_MAINTENANCE_INTERVAL
from .coordinator import WeishauptDataUpdateCoordinator
from .sensor import DEVICE_GROUP_MODELS, DEVICE_GROUP_NAMES, _device_identifier, _is_group_active, _via_device_id
from .sensors import ALL_SENSORS, WeishauptDeviceGroup, WeishauptSensorDefinition

_LOGGER = logging.getLogger(__name__)

# One write at a time; the client also allows only one per minute.
PARALLEL_UPDATES = 1

# Keys that are exposed as buttons rather than sensors
BUTTON_KEYS = {"sg_warmwasser_push"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Weishaupt Button entities from a config entry."""
    coordinator = entry.runtime_data

    entities: list[ButtonEntity] = []
    if entry.options.get(CONF_MAINTENANCE_INTERVAL):
        entities.append(WeishauptMaintenanceDoneButton(coordinator, entry))
    for sensor_def in ALL_SENSORS:
        if sensor_def.key in BUTTON_KEYS and _is_group_active(
            coordinator, sensor_def.group
        ):
            entities.append(
                WeishauptButtonEntity(
                    coordinator=coordinator, sensor_def=sensor_def, entry=entry
                )
            )

    async_add_entities(entities)


class WeishauptButtonEntity(CoordinatorEntity, ButtonEntity):
    """Representation of a one-shot Weishaupt writable register as a Button."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        sensor_def: WeishauptSensorDefinition,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, context=frozenset())  # shows no register value
        self._sensor_def = sensor_def
        self._entry = entry

        self._attr_unique_id = f"{entry.entry_id}_{sensor_def.key}"
        self._attr_translation_key = sensor_def.key
        self._attr_icon = sensor_def.icon

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

    async def async_press(self) -> None:
        """Trigger the warm-water push by writing 1 to the register."""
        try:
            success = await self.coordinator.client.write_parameter(
                mi=self._sensor_def.mi,
                mx=self._sensor_def.mx,
                ox=self._sensor_def.ox,
                os_val=self._sensor_def.os,
                vs=self._sensor_def.vs,
                value_int=1,
            )
        except WeishauptRequestBlockedError as err:
            raise HomeAssistantError(str(err)) from err  # nothing was sent
        except Exception as err:
            _LOGGER.error(
                "Failed to trigger %s: %s", self._sensor_def.key, err
            )
            return

        if success:
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.debug("Write reported unsuccessful for %s", self._sensor_def.key)


class WeishauptMaintenanceDoneButton(
    CoordinatorEntity[WeishauptDataUpdateCoordinator], ButtonEntity
):
    """Record today as the last maintenance; the next due date moves on."""

    _attr_has_entity_name = True
    _attr_translation_key = "wartung_erledigt"

    def __init__(
        self, coordinator: WeishauptDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the maintenance button."""
        super().__init__(coordinator, context=frozenset())  # reads no register
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_wartung_erledigt"

    @property
    def device_info(self) -> DeviceInfo:
        """Attach to the Systemgerät."""
        return DeviceInfo(
            identifiers={_device_identifier(self._entry.entry_id, WeishauptDeviceGroup.SG)}
        )

    @property
    def available(self) -> bool:
        """Independent of the device: only the options change."""
        return True

    async def async_press(self) -> None:
        """Store today (Home Assistant's time zone) as the last maintenance."""
        today = datetime.now(ZoneInfo(self.hass.config.time_zone)).date()
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, CONF_LAST_MAINTENANCE: today.isoformat()},
        )
        self.coordinator.update_maintenance_issue()
        self.coordinator.async_update_listeners()  # the due-date sensor moves on
