"""API client for Weishaupt WTC CanApiJson protocol."""

from __future__ import annotations

import asyncio
from collections import deque
import logging
import math
import time
from typing import Any

import aiohttp

from .const import API_ENDPOINT, CMD_GET, CMD_SET, CMD_RESPONSE, CMD_ERROR, SRC_DDC

_LOGGER = logging.getLogger(__name__)

REQUEST_ID = "12345678"
# Larger batches have been observed to cause individual frames to return
# CMD_ERROR on some hardware, even though the same registers read fine in
# smaller batches; 6 is what other CanApiJson implementations settled on.
# 11+ frames per request stall the device (HTTP 200, empty body, other clients
# blocked ~12 s); 8 and 10 did so sporadically (kraiz/hassio-weishaupt#15).
MAX_PARAMS_PER_REQUEST = 6
MIN_REQUEST_INTERVAL = 0.3  # seconds between requests, avoids overloading the device
# Pause after a request the device could not serve (empty reply, no register
# answered, unreachable); each further failure takes the next step, the first
# healthy reply resets. Retrying a struggling WEM right away has locked its JSON
# interface until a power cycle (jvejmelka/weishaupt-wem-can-esphome, PROTOKOLL.md §8).
BACKOFF_STEPS = (120, 240, 480, 960, 1800)  # seconds
# Several writes within a few minutes have locked the WEM (same source).
MIN_WRITE_INTERVAL = 60  # seconds between writes


class WeishauptApiError(Exception):
    """Exception for Weishaupt API errors."""


class WeishauptConnectionError(WeishauptApiError):
    """Exception for connection errors."""


class WeishauptAuthError(WeishauptApiError):
    """Exception for authentication errors."""


class WeishauptEmptyResponseError(WeishauptConnectionError):
    """The device replied, but without usable data (empty body or no CAPI)."""


class WeishauptRequestBlockedError(WeishauptApiError):
    """Raised without contacting the device: requests are paused or a write came too soon."""


def build_vg_frame(cmd: int, mi: int, mx: int, ox: int, os_val: int, vs: int) -> str:
    """Build a VG hex frame string for a CanApiJson request.

    Format: CM(1B) MI(1B) MX(1B) OX(2B) OS(1B) VS(2B)
    """
    return f"{cmd:02x}{mi:02x}{mx:02x}{ox:04x}{os_val:02x}{vs:04x}00"


def build_read_vg(mi: int, mx: int, ox: int, os_val: int, vs: int) -> str:
    """Build a VG read request frame (CMD=0x01 GET)."""
    # For read, we pad with zeros for the value area
    padding = "00" * vs
    return f"{CMD_GET:02x}{mi:02x}{mx:02x}{ox:04x}{os_val:02x}{vs:04x}{padding}"


def build_write_vg(
    mi: int, mx: int, ox: int, os_val: int, vs: int, value_hex: str
) -> str:
    """Build a VG write request frame (CMD=0x03 SET) including value area."""
    return f"{CMD_SET:02x}{mi:02x}{mx:02x}{ox:04x}{os_val:02x}{vs:04x}{value_hex}"


def parse_vg_response(vg: str) -> dict[str, Any]:
    """Parse a VG response frame.

    Returns dict with keys: cmd, mi, mx, ox, os, vs, value_hex, value_int
    """
    if len(vg) < 16:
        raise WeishauptApiError(f"VG frame too short: {vg}")

    cmd = int(vg[0:2], 16)
    mi = int(vg[2:4], 16)
    mx = int(vg[4:6], 16)
    ox = int(vg[6:10], 16)
    os_val = int(vg[10:12], 16)
    vs = int(vg[12:16], 16)

    value_hex = vg[16:]
    value_int = int(value_hex, 16) if value_hex else 0

    return {
        "cmd": cmd,
        "mi": mi,
        "mx": mx,
        "ox": ox,
        "os": os_val,
        "vs": vs,
        "value_hex": value_hex,
        "value_int": value_int,
    }


