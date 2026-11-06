"""Unit tests for Weishaupt API client edge cases."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import time
import types
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "custom_components" / "weishaupt_wtc"


aiohttp_stub = types.ModuleType("aiohttp")


class ClientConnectorError(Exception):
    """Minimal aiohttp exception stub used by the API client."""


class BasicAuth:
    """Minimal aiohttp auth stub."""

    def __init__(self, login: str, password: str) -> None:
        self.login = login
        self.password = password


class ClientTimeout:
    """Minimal aiohttp timeout stub."""

    def __init__(self, total: int | None = None) -> None:
        self.total = total


class ClientSession:
    """Minimal aiohttp session stub for type compatibility."""

    closed = False

    async def close(self) -> None:
        """Provide the interface used by the client close method."""


aiohttp_stub.ClientConnectorError = ClientConnectorError
aiohttp_stub.BasicAuth = BasicAuth
aiohttp_stub.ClientTimeout = ClientTimeout
aiohttp_stub.ClientSession = ClientSession
sys.modules.setdefault("aiohttp", aiohttp_stub)


def load_module(module_name: str, file_path: Path):
    """Load a module from file while preserving package-relative imports."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


custom_components_pkg = types.ModuleType("custom_components")
custom_components_pkg.__path__ = [str(REPO_ROOT / "custom_components")]
sys.modules.setdefault("custom_components", custom_components_pkg)

integration_pkg = types.ModuleType("custom_components.weishaupt_wtc")
integration_pkg.__path__ = [str(PACKAGE_ROOT)]
sys.modules.setdefault("custom_components.weishaupt_wtc", integration_pkg)

load_module("custom_components.weishaupt_wtc.const", PACKAGE_ROOT / "const.py")
api = load_module("custom_components.weishaupt_wtc.api", PACKAGE_ROOT / "api.py")


class EmptyResponseClient(api.WeishauptApiClient):
    """API client test double that always returns an empty device response."""

    async def _post(self, payload: dict) -> dict | None:
        """Return the empty response that triggered issue #2."""
        return None


class ApiClientTests(unittest.IsolatedAsyncioTestCase):
    """Test API client behavior for empty device responses."""

    async def test_test_connection_returns_false_for_empty_response(self) -> None:
        """Connection test should not crash on an empty response."""
        client = EmptyResponseClient("wem-sg", "admin", "Admin123")

        self.assertFalse(await client.test_connection())



READ_PARAM = {"key": "hk1", "mi": 0x02, "mx": 0x00, "ox": 0x2533, "os": 0x02, "vs": 1}
VALUE_REPLY = {"CAPI": {"N01": {"VG": "020200253302000101"}}}  # CMD_RESPONSE, value 1
ERROR_REPLY = {"CAPI": {"N01": {"VG": "050200253302000100"}}}  # CMD_ERROR
WRITE_ACK = {"CAPI": {"N01": {"VG": "040300252002000101"}}}  # CMD_ACK


class ScriptedClient(api.WeishauptApiClient):
    """Returns (or raises) the scripted device replies in order and counts requests."""

    def __init__(self, replies: list) -> None:
        super().__init__("wem-sg", "admin", "Admin123", min_request_interval=0)
        self._replies = list(replies)
        self.sent = 0

    async def _post_once(self, payload: dict) -> dict | None:
        self.sent += 1
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def end_pause(self) -> None:
        """Let the current pause run out without waiting for it."""
        self._paused_until = time.monotonic() - 1


