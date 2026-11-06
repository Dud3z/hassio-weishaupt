"""Binary sensor platform for Weishaupt WTC integration.

Exposes the fault state of the CANopen error block (Modbus 120-123) as a
problem sensor, so a "heating reports a fault" automation needs no template.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import WeishauptDataUpdateCoordinator
from .parsing import decode_fault_status, extract_value_segment, is_fault
from .sensor import _system_device_identifier

FAULT_BLOCK_KEY = "sg_canopen_fehlerblock"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Weishaupt fault binary sensor."""
    async_add_entities([WeishauptFaultBinarySensor(entry.runtime_data, entry)])


class WeishauptFaultBinarySensor(
    CoordinatorEntity[WeishauptDataUpdateCoordinator], BinarySensorEntity
):
    """On while the Systemgerät reports an error (register 121)."""

    _attr_has_entity_name = True
    _attr_translation_key = "sg_stoerung"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self, coordinator: WeishauptDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the fault sensor."""
        super().__init__(coordinator, context=frozenset({FAULT_BLOCK_KEY}))
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sg_stoerung"

    @property
    def device_info(self) -> DeviceInfo:
        """Attach to the Systemgerät."""
        return DeviceInfo(identifiers={_system_device_identifier(self._entry.entry_id)})

    def _segment(self, byte_offset: int) -> int | None:
        """Return a 16-bit value from the error block (121 at offset 2, 122 at 4)."""
        data = (self.coordinator.data or {}).get(FAULT_BLOCK_KEY)
        if not data:
            return None
        extracted = extract_value_segment(data.get("value_hex", ""), byte_offset, 2)
        return None if extracted is None else extracted[1]

    @property
    def available(self) -> bool:
        """Available while the error block was read."""
        return super().available and self._segment(2) is not None

    @property
    def is_on(self) -> bool | None:
        """Return True while an error (not a warning or info) is reported."""
        status = self._segment(2)
        return None if status is None else is_fault(status)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the message type and error number."""
        status = self._segment(2)
        return {
            "meldung": None if status is None else decode_fault_status(status),
            "fehlernummer": self._segment(4),
        }
