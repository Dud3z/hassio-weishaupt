"""Regression tests for optional device group auto-detection in the coordinator."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
import unittest
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


if __name__ == "__main__":
    unittest.main()
