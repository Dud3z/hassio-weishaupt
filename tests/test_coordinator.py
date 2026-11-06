"""Regression tests for optional device group auto-detection in the coordinator."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ha_stubs  # noqa: E402  (installs the Home Assistant stand-ins)

sensors = ha_stubs.load("sensors")
coordinator_module = ha_stubs.load("coordinator")
WeishauptConnectionError = ha_stubs.load("api").WeishauptConnectionError
UpdateFailed = ha_stubs.UpdateFailed


class FakeClient:
    """Records the params passed to the read and returns canned (results, answered)."""

    suspended = False

    def __init__(self, responses: list[tuple[dict, set]]) -> None:
        self._responses = responses
        self.calls: list[list[dict]] = []

    async def read_parameters_with_status(self, params: list[dict]) -> tuple[dict, set]:
        self.calls.append(params)
        return self._responses[len(self.calls) - 1]


class CoordinatorOptionalGroupDetectionTests(unittest.IsolatedAsyncioTestCase):
    """Verify optional device groups are detected once and then not re-probed."""

    async def test_undetected_optional_groups_are_skipped_on_next_poll(self) -> None:
        """Only groups that answered on the first poll stay in later polls."""
        hk_key = sensors.HK_SENSORS[0].key
        sol_key = sensors.SOL_SENSORS[0].key
        hk3_key = sensors.HK3_SENSORS[0].key
        sg_key = sensors.SG_SENSORS[0].key

        # First poll: HK responds, SOL and HK3 do not (CMD_ERROR, so they are answered).
        first_response = {
            sg_key: {"value_int": 0},
            hk_key: {"value_int": 0},
        }
        answered = {sg_key, hk_key, sol_key, hk3_key}
        client = FakeClient(responses=[(first_response, answered), ({}, set())])
        coordinator = coordinator_module.WeishauptDataUpdateCoordinator(
            hass=SimpleNamespace(), client=client, scan_interval=30
        )

        self.assertIsNone(coordinator.active_groups)
        await coordinator._async_update_data()

        self.assertEqual(coordinator.active_groups, {sensors.WeishauptDeviceGroup.HK})

        first_call_keys = {param["key"] for param in client.calls[0]}
        first_optional_keys = first_call_keys & {hk_key, sol_key, hk3_key}
        self.assertEqual(first_optional_keys, {hk_key, sol_key, hk3_key})
        self.assertEqual(
            sum(
                param["key"] in {hk_key, sol_key, hk3_key}
                for param in client.calls[0]
            ),
            3,
        )

        coordinator._last_read.clear()  # every register is due again
        await coordinator._async_update_data()

        second_call_keys = {param["key"] for param in client.calls[1]}
        self.assertIn(sg_key, second_call_keys)
        self.assertIn(hk_key, second_call_keys)
        self.assertNotIn(sol_key, second_call_keys)
        self.assertNotIn(hk3_key, second_call_keys)

        # Detection only happens once; it doesn't get re-evaluated afterward.
        self.assertEqual(coordinator.active_groups, {sensors.WeishauptDeviceGroup.HK})

    async def test_complete_detection_is_handed_to_persist_callback(self) -> None:
        """Every probe answered (value or CMD_ERROR) means the result can be stored."""
        hk_key = sensors.HK_SENSORS[0].key
        sol_key = sensors.SOL_SENSORS[0].key
        hk3_key = sensors.HK3_SENSORS[0].key
        sg_key = sensors.SG_SENSORS[0].key
        client = FakeClient(
            responses=[
                (
                    {sg_key: {"value_int": 0}, hk_key: {"value_int": 0}},
                    {sg_key, hk_key, sol_key, hk3_key},
                )
            ]
        )
        stored: list[set] = []
        coordinator = coordinator_module.WeishauptDataUpdateCoordinator(
            hass=SimpleNamespace(),
            client=client,
            scan_interval=30,
            on_groups_detected=lambda groups: stored.append(set(groups)),
        )

        await coordinator._async_update_data()

        self.assertEqual(stored, [{sensors.WeishauptDeviceGroup.HK}])

    async def test_unanswered_probe_is_not_stored(self) -> None:
        """A probe without any reply says nothing about its module, so it is not persisted."""
        hk_key = sensors.HK_SENSORS[0].key
        sg_key = sensors.SG_SENSORS[0].key
        client = FakeClient(
            responses=[
                ({sg_key: {"value_int": 0}, hk_key: {"value_int": 0}}, {sg_key, hk_key})
            ]
        )
        stored: list[set] = []
        coordinator = coordinator_module.WeishauptDataUpdateCoordinator(
            hass=SimpleNamespace(),
            client=client,
            scan_interval=30,
            on_groups_detected=lambda groups: stored.append(set(groups)),
        )

        await coordinator._async_update_data()

        self.assertEqual(coordinator.active_groups, {sensors.WeishauptDeviceGroup.HK})
        self.assertEqual(stored, [])

    async def test_stored_groups_are_polled_without_probes(self) -> None:
        """A stored detection result skips probing: only stored optional groups are read."""
        hk_key = sensors.HK_SENSORS[0].key
        sol_key = sensors.SOL_SENSORS[0].key
        hk3_key = sensors.HK3_SENSORS[0].key
        sg_key = sensors.SG_SENSORS[0].key
        client = FakeClient(
            responses=[
                ({sg_key: {"value_int": 0}, hk_key: {"value_int": 0}}, {sg_key, hk_key})
            ]
        )
        stored: list[set] = []
        coordinator = coordinator_module.WeishauptDataUpdateCoordinator(
            hass=SimpleNamespace(),
            client=client,
            scan_interval=30,
            active_groups={sensors.WeishauptDeviceGroup.HK},
            on_groups_detected=lambda groups: stored.append(set(groups)),
        )

        await coordinator._async_update_data()

        keys = {param["key"] for param in client.calls[0]}
        self.assertIn(hk_key, keys)
        self.assertFalse(keys & {sol_key, hk3_key})
        self.assertEqual(stored, [])


class PausingClient:
    """Fails the first read and reports a pause, like the real client's backoff."""

    suspended = False

    def __init__(self, pause_remaining: float) -> None:
        self.pause_remaining = pause_remaining
        self.reads = 0

    async def read_parameters_with_status(self, params: list[dict]) -> tuple[dict, set]:
        self.reads += 1
        if self.reads == 1:
            raise WeishauptConnectionError("empty reply")
        return {params[0]["key"]: {"value_int": 0}}, {params[0]["key"]}


