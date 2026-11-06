"""DataUpdateCoordinator for Weishaupt WTC."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WeishauptApiClient, WeishauptApiError, WeishauptConnectionError
from .const import DOMAIN
from .sensors import (
    OPTIONAL_GROUPS,
    POLLED_SENSORS,
    WeishauptDeviceGroup,
    WeishauptSensorDefinition,
)

_LOGGER = logging.getLogger(__name__)


class WeishauptDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to manage fetching data from Weishaupt device."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: WeishauptApiClient,
        scan_interval: int = 30,
        active_groups: set[WeishauptDeviceGroup] | None = None,
        on_groups_detected: Callable[[set[WeishauptDeviceGroup]], None] | None = None,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self._scan_interval = timedelta(seconds=scan_interval)
        # None until detection has run; then the optional groups (HK, HK3, SOL, ...)
        # that responded on this device. Restored from the config entry when stored.
        # shortcut: a module removed after detection stays polled until Reconfigure re-detects, upgrade to dropping groups after repeated CMD_ERROR
        self.active_groups = active_groups
        self._on_groups_detected = on_groups_detected
        # When each register was last read (time.monotonic), for its poll_interval.
        self._last_read: dict[str, float] = {}

    def invalidate(self, keys: Iterable[str]) -> None:
        """Read these registers on the next refresh, e.g. right after a write."""
        for key in keys:
            self._last_read.pop(key, None)

    def _wanted_keys(self) -> set[str] | None:
        """Registers the added entities read; None = no entity added yet, read all.

        Each entity passes the keys it reads as its coordinator context. Disabled
        entities are never added, so their registers are not polled.
        """
        wanted: set[str] = set()
        has_listener = False
        for context in self.async_contexts():
            has_listener = True
            if context:
                wanted.update(context)
        return wanted if has_listener else None

    def _is_due(self, sensor_def: WeishauptSensorDefinition, now: float) -> bool:
        last = self._last_read.get(sensor_def.key)
        if last is None:
            return True
        # Half a scan interval of slack, so a 60 s register is read on every
        # second 30 s tick instead of slipping to the third one.
        slack = self._scan_interval.total_seconds() / 2
        return now - last >= sensor_def.poll_interval - slack

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the Weishaupt device."""
        detecting = self.active_groups is None
        optional_probes: dict[WeishauptDeviceGroup, WeishauptSensorDefinition] = {}
        sensor_definitions = POLLED_SENSORS
        if detecting:
            for sensor_def in POLLED_SENSORS:
                if sensor_def.group in OPTIONAL_GROUPS:
                    optional_probes.setdefault(sensor_def.group, sensor_def)
            sensor_definitions = [
                sensor_def
                for sensor_def in POLLED_SENSORS
                if sensor_def.group not in OPTIONAL_GROUPS
            ] + list(optional_probes.values())

        now = time.monotonic()
        wanted = None if detecting else self._wanted_keys()
        params = []
        for sensor_def in sensor_definitions:
            if (
                self.active_groups is not None
                and sensor_def.group in OPTIONAL_GROUPS
                and sensor_def.group not in self.active_groups
            ):
                continue
            if wanted is not None and sensor_def.key not in wanted:
                continue
            if not self._is_due(sensor_def, now):
                continue
            params.append(
                {
                    "key": sensor_def.key,
                    "mi": sensor_def.mi,
                    "mx": sensor_def.mx,
                    "ox": sensor_def.ox,
                    "os": sensor_def.os,
                    "vs": sensor_def.vs,
                }
            )

        if not params:
            return dict(self.data or {})

        try:
            results, answered = await self.client.read_parameters_with_status(params)
        except WeishauptApiError as err:
            # Next poll once the client's backoff pause is over, not every scan interval.
            self.update_interval = max(
                self._scan_interval, timedelta(seconds=self.client.pause_remaining)
            )
            if isinstance(err, WeishauptConnectionError):
                raise UpdateFailed(f"Connection error: {err}") from err
            raise UpdateFailed(f"API error: {err}") from err
        self.update_interval = self._scan_interval

        if detecting:
            self._finish_detection(optional_probes, results, answered)

        # Keep the values of registers that were not due; drop those that were
        # read but returned nothing, so their entities show unknown, not stale.
        data = dict(self.data or {})
        for param in params:
            self._last_read[param["key"]] = now
            data.pop(param["key"], None)
        data.update(results)
        return data

    def _finish_detection(
        self,
        probes: dict[WeishauptDeviceGroup, WeishauptSensorDefinition],
        results: dict[str, Any],
        answered: set[str],
    ) -> None:
        """Turn probe answers into active groups; persist only complete answers."""
        self.active_groups = {
            group for group, sensor_def in probes.items() if sensor_def.key in results
        }
        _LOGGER.info(
            "Detected optional device groups: %s",
            sorted(group.value for group in self.active_groups),
        )
        # A probe without any reply says nothing about its module. Keep the result
        # for this session, but don't persist it, so the next setup probes again.
        # shortcut: one CMD_ERROR probe is stored as absent until Reconfigure re-detects, upgrade to retry-before-store if transient errors show up
        complete = all(sensor_def.key in answered for sensor_def in probes.values())
        if complete and self._on_groups_detected is not None:
            self._on_groups_detected(self.active_groups)
