"""Tests for reauth, options, the fault and connection entities, and diagnostics."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
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
health = ha_stubs.load("health")
button = ha_stubs.load("button")
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


def make_flow(**entries):
    flow = config_flow.WeishauptWemConfigFlow()
    flow.hass = SimpleNamespace()
    for name, entry in entries.items():
        setattr(flow, name, entry)
    return flow


@unittest.skipIf(config_flow is None, "voluptuous is not installed")
class ReconfigureFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.entry = ha_stubs.ConfigEntry({**ENTRY_DATA, "detected_groups": ["hk"]})
        self.entry.unique_id = ENTRY_DATA["host"]
        self.flow = make_flow(reconfigure_entry=self.entry)
        self.flow.existing_entries = [self.entry]

    async def test_new_address_is_checked_and_stored_with_its_modules(self) -> None:
        with patch.object(api.WeishauptApiClient, "test_connection", AsyncMock(return_value=True)):
            result = await self.flow.async_step_reconfigure(
                {"host": "192.168.1.77", "redetect_modules": False}
            )

        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertEqual(self.entry.data["host"], "192.168.1.77")
        self.assertEqual(self.entry.unique_id, "192.168.1.77")
        self.assertEqual(self.entry.data["detected_groups"], ["hk"])  # no probes

    async def test_unreachable_new_address_is_not_stored(self) -> None:
        with patch.object(
            api.WeishauptApiClient,
            "test_connection",
            AsyncMock(side_effect=api.WeishauptConnectionError("timeout")),
        ):
            result = await self.flow.async_step_reconfigure(
                {"host": "192.168.1.77", "redetect_modules": False}
            )

        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        self.assertEqual(self.entry.data["host"], "192.168.1.50")

    async def test_redetect_without_address_change_sends_nothing_now(self) -> None:
        test_connection = AsyncMock()
        with patch.object(api.WeishauptApiClient, "test_connection", test_connection):
            await self.flow.async_step_reconfigure(
                {"host": "192.168.1.50", "redetect_modules": True}
            )

        test_connection.assert_not_awaited()
        self.assertNotIn("detected_groups", self.entry.data)  # probed on reload

    async def test_address_of_another_entry_is_refused(self) -> None:
        other = ha_stubs.ConfigEntry({**ENTRY_DATA, "host": "192.168.1.77"})
        other.unique_id = "192.168.1.77"
        self.flow.existing_entries.append(other)

        with self.assertRaises(ha_stubs.AbortFlow):
            await self.flow.async_step_reconfigure(
                {"host": "192.168.1.77", "redetect_modules": False}
            )


@unittest.skipIf(config_flow is None, "voluptuous is not installed")
class DhcpDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def discovery(self):
        return SimpleNamespace(ip="192.168.1.60", hostname="wem-sg", macaddress="aabbccddeeff")

    async def test_new_device_asks_for_credentials_then_creates_entry(self) -> None:
        flow = make_flow()

        form = await flow.async_step_dhcp(self.discovery())
        self.assertEqual(form["step_id"], "discovery_confirm")
        with patch.object(api.WeishauptApiClient, "test_connection", AsyncMock(return_value=True)):
            result = await flow.async_step_discovery_confirm(
                {"username": "admin", "password": "Admin123"}
            )

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["host"], "192.168.1.60")
        self.assertEqual(result["data"]["scan_interval"], 30)

    async def test_device_configured_by_hostname_or_ip_is_not_offered(self) -> None:
        for host in ("wem-sg", "192.168.1.60"):
            flow = make_flow()
            flow.existing_entries = [ha_stubs.ConfigEntry({**ENTRY_DATA, "host": host})]
            with self.assertRaises(ha_stubs.AbortFlow):
                await flow.async_step_dhcp(self.discovery())


@unittest.skipIf(config_flow is None, "voluptuous is not installed")
class OptionsFlowTests(unittest.IsolatedAsyncioTestCase):
    def make(self, options: dict) -> config_flow.WeishauptOptionsFlow:
        flow = config_flow.WeishauptOptionsFlow()
        flow.config_entry = ha_stubs.ConfigEntry(ENTRY_DATA, options)
        return flow

    async def test_form_defaults_to_the_current_interval(self) -> None:
        for options, expected in (({}, 30), ({"scan_interval": 90}, 90)):
            result = await self.make(options).async_step_init()
            key = next(k for k in result["data_schema"].schema if k == "scan_interval")
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


BERLIN = ZoneInfo("Europe/Berlin")


def device_time(hour: int, minute: int, day: int = 6, month: int = 11, year: int = 26) -> dict:
    values = {
        "sg_uhrzeit_stunden": hour,
        "sg_uhrzeit_minuten": minute,
        "sg_datum_tag": day,
        "sg_datum_monat": month,
        "sg_datum_jahr": year,
    }
    return {key: {"value_int": value} for key, value in values.items()}


class HealthTests(unittest.TestCase):
    def test_add_months_clamps_to_month_end(self) -> None:
        self.assertEqual(health.add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(health.add_months(date(2026, 11, 15), 12), date(2027, 11, 15))
        self.assertEqual(health.add_months(date(2026, 11, 15), 3), date(2027, 2, 15))

    def test_next_maintenance(self) -> None:
        self.assertIsNone(health.next_maintenance({}))
        self.assertIsNone(health.next_maintenance({"maintenance_interval_months": 0, "last_maintenance": "2026-01-10"}))
        self.assertIsNone(health.next_maintenance({"maintenance_interval_months": 12}))
        self.assertEqual(
            health.next_maintenance({"maintenance_interval_months": 12, "last_maintenance": "2026-01-10"}),
            date(2027, 1, 10),
        )

    def test_clock_drift(self) -> None:
        now = datetime(2026, 11, 6, 14, 30, tzinfo=BERLIN)
        self.assertAlmostEqual(health.clock_drift_minutes(device_time(14, 30), BERLIN, now), 0)
        self.assertAlmostEqual(health.clock_drift_minutes(device_time(15, 30), BERLIN, now), 60)
        self.assertIsNone(health.clock_drift_minutes({}, BERLIN, now))

    def test_active_fault(self) -> None:
        self.assertEqual(
            health.active_fault({"sg_canopen_fehlerblock": fault_block(0x0001, 31)}), ("Fehler", 31)
        )
        self.assertIsNone(health.active_fault({"sg_canopen_fehlerblock": fault_block(0x0002, 31)}))
        self.assertIsNone(health.active_fault({}))


class ScriptedReads:
    """Answers each poll with the next scripted register values."""

    suspended = False
    pause_remaining = 0

    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.calls: list[set[str]] = []

    async def read_parameters_with_status(self, params):
        keys = {param["key"] for param in params}
        self.calls.append(keys)
        reply = {key: value for key, value in self.replies.pop(0).items() if key in keys}
        return reply, set(reply)


class RepairIssueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        ha_stubs.issues.clear()
        self.now = 1000.0
        patcher = patch.object(coordinator_module.time, "monotonic", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.wall = datetime(2026, 11, 6, 14, 30, tzinfo=BERLIN)
        dt_patcher = patch.object(
            coordinator_module, "datetime", SimpleNamespace(now=lambda tz=None: self.wall)
        )
        dt_patcher.start()
        self.addCleanup(dt_patcher.stop)

    def make(self, client, options=None):
        coordinator = coordinator_module.WeishauptDataUpdateCoordinator(
            hass=SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Berlin")),
            client=client,
            scan_interval=30,
            active_groups=set(),
            config_entry=SimpleNamespace(
                entry_id="e1", title="Weishaupt WTC (wem-sg)", options=options or {}
            ),
        )
        coordinator._contexts = [frozenset({"wtc_kesseltemperatur", "sg_canopen_fehlerblock"})]
        return coordinator

    async def poll_at(self, coordinator, seconds: float) -> None:
        self.now = 1000.0 + seconds
        coordinator.data = await coordinator._async_update_data()

    async def test_fault_issue_follows_the_error_block(self) -> None:
        client = ScriptedReads([
            {"sg_canopen_fehlerblock": fault_block(0x0001, 31)},
            {"sg_canopen_fehlerblock": fault_block(0x0000, 0)},
        ])
        coordinator = self.make(client)

        await self.poll_at(coordinator, 0)
        issue = ha_stubs.issues["device_fault_e1"]
        self.assertEqual(issue["translation_placeholders"]["fehlernummer"], "31")
        self.assertEqual(issue["translation_placeholders"]["meldung"], "Fehler")

        await self.poll_at(coordinator, 60)
        self.assertNotIn("device_fault_e1", ha_stubs.issues)

    async def test_clock_is_checked_every_10_minutes_with_its_entity_disabled(self) -> None:
        client = ScriptedReads([device_time(14, 30)] * 3)
        coordinator = self.make(client)

        await self.poll_at(coordinator, 0)
        await self.poll_at(coordinator, 60)
        await self.poll_at(coordinator, 600)

        has_time = [set(health.DEVICE_TIME_KEYS) <= keys for keys in client.calls]
        self.assertEqual(has_time, [True, False, True])

    async def test_clock_drift_warns_after_two_readings_and_clears(self) -> None:
        client = ScriptedReads([device_time(15, 30), device_time(15, 30), device_time(14, 31)])
        coordinator = self.make(client)

        await self.poll_at(coordinator, 0)
        self.assertNotIn("clock_drift_e1", ha_stubs.issues)  # one reading may be midnight
        await self.poll_at(coordinator, 600)
        self.assertEqual(ha_stubs.issues["clock_drift_e1"]["translation_placeholders"]["minutes"], "60")
        await self.poll_at(coordinator, 1200)
        self.assertNotIn("clock_drift_e1", ha_stubs.issues)

    async def test_maintenance_issue_when_due(self) -> None:
        for last, due in (("2025-10-01", True), ("2026-10-01", False)):
            ha_stubs.issues.clear()
            coordinator = self.make(
                ScriptedReads([{}]),
                {"maintenance_interval_months": 12, "last_maintenance": last},
            )
            await self.poll_at(coordinator, 0)
            self.assertEqual("maintenance_due_e1" in ha_stubs.issues, due, last)


class MaintenanceEntityTests(unittest.IsolatedAsyncioTestCase):
    async def test_button_records_today_and_moves_the_due_date(self) -> None:
        entry = ha_stubs.ConfigEntry(
            ENTRY_DATA, {"maintenance_interval_months": 12, "last_maintenance": "2025-01-10"}
        )
        coordinator = SimpleNamespace(update_maintenance_issue=Mock(), async_update_listeners=Mock())
        press = button.WeishauptMaintenanceDoneButton(coordinator, entry)
        press.hass = SimpleNamespace(
            config=SimpleNamespace(time_zone="Europe/Berlin"),
            config_entries=SimpleNamespace(
                async_update_entry=lambda e, options: setattr(e, "options", options)
            ),
        )
        today = datetime.now(BERLIN).date()

        await press.async_press()

        self.assertEqual(entry.options["last_maintenance"], today.isoformat())
        self.assertEqual(entry.options["maintenance_interval_months"], 12)
        coordinator.update_maintenance_issue.assert_called_once()
        due = sensor.WeishauptNextMaintenanceSensor(coordinator, entry).native_value
        self.assertEqual(due, health.add_months(today, 12))


class RequestRateTests(unittest.IsolatedAsyncioTestCase):
    async def test_counts_requests_of_the_last_hour_including_failed_ones(self) -> None:
        client = api.WeishauptApiClient("wem-sg", "admin", "x", min_request_interval=0)
        client._post_once = AsyncMock(return_value={"CAPI": {"N01": {"VG": "020200253302000101"}}})
        params = [{"key": f"p{i}", "mi": 2, "mx": 0, "ox": 0x2533, "os": 2, "vs": 1} for i in range(8)]
        now = [1000.0]
        with patch.object(api.time, "monotonic", lambda: now[0]):
            await client.read_parameters(params)  # 8 registers -> 2 requests
            self.assertEqual(client.requests_last_hour, 2)

            client._post_once = AsyncMock(side_effect=api.WeishauptConnectionError("timeout"))
            with self.assertLogs("custom_components.weishaupt_wtc.api", level="WARNING"):
                with self.assertRaises(api.WeishauptConnectionError):
                    await client.read_parameters(params)
            self.assertEqual(client.requests_last_hour, 3)

            now[0] += 3601
            self.assertEqual(client.requests_last_hour, 0)

    def test_sensor_shows_the_count_and_stays_available(self) -> None:
        entity = sensor.WeishauptRequestRateSensor(
            SimpleNamespace(client=SimpleNamespace(requests_last_hour=42), last_update_success=False),
            ha_stubs.ConfigEntry(ENTRY_DATA),
        )
        self.assertEqual(entity.native_value, 42)
        self.assertTrue(entity.available)


class LowPressureTests(unittest.TestCase):
    def make(self, raw, options=None):
        data = {} if raw is None else {"wtc_anlagendruck": {"value_int": raw}}
        return binary_sensor.WeishauptLowPressureBinarySensor(
            SimpleNamespace(data=data, last_update_success=True),
            ha_stubs.ConfigEntry(ENTRY_DATA, options),
        )

    def test_threshold_default_and_option(self) -> None:
        self.assertTrue(self.make(95).is_on)  # 0.95 bar < 1.0 default
        self.assertFalse(self.make(150).is_on)
        self.assertTrue(self.make(150, {"pressure_min": 1.6}).is_on)
        self.assertEqual(
            self.make(95).extra_state_attributes, {"anlagendruck": 0.95, "schwelle": 1.0}
        )

    def test_unavailable_without_a_reading(self) -> None:
        for raw in (None, 0x8000):
            entity = self.make(raw)
            self.assertFalse(entity.available)
            self.assertIsNone(entity.is_on)

    def test_reads_only_the_pressure_register(self) -> None:
        self.assertEqual(self.make(150).coordinator_context, {"wtc_anlagendruck"})


class FaultParsingTests(unittest.TestCase):
    def test_is_fault(self) -> None:
        self.assertTrue(parsing.is_fault(0x0001))
        self.assertTrue(parsing.is_fault(0x0300))
        self.assertFalse(parsing.is_fault(0x0002))
        self.assertFalse(parsing.is_fault(0x0003))
        self.assertFalse(parsing.is_fault(0x0000))


if __name__ == "__main__":
    unittest.main()
