"""Tests for reauth, options, the fault and connection entities, and diagnostics."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ha_stubs  # noqa: E402  (installs the Home Assistant stand-ins)

api = ha_stubs.load("api")
parsing = ha_stubs.load("parsing")
sensors = ha_stubs.load("sensors")
coordinator_module = ha_stubs.load("coordinator")
sensor = ha_stubs.load("sensor")
binary_sensor = ha_stubs.load("binary_sensor")
switch = ha_stubs.load("switch")
diagnostics = ha_stubs.load("diagnostics")
try:
    config_flow = ha_stubs.load("config_flow")
except ModuleNotFoundError:  # voluptuous is not installed
    config_flow = None

ENTRY_DATA = {
    "host": "192.168.1.50",
    "username": "admin",
    "password": "secret",
    "scan_interval": 30,
}


def fault_block(status: int, number: int) -> dict:
    """Error block reply: register 120 (unused here), 121 status, 122 number, 123 module."""
    value_hex = f"0000{status:04x}{number:04x}0000"
    return {"value_hex": value_hex, "value_int": int(value_hex, 16)}


class CoordinatorAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_credentials_start_reauth(self) -> None:
        """HTTP 401 becomes ConfigEntryAuthFailed, which makes HA ask for new credentials."""
        client = SimpleNamespace(
            read_parameters_with_status=AsyncMock(side_effect=api.WeishauptAuthError("401")),
            pause_remaining=0,
            suspended=False,
        )
        coordinator = coordinator_module.WeishauptDataUpdateCoordinator(
            hass=SimpleNamespace(), client=client, scan_interval=30, active_groups=set()
        )

        with self.assertRaises(ha_stubs.ConfigEntryAuthFailed):
            await coordinator._async_update_data()


class FaultBinarySensorTests(unittest.TestCase):
    def make(self, data):
        coordinator = SimpleNamespace(data=data, last_update_success=True)
        return binary_sensor.WeishauptFaultBinarySensor(
            coordinator, ha_stubs.ConfigEntry(ENTRY_DATA)
        )

    def test_error_is_on_warning_and_none_are_off(self) -> None:
        for status, expected in ((0x0001, True), (0x0100, True), (0x0002, False), (0x0000, False)):
            entity = self.make({"sg_canopen_fehlerblock": fault_block(status, 42)})
            self.assertIs(entity.is_on, expected, hex(status))

        entity = self.make({"sg_canopen_fehlerblock": fault_block(0x0001, 42)})
        self.assertEqual(entity.extra_state_attributes, {"meldung": "Fehler", "fehlernummer": 42})

    def test_unavailable_without_the_error_block(self) -> None:
        entity = self.make({})

        self.assertFalse(entity.available)
        self.assertIsNone(entity.is_on)

    def test_reads_only_the_error_block(self) -> None:
        self.assertEqual(self.make({}).coordinator_context, {"sg_canopen_fehlerblock"})


class ConnectionSensorTests(unittest.TestCase):
    def make(self, success: bool, pause: float, error: Exception | None = None):
        coordinator = SimpleNamespace(
            last_update_success=success,
            last_exception=error,
            client=SimpleNamespace(pause_remaining=pause, suspended=False),
        )
        return sensor.WeishauptConnectionSensor(coordinator, ha_stubs.ConfigEntry(ENTRY_DATA))

    def test_states(self) -> None:
        ok = self.make(True, 0)
        paused = self.make(False, 240, api.WeishauptEmptyResponseError("empty"))
        failed = self.make(False, 0, api.WeishauptApiError("boom"))

        self.assertEqual(ok.native_value, "ok")
        self.assertIsNone(ok.extra_state_attributes["paused_until"])
        self.assertEqual(paused.native_value, "paused")
        self.assertIsNotNone(paused.extra_state_attributes["paused_until"])
        self.assertEqual(failed.native_value, "error")
        self.assertEqual(failed.extra_state_attributes["last_error"], "boom")

    def test_stays_available_while_the_device_fails(self) -> None:
        self.assertTrue(self.make(False, 240).available)


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_credentials_and_address_are_redacted(self) -> None:
        entry = ha_stubs.ConfigEntry(ENTRY_DATA)
        entry.runtime_data = SimpleNamespace(
            active_groups={sensors.WeishauptDeviceGroup.HK},
            last_update_success=False,
            last_exception=api.WeishauptConnectionError(
                "Cannot connect to Weishaupt device at 192.168.1.50: refused"
            ),
            client=SimpleNamespace(pause_remaining=119.6),
            data={"wtc_kesseltemperatur": {"value_hex": "01c2", "value_int": 450, "cmd": 2}},
        )

        result = await diagnostics.async_get_config_entry_diagnostics(None, entry)

        text = str(result)
        for secret in ("192.168.1.50", "secret", "admin"):
            self.assertNotIn(secret, text)
        self.assertEqual(result["active_groups"], ["hk"])
        self.assertEqual(result["connection"]["pause_remaining_s"], 120)
        self.assertEqual(
            result["registers"]["wtc_kesseltemperatur"], {"value_hex": "01c2", "value_int": 450}
        )


@unittest.skipIf(config_flow is None, "voluptuous is not installed")
class ReauthFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.flow = config_flow.WeishauptWemConfigFlow()
        self.flow.hass = SimpleNamespace()
        self.flow.reauth_entry = ha_stubs.ConfigEntry(ENTRY_DATA)

    async def test_wrong_password_shows_error_and_keeps_old_credentials(self) -> None:
        with patch.object(
            api.WeishauptApiClient,
            "test_connection",
            AsyncMock(side_effect=api.WeishauptAuthError("401")),
        ):
            result = await self.flow.async_step_reauth_confirm(
                {"username": "admin", "password": "wrong"}
            )

        self.assertEqual(result["errors"], {"base": "invalid_auth"})
        self.assertEqual(self.flow.reauth_entry.data["password"], "secret")

    async def test_accepted_credentials_are_stored(self) -> None:
        with patch.object(api.WeishauptApiClient, "test_connection", AsyncMock(return_value=True)):
            result = await self.flow.async_step_reauth_confirm(
                {"username": "admin", "password": "new"}
            )

        self.assertEqual(result["reason"], "reauth_successful")
        self.assertEqual(self.flow.reauth_entry.data["password"], "new")
        self.assertEqual(self.flow.reauth_entry.data["host"], "192.168.1.50")


@unittest.skipIf(config_flow is None, "voluptuous is not installed")
class OptionsFlowTests(unittest.IsolatedAsyncioTestCase):
    def make(self, options: dict) -> config_flow.WeishauptOptionsFlow:
        flow = config_flow.WeishauptOptionsFlow()
        flow.config_entry = ha_stubs.ConfigEntry(ENTRY_DATA, options)
        return flow

    async def test_form_defaults_to_the_current_interval(self) -> None:
        for options, expected in (({}, 30), ({"scan_interval": 90}, 90)):
            result = await self.make(options).async_step_init()
            (key,) = result["data_schema"].schema
            self.assertEqual(key.default(), expected)

    async def test_out_of_range_interval_is_rejected(self) -> None:
        result = await self.make({}).async_step_init()

        with self.assertRaises(Exception):
            result["data_schema"]({"scan_interval": 5})

    async def test_new_interval_is_stored_as_option(self) -> None:
        result = await self.make({}).async_step_init({"scan_interval": 120})

        self.assertEqual(result["data"], {"scan_interval": 120})


class PauseSwitchTests(unittest.IsolatedAsyncioTestCase):
    """The pause switch stops every request and survives a restart."""

    async def asyncSetUp(self) -> None:
        self.client = api.WeishauptApiClient("wem-sg", "admin", "x", min_request_interval=0)
        self.client._post_once = AsyncMock(return_value={"CAPI": {}})
        self.entry = ha_stubs.ConfigEntry(ENTRY_DATA)
        self.coordinator = SimpleNamespace(
            client=self.client,
            async_request_refresh=AsyncMock(),
            async_update_listeners=Mock(),
        )
        self.switch = switch.WeishauptPauseSwitch(self.coordinator, self.entry)
        self.switch.hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_update_entry=lambda entry, data: entry.data.update(data)
            )
        )
        self.switch.async_write_ha_state = Mock()

    async def test_paused_client_sends_nothing(self) -> None:
        await self.switch.async_turn_on()

        self.assertTrue(self.switch.is_on)
        self.assertTrue(self.entry.data["suspended"])
        with self.assertRaises(api.WeishauptRequestBlockedError):
            await self.client.read_parameters([{"key": "k", "mi": 2, "mx": 0, "ox": 1, "os": 2, "vs": 1}])
        with self.assertRaises(api.WeishauptRequestBlockedError):
            await self.client.write_parameter(0x03, 0x00, 0x2520, 0x02, 1, 1)
        self.client._post_once.assert_not_awaited()

    async def test_resume_reads_right_away(self) -> None:
        await self.switch.async_turn_on()
        await self.switch.async_turn_off()

        self.assertFalse(self.switch.is_on)
        self.assertFalse(self.entry.data["suspended"])
        self.coordinator.async_request_refresh.assert_awaited_once()

    def test_stays_available(self) -> None:
        self.assertTrue(self.switch.available)

    async def test_coordinator_keeps_last_values_while_paused(self) -> None:
        self.client.suspended = True
        client_read = AsyncMock()
        self.client.read_parameters_with_status = client_read
        coordinator = coordinator_module.WeishauptDataUpdateCoordinator(
            hass=SimpleNamespace(), client=self.client, scan_interval=30, active_groups=set()
        )
        coordinator.data = {"wtc_kesseltemperatur": {"value_int": 450}}

        self.assertEqual(await coordinator._async_update_data(), coordinator.data)
        client_read.assert_not_awaited()

    def test_connection_sensor_shows_the_pause(self) -> None:
        self.client.suspended = True
        sensor_entity = sensor.WeishauptConnectionSensor(
            SimpleNamespace(last_update_success=True, last_exception=None, client=self.client),
            self.entry,
        )
        self.assertEqual(sensor_entity.native_value, "suspended")


class FaultParsingTests(unittest.TestCase):
    def test_is_fault(self) -> None:
        self.assertTrue(parsing.is_fault(0x0001))
        self.assertTrue(parsing.is_fault(0x0300))
        self.assertFalse(parsing.is_fault(0x0002))
        self.assertFalse(parsing.is_fault(0x0003))
        self.assertFalse(parsing.is_fault(0x0000))


if __name__ == "__main__":
    unittest.main()