class WeishauptApiClient:
    """Client to interact with Weishaupt WTC CanApiJson API."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        session: aiohttp.ClientSession | None = None,
        min_request_interval: float = MIN_REQUEST_INTERVAL,
    ) -> None:
        """Initialize the API client."""
        self._host = host
        self._username = username
        self._password = password
        self._session = session
        self._own_session = session is None
        self._base_url = f"http://{host}{API_ENDPOINT}"
        self._request_lock = asyncio.Lock()
        self._last_request_at: float | None = None
        self._min_request_interval = min_request_interval
        self._failures = 0
        self._paused_until: float | None = None
        # Set by the "Abfragen pausieren" switch: send nothing until cleared.
        self.suspended = False
        self._sent_at: deque[float] = deque()  # time.monotonic() of each request sent
        self._last_write_at: float | None = None

    @property
    def requests_last_hour(self) -> int:
        """Return how many requests were sent to the device in the last hour."""
        cutoff = time.monotonic() - 3600
        while self._sent_at and self._sent_at[0] < cutoff:
            self._sent_at.popleft()
        return len(self._sent_at)

    @property
    def pause_remaining(self) -> float:
        """Seconds until requests to the device are allowed again (0 = not paused)."""
        if self._paused_until is None:
            return 0.0
        return max(0.0, self._paused_until - time.monotonic())

    def _start_pause(self) -> None:
        """Pause all requests for the next backoff step."""
        step = BACKOFF_STEPS[min(self._failures, len(BACKOFF_STEPS) - 1)]
        self._failures += 1
        self._paused_until = time.monotonic() + step
        _LOGGER.warning(
            "Weishaupt device at %s did not answer properly, pausing requests for %d min",
            self._host,
            step // 60,
        )

    def _record_success(self) -> None:
        """End the backoff after a healthy reply."""
        if self._failures:
            _LOGGER.info("Weishaupt device at %s answers again", self._host)
        self._failures = 0
        self._paused_until = None

    def _raise_if_paused(self) -> None:
        if self.suspended:
            raise WeishauptRequestBlockedError(
                "Requests to the Weishaupt device are paused by the "
                "\"Abfragen pausieren\" switch"
            )
        remaining = self.pause_remaining
        if remaining > 0:
            raise WeishauptRequestBlockedError(
                f"Requests to the Weishaupt device are paused for another "
                f"{math.ceil(remaining / 60)} min after it did not answer properly"
            )

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Ensure we have an active session."""
        if self._session is None or self._session.closed:
            auth = aiohttp.BasicAuth(self._username, self._password)
            self._session = aiohttp.ClientSession(auth=auth)
            self._own_session = True
        return self._session

    async def close(self) -> None:
        """Close the session."""
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()

    async def _wait_for_request_slot(self) -> None:
        """Sleep if needed so requests stay at least min_request_interval apart."""
        if self._last_request_at is None:
            return
        remaining = self._min_request_interval - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _post(self, payload: dict) -> dict:
        """Post a JSON payload to the Weishaupt device.

        Serialized through a single lock with a minimum gap between requests,
        since the device can return CMD_ERROR for individual frames when
        requests arrive too close together. A failed request starts a pause
        (see BACKOFF_STEPS); while it lasts, nothing is sent.
        """
        async with self._request_lock:
            self._raise_if_paused()
            await self._wait_for_request_slot()
            self._sent_at.append(time.monotonic())
            try:
                response = await self._post_once(payload)
            except WeishauptAuthError:
                raise  # wrong credentials, not a struggling device
            except WeishauptApiError:
                self._start_pause()
                raise
            finally:
                self._last_request_at = time.monotonic()
            if not isinstance(response, dict) or not isinstance(response.get("CAPI"), dict):
                self._start_pause()
                raise WeishauptEmptyResponseError(
                    f"Empty response from Weishaupt device at {self._host}"
                )
            return response

    async def _post_once(self, payload: dict) -> dict:
        """Perform a single HTTP POST, without throttling or locking."""
        session = await self._ensure_session()
        headers = {
            "Connection": "keep-alive",
            "Referer": f"http://{self._host}/",
            "Content-Type": "application/json",
        }

        try:
            async with session.post(
                self._base_url,
                json=payload,
                headers=headers,
                auth=aiohttp.BasicAuth(self._username, self._password),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status == 401:
                    raise WeishauptAuthError(
                        "Authentication failed. Check username/password."
                    )
                if response.status != 200:
                    raise WeishauptApiError(
                        f"Unexpected HTTP status: {response.status}"
                    )
                return await response.json(content_type=None)
        except aiohttp.ClientConnectorError as err:
            raise WeishauptConnectionError(
                f"Cannot connect to Weishaupt device at {self._host}: {err}"
            ) from err
        except asyncio.TimeoutError as err:
            raise WeishauptConnectionError(
                f"Timeout connecting to Weishaupt device at {self._host}"
            ) from err
        except (aiohttp.ClientError, ValueError) as err:
            # e.g. the device dropping the connection or answering with something
            # that is not JSON; counts as a failed request so the backoff applies.
            raise WeishauptConnectionError(
                f"Bad reply from Weishaupt device at {self._host}: {err}"
            ) from err

    async def test_connection(self) -> bool:
        """Test if we can connect and authenticate to the device."""
        # Try reading a simple register (Betriebsart HK1 - reg 100)
        vg = build_read_vg(mi=0x02, mx=0x00, ox=0x2533, os_val=0x02, vs=0x01)
        payload = {
            "ID": REQUEST_ID,
            "SRC": SRC_DDC,
            "CAPI": {"NN": 1, "N01": {"VG": vg}},
        }
        try:
            result = await self._post(payload)
            return bool(result and "CAPI" in result)
        except WeishauptApiError:
            raise
        except Exception as err:
            _LOGGER.error("Connection test failed: %s", err)
            return False

    async def read_parameters(self, params: list[dict[str, int]]) -> dict[str, Any]:
        """Read multiple parameters from the device (results only)."""
        results, _ = await self.read_parameters_with_status(params)
        return results

    async def read_parameters_with_status(
        self, params: list[dict[str, int]]
    ) -> tuple[dict[str, Any], set[str]]:
        """Read multiple parameters from the device.

        Args:
            params: List of dicts with keys: mi, mx, ox, os, vs, key
                    where 'key' is a unique identifier for the parameter.

        Returns:
            (results, answered): results maps key -> parsed response value dict;
            answered holds every key the device replied to, with a value or with
            CMD_ERROR. A key missing from answered got no reply at all.
        """
        results = {}
        answered: set[str] = set()

        # Split into batches of MAX_PARAMS_PER_REQUEST
        for batch_start in range(0, len(params), MAX_PARAMS_PER_REQUEST):
            batch = params[batch_start : batch_start + MAX_PARAMS_PER_REQUEST]
            capi = {"NN": len(batch)}

            for i, param in enumerate(batch):
                vg = build_read_vg(
                    mi=param["mi"],
                    mx=param["mx"],
                    ox=param["ox"],
                    os_val=param["os"],
                    vs=param["vs"],
                )
                capi[f"N{i + 1:02d}"] = {"VG": vg}

            payload = {
                "ID": REQUEST_ID,
                "SRC": SRC_DDC,
                "CAPI": capi,
            }

            # A failed batch aborts the poll: sending the next batch right away is
            # the immediate retry that has locked the WEM.
            response = await self._post(payload)

            response_capi = response["CAPI"]
            for i, param in enumerate(batch):
                key = f"N{i + 1:02d}"
                if key not in response_capi:
                    _LOGGER.debug("Missing %s in response", key)
                    continue

                vg_str = response_capi[key].get("VG", "")
                if not vg_str:
                    continue

                try:
                    parsed = parse_vg_response(vg_str)
                except (ValueError, WeishauptApiError) as err:
                    _LOGGER.debug(
                        "Failed to parse VG response for %s: %s",
                        param.get("key", key),
                        err,
                    )
                    continue

                # Check for error response
                if parsed["cmd"] == CMD_ERROR:
                    answered.add(param["key"])
                    _LOGGER.debug(
                        "Error response for %s: %s",
                        param.get("key", key),
                        vg_str,
                    )
                    continue

                # Check it's a proper response
                if parsed["cmd"] == CMD_RESPONSE:
                    answered.add(param["key"])
                    results[param["key"]] = parsed

        if params and not results:
            # CMD_ERROR on every register is how a locked WEM answers.
            self._start_pause()
            raise WeishauptEmptyResponseError(
                f"Weishaupt device at {self._host} answered none of {len(params)} registers"
            )
        self._record_success()
        return results, answered

    async def write_parameter(
        self,
        mi: int,
        mx: int,
        ox: int,
        os_val: int,
        vs: int,
        value_int: int,
    ) -> bool:
        """Write a single parameter value to the device.

        Returns True on success, False on failure. Raises
        WeishauptRequestBlockedError, without sending anything, while requests
        are paused or within MIN_WRITE_INTERVAL of the previous write.
        """
        self._raise_if_paused()
        if self._last_write_at is not None:
            remaining = MIN_WRITE_INTERVAL - (time.monotonic() - self._last_write_at)
            if remaining > 0:
                raise WeishauptRequestBlockedError(
                    f"Wait {math.ceil(remaining)} s before the next write to the "
                    "Weishaupt device"
                )
        self._last_write_at = time.monotonic()

        # Encode value_int as big-endian hex with vs bytes
        value_hex = f"{value_int:0{vs * 2}x}"
        vg = build_write_vg(
            mi=mi, mx=mx, ox=ox, os_val=os_val, vs=vs, value_hex=value_hex
        )

        payload = {
            "ID": REQUEST_ID,
            "SRC": SRC_DDC,
            "CAPI": {"NN": 1, "N01": {"VG": vg}},
        }

        try:
            response = await self._post(payload)
        except WeishauptRequestBlockedError:
            self._last_write_at = None  # a pause began while queued; nothing was sent
            raise
        except WeishauptApiError as err:
            _LOGGER.error("Failed to write parameter: %s", err)
            raise

        response_capi = response["CAPI"]
        key = "N01"
        if key not in response_capi:
            _LOGGER.debug("Missing %s in write response", key)
            return False

        vg_str = response_capi[key].get("VG", "")
        if not vg_str:
            _LOGGER.debug("No VG in write response for %s", key)
            return False

        try:
            parsed = parse_vg_response(vg_str)
        except (ValueError, WeishauptApiError) as err:
            _LOGGER.debug("Failed to parse VG write response: %s", err)
            return False

        if parsed["cmd"] == CMD_ERROR:
            _LOGGER.debug("Error response for write VG: %s", vg_str)
            return False

        self._record_success()
        return True