class CoordinatorBackoffTests(unittest.IsolatedAsyncioTestCase):
    """The coordinator waits out the client's pause instead of polling every scan interval."""

    async def test_failed_poll_waits_for_pause_then_restores_scan_interval(self) -> None:
        client = PausingClient(pause_remaining=240)
        coordinator = coordinator_module.WeishauptDataUpdateCoordinator(
            hass=SimpleNamespace(), client=client, scan_interval=30, active_groups=set()
        )

        with self.assertRaises(UpdateFailed):
            await coordinator._async_update_data()
        self.assertEqual(coordinator.update_interval, timedelta(seconds=240))

        await coordinator._async_update_data()
        self.assertEqual(coordinator.update_interval, timedelta(seconds=30))


class RecordingClient:
    """Answers every requested register with value 0 and records the keys."""

    suspended = False

    def __init__(self) -> None:
        self.calls: list[set[str]] = []

    async def read_parameters_with_status(self, params: list[dict]) -> tuple[dict, set]:
        keys = {param["key"] for param in params}
        self.calls.append(keys)
        return {key: {"value_int": 0} for key in keys}, keys


def make_coordinator(client) -> coordinator_module.WeishauptDataUpdateCoordinator:
    return coordinator_module.WeishauptDataUpdateCoordinator(
        hass=SimpleNamespace(), client=client, scan_interval=30, active_groups=set()
    )