class ApiClientBackoffTests(unittest.IsolatedAsyncioTestCase):
    """A struggling device gets a growing pause instead of an immediate retry."""

    async def test_empty_reply_pauses_and_blocks_without_sending(self) -> None:
        """After an empty reply nothing is sent until the pause is over."""
        client = ScriptedClient([None])

        with self.assertLogs("custom_components.weishaupt_wtc.api", level="WARNING"):
            with self.assertRaises(api.WeishauptEmptyResponseError):
                await client.read_parameters([READ_PARAM])

        self.assertAlmostEqual(client.pause_remaining, 120, delta=1)
        with self.assertRaises(api.WeishauptRequestBlockedError):
            await client.read_parameters([READ_PARAM])
        with self.assertRaises(api.WeishauptRequestBlockedError):
            await client.write_parameter(0x03, 0x00, 0x2520, 0x02, 1, 2)
        self.assertEqual(client.sent, 1)

    async def test_failed_batch_aborts_the_rest_of_the_poll(self) -> None:
        """The next batch would be the immediate retry that locks the WEM."""
        client = ScriptedClient([VALUE_REPLY, None, VALUE_REPLY])
        params = [dict(READ_PARAM, key=f"p{i}") for i in range(14)]

        with self.assertLogs("custom_components.weishaupt_wtc.api", level="WARNING"):
            with self.assertRaises(api.WeishauptEmptyResponseError):
                await client.read_parameters(params)

        self.assertEqual(client.sent, 2)

    async def test_pause_grows_per_failure_and_resets_on_success(self) -> None:
        """2, 4, 8, 16, then 30 min; the first healthy reply ends the backoff."""
        timeout = api.WeishauptConnectionError("timeout")
        client = ScriptedClient([timeout] * 6 + [VALUE_REPLY, timeout])
        pauses = []
        with self.assertLogs("custom_components.weishaupt_wtc.api", level="WARNING"):
            for _ in range(6):
                with self.assertRaises(api.WeishauptConnectionError):
                    await client.read_parameters([READ_PARAM])
                pauses.append(round(client.pause_remaining / 60))
                client.end_pause()

            await client.read_parameters([READ_PARAM])
            self.assertEqual(client.pause_remaining, 0)

            with self.assertRaises(api.WeishauptConnectionError):
                await client.read_parameters([READ_PARAM])

        self.assertEqual(pauses, [2, 4, 8, 16, 30, 30])
        self.assertAlmostEqual(client.pause_remaining, 120, delta=1)

    async def test_error_on_every_register_pauses(self) -> None:
        """CMD_ERROR for all registers is how a locked WEM answers."""
        client = ScriptedClient([ERROR_REPLY])

        with self.assertLogs("custom_components.weishaupt_wtc.api", level="WARNING"):
            with self.assertRaises(api.WeishauptEmptyResponseError):
                await client.read_parameters([READ_PARAM])

        self.assertGreater(client.pause_remaining, 0)

    async def test_wrong_credentials_do_not_pause(self) -> None:
        """A 401 is a configuration problem, not a struggling device."""
        client = ScriptedClient([api.WeishauptAuthError("401")])

        with self.assertRaises(api.WeishauptAuthError):
            await client.read_parameters([READ_PARAM])

        self.assertEqual(client.pause_remaining, 0)

    async def test_malformed_json_reply_pauses(self) -> None:
        """Valid JSON of the wrong shape is a failed reply, not a crash."""
        for reply in ([], "busy", {"CAPI": None}):
            client = ScriptedClient([reply])

            with self.assertLogs("custom_components.weishaupt_wtc.api", level="WARNING"):
                with self.assertRaises(api.WeishauptEmptyResponseError):
                    await client.read_parameters([READ_PARAM])

            self.assertGreater(client.pause_remaining, 0)


class ApiClientWriteThrottleTests(unittest.IsolatedAsyncioTestCase):
    """At most one write per MIN_WRITE_INTERVAL."""

    async def test_second_write_within_a_minute_is_blocked(self) -> None:
        """A blocked write sends nothing; after the interval the next one goes out."""
        client = ScriptedClient([WRITE_ACK, WRITE_ACK])

        self.assertTrue(await client.write_parameter(0x03, 0x00, 0x2520, 0x02, 1, 2))
        with self.assertRaises(api.WeishauptRequestBlockedError):
            await client.write_parameter(0x03, 0x00, 0x2520, 0x02, 1, 1)
        self.assertEqual(client.sent, 1)

        client._last_write_at -= api.MIN_WRITE_INTERVAL
        self.assertTrue(await client.write_parameter(0x03, 0x00, 0x2520, 0x02, 1, 1))
        self.assertEqual(client.sent, 2)

    async def test_write_blocked_by_a_pause_keeps_the_write_slot(self) -> None:
        """A pause that starts while the write waits for the lock sends nothing,
        so the next write must not have to wait a minute."""
        client = ScriptedClient([WRITE_ACK])

        async with client._request_lock:
            write = asyncio.ensure_future(
                client.write_parameter(0x03, 0x00, 0x2520, 0x02, 1, 2)
            )
            await asyncio.sleep(0)  # the write is now queued on the lock
            with self.assertLogs("custom_components.weishaupt_wtc.api", level="WARNING"):
                client._start_pause()
        with self.assertRaises(api.WeishauptRequestBlockedError):
            await write

        client.end_pause()
        self.assertTrue(await client.write_parameter(0x03, 0x00, 0x2520, 0x02, 1, 2))
        self.assertEqual(client.sent, 1)


