"""Regression tests for Weishaupt device registry metadata."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, call
from zoneinfo import ZoneInfo
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ha_stubs  # noqa: E402  (installs the Home Assistant stand-ins)

sensors = ha_stubs.load("sensors")
sensor = ha_stubs.load("sensor")
switch = ha_stubs.load("switch")
fake_device_registry = ha_stubs.fake_device_registry


class SensorDefinitionTests(unittest.TestCase):
    """Test register metadata used to poll sensor values."""

    def test_hk2_definitions_use_second_module_index(self) -> None:
        """Expose the documented HK2 registers through module index one."""
        self.assertEqual(
            [sensor_def.modbus_reg for sensor_def in sensors.HK_SENSORS],
            [
                "1030",
                "1031",
                "1032",
                "1033",
                "1036",
                "1037",
                "1038",
                "1039",
                "1040",
                "1041",
                "1042",
                "1043",
                "1045",
                "1046",
            ],
        )
        self.assertTrue(
            all(
                sensor_def.mx == 0x01
                and sensor_def.group is sensors.WeishauptDeviceGroup.HK
                for sensor_def in sensors.HK_SENSORS
            )
        )

    def test_hk3_definitions_assume_second_module_address(self) -> None:
        """Assume the second EM-HK module addresses itself as MX=2 (#11)."""
        self.assertEqual(len(sensors.HK3_SENSORS), len(sensors.HK_SENSORS))
        self.assertTrue(
            all(
                sensor_def.mx == 0x02
                and sensor_def.group is sensors.WeishauptDeviceGroup.HK3
                for sensor_def in sensors.HK3_SENSORS
            )
        )

    def test_date_components_use_year_month_day_byte_order(self) -> None:
        """Map the device date bytes to their corresponding components."""
        date_definitions = {
            sensor_def.key: (sensor_def.os, sensor_def.modbus_reg)
            for sensor_def in sensors.SG_SENSORS
            if sensor_def.key.startswith("sg_datum_")
        }

        self.assertEqual(
            date_definitions,
            {
                "sg_datum_jahr": (0x02, "153"),
                "sg_datum_monat": (0x03, "154"),
                "sg_datum_tag": (0x04, "155"),
            },
        )


class SensorTimestampTests(unittest.TestCase):
    """Test the consolidated device timestamp entity."""

    def test_device_time_uses_configured_timezone(self) -> None:
        """Return an aware datetime in Home Assistant's configured timezone."""
        sensor_def = next(
            sensor_def
            for sensor_def in sensors.SG_SENSORS
            if sensor_def.key == "sg_device_time"
        )
        entity = sensor.WeishauptSensorEntity(
            coordinator=SimpleNamespace(
                data={
                    "sg_uhrzeit_stunden": {"value_int": 13},
                    "sg_uhrzeit_minuten": {"value_int": 49},
                    "sg_datum_jahr": {"value_int": 26},
                    "sg_datum_monat": {"value_int": 8},
                    "sg_datum_tag": {"value_int": 29},
                }
            ),
            sensor_def=sensor_def,
            entry=SimpleNamespace(entry_id="entry-123"),
        )
        entity.hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Berlin"))

        self.assertEqual(
            entity.native_value,
            datetime(2026, 8, 29, 13, 49, tzinfo=ZoneInfo("Europe/Berlin")),
        )


class SensorPrecisionTests(unittest.TestCase):
    """Test rounding of scaled sensor values."""

    def test_wtc_istleistung_rounds_to_whole_percent(self) -> None:
        """Expose the modulation level as whole percent like the display."""
        sensor_def = next(
            sensor_def
            for sensor_def in sensors.WTC_SENSORS
            if sensor_def.key == "wtc_istleistung"
        )
        self.assertEqual(
            (sensor_def.mi, sensor_def.mx, sensor_def.ox, sensor_def.os, sensor_def.vs),
            (0x07, 0x00, 0x2534, 0x00, 2),
        )

        entity = sensor.WeishauptSensorEntity(
            coordinator=SimpleNamespace(
                data={"wtc_istleistung": {"value_int": 6819}}
            ),
            sensor_def=sensor_def,
            entry=SimpleNamespace(entry_id="entry-123"),
        )

        self.assertEqual(entity.native_value, 68)
        self.assertIsInstance(entity.native_value, int)
        self.assertEqual(entity._attr_suggested_display_precision, 0)


class SensorDeviceInfoTests(unittest.TestCase):
    """Test device registry metadata for sensor entities."""

    def test_system_device_has_no_via_device(self) -> None:
        """The SG device is the root of the device tree."""
        entity = sensor.WeishauptSensorEntity(
            coordinator=SimpleNamespace(data={}),
            sensor_def=sensors.SG_SENSORS[0],
            entry=SimpleNamespace(entry_id="entry-123"),
        )

        self.assertEqual(
            entity.device_info["identifiers"],
            {("weishaupt_wtc", "entry-123_sg")},
        )
        self.assertNotIn("via_device", entity.device_info)
        self.assertNotIn("via_device_id", entity.device_info)

    def test_child_device_points_to_system_device(self) -> None:
        """Non-SG groups should reference the SG device's registry id as via_device_id."""
        fake_device_registry.register(
            ("weishaupt_wtc", "entry-123_sg"), "entry-123", "sg-device-id"
        )
        entity = sensor.WeishauptSensorEntity(
            coordinator=SimpleNamespace(data={}),
            sensor_def=sensors.WTC_SENSORS[0],
            entry=SimpleNamespace(entry_id="entry-123"),
        )
        entity.hass = SimpleNamespace()

        self.assertEqual(
            entity.device_info["identifiers"],
            {("weishaupt_wtc", "entry-123_wtc")},
        )
        self.assertEqual(entity.device_info["via_device_id"], "sg-device-id")

    def test_hk2_device_is_named_as_second_heating_circuit(self) -> None:
        """Identify module index one as heating circuit two."""
        entity = sensor.WeishauptSensorEntity(
            coordinator=SimpleNamespace(data={}),
            sensor_def=sensors.HK_SENSORS[0],
            entry=SimpleNamespace(entry_id="entry-123"),
        )
        entity.hass = SimpleNamespace()

        self.assertEqual(entity.device_info["name"], "Weishaupt Heizkreis 2")


class WarmwasserSwitchTests(unittest.IsolatedAsyncioTestCase):
    """Test the Warmwasser on/off switch (WW0 object 0x2520: 1 Ein, 2 Aus)."""

    def setUp(self) -> None:
        self.coordinator = SimpleNamespace(
            data={},
            client=SimpleNamespace(write_parameter=AsyncMock(return_value=True)),
            async_request_refresh=AsyncMock(),
        )
        sensor_def = next(
            d for d in sensors.SG_SENSORS if d.key == "sg_betriebsart_warmwasser"
        )
        self.entity = switch.WeishauptWarmwasserSwitch(
            coordinator=self.coordinator,
            sensor_def=sensor_def,
            entry=SimpleNamespace(entry_id="entry-123"),
        )

    def test_is_on_maps_ein_aus_and_unknown_values(self) -> None:
        """Read Ein as on, Aus as off, and any other value as unknown."""
        for raw, expected in ((1, True), (2, False), (7, None)):
            self.coordinator.data = {"sg_betriebsart_warmwasser": {"value_int": raw}}
            self.assertIs(self.entity.is_on, expected)

        self.coordinator.data = {}
        self.assertIsNone(self.entity.is_on)

    async def test_turn_on_and_off_write_ein_aus_then_refresh(self) -> None:
        """Write 1 for on and 2 for off to MI 03 / OX 0x2520, then read back."""
        await self.entity.async_turn_on()
        await self.entity.async_turn_off()

        self.coordinator.client.write_parameter.assert_has_awaits(
            [
                call(mi=0x03, mx=0x00, ox=0x2520, os_val=0x02, vs=1, value_int=1),
                call(mi=0x03, mx=0x00, ox=0x2520, os_val=0x02, vs=1, value_int=2),
            ]
        )
        self.assertEqual(self.coordinator.async_request_refresh.await_count, 2)

    async def test_failed_write_is_logged_not_raised(self) -> None:
        """A failing client must not raise into HA; state is still refreshed."""
        self.coordinator.client.write_parameter.side_effect = RuntimeError("offline")

        with self.assertLogs("custom_components.weishaupt_wtc.switch", level="ERROR"):
            await self.entity.async_turn_off()

        self.coordinator.async_request_refresh.assert_awaited_once()

    async def test_blocked_write_is_shown_to_the_user(self) -> None:
        """Paused or throttled: nothing was sent, so say so instead of reading back."""
        self.coordinator.client.write_parameter.side_effect = (
            switch.WeishauptRequestBlockedError("Wait 42 s")
        )

        with self.assertRaises(switch.HomeAssistantError):
            await self.entity.async_turn_off()

        self.coordinator.async_request_refresh.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
