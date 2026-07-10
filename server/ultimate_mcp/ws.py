"""HaWsClient — persistent WebSocket client to the HA core WS API (W0).

Connects to ws://supervisor/core/websocket, authenticates with the
SUPERVISOR_TOKEN, and reconnects automatically with exponential backoff.

Message flow (https://developers.home-assistant.io/docs/api/websocket):
  server -> {"type": "auth_required"}
  client -> {"type": "auth", "access_token": <token>}
  server -> {"type": "auth_ok"} | {"type": "auth_invalid", "message": ...}
Then id-correlated commands: {"id": N, "type": <command>, ...} answered by
{"id": N, "type": "result", "success": bool, "result"|"error": ...} and
subscription events {"id": <sub_id>, "type": "event", "event": {...}}.

This module deliberately has no dependency on context.py (no httpx import) so
it can be unit-tested standalone against a local fake server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Generator

from websockets.asyncio.client import connect as ws_connect

log = logging.getLogger("umcp.ws")

DEFAULT_WS_URL = "ws://supervisor/core/websocket"
_MAX_MESSAGE_BYTES = 16 * 1024 * 1024  # diagnostics dumps can be large


class HaWsError(RuntimeError):
    """A command result with success=False."""

    def __init__(self, code: str, message: str, raw: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.raw = raw or {}


class HaWsAuthError(RuntimeError):
    """Auth handshake rejected (auth_invalid) — never retried."""


class Subscription:
    """Async iterator of HA events for one subscribe_events subscription.

    Works both ways (the W0 stub was ambiguous, so support both):
        async for event in client.subscribe("state_changed"): ...
        sub = await client.subscribe("state_changed")  # eagerly subscribed
    Call `await sub.aclose()` to send unsubscribe_events and stop iteration.
    """

    def __init__(self, client: "HaWsClient", event_type: str | None) -> None:
        self._client = client
        self._event_type = event_type
        self._queue: asyncio.Queue = asyncio.Queue()
        self._sub_id: int | None = None
        self._ws: Any = None
        self._closed = False

    @property
    def subscription_id(self) -> int | None:
        return self._sub_id

    def __await__(self) -> Generator[Any, None, "Subscription"]:
        return self._start().__await__()

    async def _start(self) -> "Subscription":
        if self._sub_id is None and not self._closed:
            await self._client._start_subscription(self)
        return self

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._closed:
            raise StopAsyncIteration
        await self._start()
        item = await self._queue.get()
        if isinstance(item, Exception):
            self._closed = True
            if self._sub_id is not None:
                self._client._event_queues.pop(self._sub_id, None)
            raise item
        return item

    async def aclose(self) -> None:
        """Stop iterating and send unsubscribe_events (best effort)."""
        if self._closed:
            return
        self._closed = True
        if self._sub_id is None:
            return
        self._client._event_queues.pop(self._sub_id, None)
        if self._client._ws is self._ws:  # connection that carried the sub is still live
            try:
                await self._client.call("unsubscribe_events", subscription=self._sub_id)
            except Exception:
                log.debug("unsubscribe %s failed (connection gone?)", self._sub_id)


class HaWsClient:
    """Persistent, auto-reconnecting HA core WebSocket client.

    call()           — send a command, await the matching-id result.
    subscribe()      — Subscription (async iterator); unsubscribes on aclose().
    wait_for_state() — block until an entity reaches a state (or timeout).
    """

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        backoff_initial: float = 1.0,
        backoff_max: float = 30.0,
        max_connect_attempts: int | None = None,
    ) -> None:
        self.url = url or os.environ.get("UMCP_WS_URL", DEFAULT_WS_URL)
        self._token = token  # None -> read SUPERVISOR_TOKEN at connect time
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._max_connect_attempts = max_connect_attempts

        self._ws: Any = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._event_queues: dict[int, asyncio.Queue] = {}
        self._reader_task: asyncio.Task | None = None
        self._conn_lock: asyncio.Lock | None = None  # created lazily in a running loop
        self._closed = False

    # ------------------------------------------------------------------ state

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    # ------------------------------------------------------------- connection

    async def _ensure_connected(self) -> Any:
        if self._closed:
            raise ConnectionError("HaWsClient is closed")
        if self._ws is not None:
            return self._ws
        if self._conn_lock is None:
            self._conn_lock = asyncio.Lock()
        async with self._conn_lock:
            if self._ws is not None:  # another task won the race
                return self._ws
            delay = self._backoff_initial
            attempt = 0
            while True:
                attempt += 1
                try:
                    ws = await ws_connect(self.url, max_size=_MAX_MESSAGE_BYTES)
                    try:
                        await self._authenticate(ws)
                    except BaseException:
                        await ws.close()
                        raise
                    self._ws = ws
                    self._reader_task = asyncio.create_task(self._reader(ws))
                    log.info("connected to %s (attempt %d)", self.url, attempt)
                    return ws
                except HaWsAuthError:
                    raise  # bad token: retrying is pointless
                except Exception as exc:
                    if (
                        self._max_connect_attempts is not None
                        and attempt >= self._max_connect_attempts
                    ):
                        raise ConnectionError(
                            f"could not connect to {self.url} after {attempt} attempts: {exc}"
                        ) from exc
                    log.warning(
                        "WS connect attempt %d failed (%s); retrying in %.1fs",
                        attempt,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self._backoff_max)

    async def _authenticate(self, ws: Any) -> None:
        first = json.loads(await ws.recv())
        if first.get("type") == "auth_ok":  # server not requiring auth
            return
        if first.get("type") != "auth_required":
            raise ConnectionError(f"unexpected first WS message: {first.get('type')}")
        token = self._token if self._token is not None else os.environ.get("SUPERVISOR_TOKEN", "")
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        resp = json.loads(await ws.recv())
        if resp.get("type") != "auth_ok":
            raise HaWsAuthError(resp.get("message", "authentication rejected"))

    async def _reader(self, ws: Any) -> None:
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    log.warning("ignoring non-JSON WS frame")
                    continue
                mtype = msg.get("type")
                mid = msg.get("id")
                if mtype == "result":
                    fut = self._pending.pop(mid, None)
                    if fut is not None and not fut.done():
                        if msg.get("success"):
                            fut.set_result(msg.get("result"))
                        else:
                            err = msg.get("error") or {}
                            fut.set_exception(
                                HaWsError(
                                    str(err.get("code", "unknown")),
                                    str(err.get("message", "command failed")),
                                    msg,
                                )
                            )
                elif mtype == "event":
                    queue = self._event_queues.get(mid)
                    if queue is not None:
                        queue.put_nowait(msg.get("event", {}))
                elif mtype == "pong":
                    fut = self._pending.pop(mid, None)
                    if fut is not None and not fut.done():
                        fut.set_result(None)
        except Exception as exc:  # connection dropped mid-read
            log.warning("WS reader stopped: %s", exc)
        finally:
            self._handle_disconnect(ConnectionError("websocket connection lost"))

    def _handle_disconnect(self, exc: Exception) -> None:
        self._ws = None
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)
        for queue in self._event_queues.values():
            queue.put_nowait(exc)

    async def aclose(self) -> None:
        """Close the connection permanently (no reconnect)."""
        self._closed = True
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None

    # ----------------------------------------------------------------- public

    async def call(self, command: str, **kwargs: Any) -> Any:
        """Send a command and await the matching-id result.

        Raises HaWsError on success=False, ConnectionError if the link drops
        before the result arrives (a later call will auto-reconnect).
        """
        ws = await self._ensure_connected()
        msg_id = self._next_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await ws.send(json.dumps({"id": msg_id, "type": command, **kwargs}))
            return await fut
        finally:
            self._pending.pop(msg_id, None)

    def subscribe(self, event_type: str | None = None) -> Subscription:
        """Async iterator of events (subscribe_events); unsubscribes on aclose().

        Both forms work:
            async for event in client.subscribe("state_changed"): ...
            sub = await client.subscribe("state_changed")  # subscribed eagerly
        """
        return Subscription(self, event_type)

    async def _start_subscription(self, sub: Subscription) -> None:
        ws = await self._ensure_connected()
        sub_id = self._next_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[sub_id] = fut
        self._event_queues[sub_id] = sub._queue
        try:
            msg: dict[str, Any] = {"id": sub_id, "type": "subscribe_events"}
            if sub._event_type is not None:
                msg["event_type"] = sub._event_type
            await ws.send(json.dumps(msg))
            await fut  # subscription confirmed by matching result
        except BaseException:
            self._pending.pop(sub_id, None)
            self._event_queues.pop(sub_id, None)
            raise
        finally:
            self._pending.pop(sub_id, None)
        sub._sub_id = sub_id
        sub._ws = ws

    async def wait_for_state(
        self, entity_id: str, to_state: str, timeout: float = 30.0
    ) -> dict[str, Any]:
        """Wait until entity_id reaches to_state; returns the state object.

        Subscribes first, then checks the current state (no race window).
        Raises asyncio.TimeoutError if the state is not reached in time.
        """

        async def _waiter() -> dict[str, Any]:
            sub = await self.subscribe("state_changed")
            try:
                states = await self.call("get_states")
                for st in states or []:
                    if st.get("entity_id") == entity_id and st.get("state") == to_state:
                        return st
                async for event in sub:
                    data = event.get("data") or {}
                    if data.get("entity_id") != entity_id:
                        continue
                    new_state = data.get("new_state") or {}
                    if new_state.get("state") == to_state:
                        return new_state
                raise ConnectionError("event stream ended before state was reached")
            finally:
                await sub.aclose()

        return await asyncio.wait_for(_waiter(), timeout)