class BatchCountingClient(api.WeishauptApiClient):
    """Test double that records the batch size of each request."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.batch_sizes: list[int] = []

    async def _post_once(self, payload: dict) -> dict | None:
        self.batch_sizes.append(payload["CAPI"]["NN"])
        return {"CAPI": {}}


class ApiClientBatchingTests(unittest.IsolatedAsyncioTestCase):
    """Test that reads are split into batches of MAX_PARAMS_PER_REQUEST."""

    async def test_read_parameters_splits_into_batches_of_six(self) -> None:
        """Larger batches have been observed to cause CMD_ERROR on real hardware."""
        client = BatchCountingClient(
            "wem-sg", "admin", "Admin123", min_request_interval=0
        )
        params = [
            {"key": f"p{i}", "mi": 0, "mx": 0, "ox": 0, "os": 0, "vs": 1}
            for i in range(14)
        ]

        with self.assertLogs("custom_components.weishaupt_wtc.api", level="WARNING"):
            with self.assertRaises(api.WeishauptEmptyResponseError):
                await client.read_parameters(params)  # no register answered

        self.assertEqual(client.batch_sizes, [6, 6, 2])


class ThrottlingClient(api.WeishauptApiClient):
    """Test double that returns an empty CAPI response for every request."""

    async def _post_once(self, payload: dict) -> dict:
        return {"CAPI": {}}


class ApiClientThrottleTests(unittest.IsolatedAsyncioTestCase):
    """Test that requests are spaced at least min_request_interval apart."""

    async def test_post_enforces_minimum_interval_between_requests(self) -> None:
        """Requests arriving too close together can cause CMD_ERROR responses."""
        client = ThrottlingClient(
            "wem-sg", "admin", "Admin123", min_request_interval=0.05
        )

        start = time.monotonic()
        await client._post({"a": 1})
        await client._post({"a": 2})
        await client._post({"a": 3})
        elapsed = time.monotonic() - start

        self.assertGreaterEqual(elapsed, 0.1)


class CannedResponseClient(api.WeishauptApiClient):
    """API client test double that returns one canned device response."""

    def __init__(self, response: dict) -> None:
        super().__init__("wem-sg", "admin", "Admin123")
        self._response = response

    async def _post(self, payload: dict) -> dict:
        """Return the canned response for every request."""
        return self._response


class ReadAnsweredKeysTests(unittest.IsolatedAsyncioTestCase):
    """Report which keys the device answered, so detection can tell absent from unknown."""

    async def test_error_replies_count_as_answered_silent_frames_do_not(self) -> None:
        """CMD_RESPONSE and CMD_ERROR both count as answered; a missing reply does not."""
        client = CannedResponseClient(
            {
                "CAPI": {
                    "N01": {"VG": "020200253302000101"},  # CMD_RESPONSE, value 1
                    "N02": {"VG": "050200257e0500020001"},  # CMD_ERROR, absent module
                }
            }
        )
        params = [
            {"key": "present", "mi": 0x02, "mx": 0x00, "ox": 0x2533, "os": 0x02, "vs": 1},
            {"key": "absent", "mi": 0x02, "mx": 0x01, "ox": 0x257E, "os": 0x05, "vs": 1},
            {"key": "silent", "mi": 0x02, "mx": 0x02, "ox": 0x257E, "os": 0x05, "vs": 1},
        ]

        results, answered = await client.read_parameters_with_status(params)

        self.assertEqual(set(results), {"present"})
        self.assertEqual(answered, {"present", "absent"})


if __name__ == "__main__":
    unittest.main()
