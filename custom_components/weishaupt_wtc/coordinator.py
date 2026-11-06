"""DataUpdateCoordinator for Weishaupt WTC."""

from __future__ import annotations

from datetime import datetime
import logging
import time
from zoneinfo import ZoneInfo
from collections.abc import Callable, Iterable
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    WeishauptApiClient,
    WeishauptApiError,
    WeishauptAuthError,
    WeishauptConnectionError,
)
from .const import DOMAIN
from .health import (
    CLOCK_DRIFT_LIMIT_MINUTES,
    FAULT_BLOCK_KEY,
    active_fault,
    clock_drift_minutes,
    next_maintenance,
)
from .sensors import (
    DEVICE_TIME_KEYS,
    OPTIONAL_GROUPS,
    POLLED_SENSORS,
    WeishauptDeviceGroup,
    WeishauptSensorDefinition,
)

_LOGGER = logging.getLogger(__name__)

type WeishauptConfigEntry = ConfigEntry[WeishauptDataUpdateCoordinator]

# A device that has not answered for this long gets a repair issue explaining
# the known causes (portal, second client, lock-up until power cycle).
UNRESPONSIVE_ISSUE_AFTER = 1800  # seconds
# The clock check reads the device time this often, even with its entity disabled.
CLOCK_CHECK_INTERVAL = 600  # seconds
# Drifted readings in a row before warning; one alone may be the date register
# lagging the time registers around midnight.
CLOCK_DRIFT_READINGS = 2


class WeishauptDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to manage fetching data from Weishaupt device."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: WeishauptApiClient,
        scan_interval: int = 30,
        active_groups: set[WeishauptDeviceGroup] | None = None,
        on_groups_detected: Callable[[set[WeishauptDeviceGroup]], None] | None = None,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
            config_entry=config_entry,
        )
        # time.monotonic() of the first failed poll in the current streak.
        self._failing_since: float | None = None
        self._last_clock_check: float | None = None
        self._clock_drift_readings = 0
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
        self.update_maintenance_issue()
        if self.client.suspended:
            # Paused by the user: keep the last values and send nothing.
            return dict(self.data or {})

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

        if self._clock_check_due(now):
            requested = {param["key"] for param in params}
            for sensor_def in POLLED_SENSORS:
                if sensor_def.key in DEVICE_TIME_KEYS and sensor_def.key not in requested:
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
        except WeishauptAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err  # HA asks for new credentials
        except WeishauptApiError as err:
            self._track_unresponsive(failed=True)
            # Next poll once the client's backoff pause is over, not every scan interval.
            self.update_interval = max(
                self._scan_interval, timedelta(seconds=self.client.pause_remaining)
            )
            if isinstance(err, WeishauptConnectionError):
                raise UpdateFailed(f"Connection error: {err}") from err
            raise UpdateFailed(f"API error: {err}") from err
        self.update_interval = self._scan_interval
        self._track_unresponsive(failed=False)

        if detecting:
            self._finish_detection(optional_probes, results, answered)

        # Keep the values of registers that were not due; drop those that were
        # read but returned nothing, so their entities show unknown, not stale.
        data = dict(self.data or {})
        for param in params:
            self._last_read[param["key"]] = now
            data.pop(param["key"], None)
        data.update(results)
        if FAULT_BLOCK_KEY in results:
            self._update_fault_issue(results)
        if self.config_entry is not None and all(key in results for key in DEVICE_TIME_KEYS):
            self._last_clock_check = now
            self._update_clock_issue(results)
        return data

    @property
    def unresponsive_issue_id(self) -> str:
        """Repair issue id for this config entry."""
        return f"device_unresponsive_{self.config_entry.entry_id}"

    def _track_unresponsive(self, failed: bool) -> None:
        """Raise a repair issue after a long failure streak; clear it on recovery."""
        if self.config_entry is None:
            return
        if not failed:
            if self._failing_since is not None:
                self._failing_since = None
                ir.async_delete_issue(self.hass, DOMAIN, self.unresponsive_issue_id)
            return
        now = time.monotonic()
        if self._failing_since is None:
            self._failing_since = now
        if now - self._failing_since >= UNRESPONSIVE_ISSUE_AFTER:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self.unresponsive_issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="device_unresponsive",
                translation_placeholders={
                    "title": self.config_entry.title,
                    "minutes": str(round((now - self._failing_since) / 60)),
                },
            )

    def _issue(self, name: str, active: bool, **placeholders: str) -> None:
        """Create or delete the repair issue `name` for this config entry."""
        if self.config_entry is None:
            return
        issue_id = f"{name}_{self.config_entry.entry_id}"
        if not active:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=name,
            translation_placeholders={"title": self.config_entry.title, **placeholders},
        )

    def _update_fault_issue(self, results: dict[str, Any]) -> None:
        """Show a repair issue while the Systemgerät reports an error."""
        fault = active_fault(results)
        if fault is None:
            self._issue("device_fault", False)
            return
        message, number = fault
        self._issue(
            "device_fault", True, meldung=message, fehlernummer="?" if number is None else str(number)
        )

    def _clock_check_due(self, now: float) -> bool:
        if self.config_entry is None:
            return False  # nowhere to report a drift
        return (
            self._last_clock_check is None
            or now - self._last_clock_check >= CLOCK_CHECK_INTERVAL
        )

    def _update_clock_issue(self, results: dict[str, Any]) -> None:
        """Warn when the device clock is off, e.g. after the DST change or a power cut."""
        timezone = ZoneInfo(self.hass.config.time_zone)
        now = datetime.now(timezone)
        drift = clock_drift_minutes(results, timezone, now)
        if drift is None or drift <= CLOCK_DRIFT_LIMIT_MINUTES:
            self._clock_drift_readings = 0
            self._issue("clock_drift", False)
            return
        self._clock_drift_readings += 1
        if self._clock_drift_readings >= CLOCK_DRIFT_READINGS:
            self._issue("clock_drift", True, minutes=str(round(drift)))

    def update_maintenance_issue(self) -> None:
        """Show a repair issue once the configured maintenance is due."""
        if self.config_entry is None:
            return
        due = next_maintenance(self.config_entry.options)
        today = datetime.now(ZoneInfo(self.hass.config.time_zone)).date()
        self._issue(
            "maintenance_due",
            due is not None and today >= due,
            due=due.isoformat() if due else "",
        )

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