class CoordinatorPollIntervalTests(unittest.IsolatedAsyncioTestCase):
    """Registers are read no more often than their Weishaupt update class allows."""

    async def asyncSetUp(self) -> None:
        self.client = RecordingClient()
        self.coordinator = make_coordinator(self.client)
        self.now = 1000.0
        patcher = patch.object(coordinator_module.time, "monotonic", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def poll_at(self, seconds: float) -> set[str]:
        self.now = 1000.0 + seconds
        self.coordinator.data = await self.coordinator._async_update_data()
        return self.client.calls[-1] if self.client.calls else set()

    async def test_fast_medium_and_slow_registers_follow_their_class(self) -> None:
        fast, medium, slow = "wtc_kesseltemperatur", "wtc_anlagendruck", "wtc_tageswaermemenge_vortag_gesamt"

        first = await self.poll_at(0)
        self.assertTrue({fast, medium, slow} <= first)

        at_30 = await self.poll_at(30)
        self.assertIn(fast, at_30)
        self.assertNotIn(medium, at_30)
        self.assertNotIn(slow, at_30)

        at_60 = await self.poll_at(60)
        self.assertTrue({fast, medium} <= at_60)
        self.assertNotIn(slow, at_60)

        at_600 = await self.poll_at(600)
        self.assertIn(slow, at_600)

    async def test_values_of_registers_not_due_are_kept(self) -> None:
        await self.poll_at(0)
        await self.poll_at(30)

        self.assertIn("wtc_anlagendruck", self.coordinator.data)
        self.assertLess(len(self.client.calls[1]), len(self.client.calls[0]))

    async def test_invalidated_register_is_read_on_the_next_refresh(self) -> None:
        await self.poll_at(0)
        self.coordinator.invalidate({"sg_betriebsart_warmwasser"})

        self.assertIn("sg_betriebsart_warmwasser", await self.poll_at(1))

    async def test_only_registers_of_added_entities_are_read(self) -> None:
        self.coordinator._contexts = [
            frozenset({"wtc_kesseltemperatur"}),
            frozenset(sensors.DEVICE_TIME_KEYS),
            None,  # an entity that reads no register
        ]

        keys = await self.poll_at(0)

        self.assertEqual(keys, {"wtc_kesseltemperatur", *sensors.DEVICE_TIME_KEYS})

    async def test_nothing_due_sends_no_request(self) -> None:
        self.coordinator._contexts = [frozenset({"wtc_anlagendruck"})]
        await self.poll_at(0)

        await self.poll_at(30)

        self.assertEqual(len(self.client.calls), 1)
        self.assertIn("wtc_anlagendruck", self.coordinator.data)


class FlakyClient:
    """Fails while `failing` is set, like a device that stopped answering."""

    suspended = False

    def __init__(self, error: Exception) -> None:
        self.failing = True
        self.error = error
        self.pause_remaining = 0

    async def read_parameters_with_status(self, params: list[dict]) -> tuple[dict, set]:
        if self.failing:
            raise self.error
        key = params[0]["key"]
        return {key: {"value_int": 0}}, {key}


class CoordinatorRepairIssueTests(unittest.IsolatedAsyncioTestCase):
    """A device that stays unresponsive gets a repair issue, cleared on recovery."""

    async def asyncSetUp(self) -> None:
        ha_stubs.issues.clear()
        self.now = 1000.0
        patcher = patch.object(coordinator_module.time, "monotonic", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, client):
        return coordinator_module.WeishauptDataUpdateCoordinator(
            hass=SimpleNamespace(),
            client=client,
            scan_interval=30,
            active_groups=set(),
            config_entry=SimpleNamespace(entry_id="entry-123", title="Weishaupt WTC (wem-sg)"),
        )

    async def poll_at(self, coordinator, seconds: float) -> None:
        self.now = 1000.0 + seconds
        coordinator._last_read.clear()
        try:
            await coordinator._async_update_data()
        except Exception:  # noqa: BLE001 - failures are what these tests drive
            pass

    async def test_issue_after_30_minutes_and_cleared_on_recovery(self) -> None:
        client = FlakyClient(WeishauptConnectionError("timeout"))
        coordinator = self.make(client)
        issue_id = "device_unresponsive_entry-123"

        await self.poll_at(coordinator, 0)
        await self.poll_at(coordinator, 29 * 60)
        self.assertNotIn(issue_id, ha_stubs.issues)

        await self.poll_at(coordinator, 30 * 60)
        self.assertEqual(ha_stubs.issues[issue_id]["translation_key"], "device_unresponsive")
        self.assertEqual(ha_stubs.issues[issue_id]["translation_placeholders"]["minutes"], "30")

        client.failing = False
        await self.poll_at(coordinator, 31 * 60)
        self.assertNotIn(issue_id, ha_stubs.issues)

    async def test_wrong_credentials_raise_no_issue(self) -> None:
        """Rejected credentials have their own reauth flow."""
        client = FlakyClient(ha_stubs.load("api").WeishauptAuthError("401"))
        coordinator = self.make(client)

        await self.poll_at(coordinator, 0)
        await self.poll_at(coordinator, 60 * 60)

        self.assertEqual(ha_stubs.issues, {})


if __name__ == "__main__":
    unittest.main()
