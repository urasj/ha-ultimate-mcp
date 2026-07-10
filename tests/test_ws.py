"""HaWsClient tests against a fake HA WebSocket server (W0)."""
import asyncio
import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from websockets.asyncio.server import serve  # noqa: E402

from ultimate_mcp.ws import HaWsAuthError, HaWsClient, HaWsError  # noqa: E402

GOOD_TOKEN = "good-supervisor-token"


class FakeHa:
    """Minimal HA-core WS protocol: auth handshake + a few canned commands."""

    def __init__(self) -> None:
        self.auth_tokens: list = []
        self.unsubscribed: list = []
        self.connections = 0

    async def handler(self, ws) -> None:
        self.connections += 1
        await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.7.1"}))
        msg = json.loads(await ws.recv())
        self.auth_tokens.append(msg.get("access_token"))
        if msg.get("access_token") != GOOD_TOKEN:
            await ws.send(json.dumps({"type": "auth_invalid", "message": "bad token"}))
            await ws.close()
            return
        await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.7.1"}))

        async for raw in ws:
            msg = json.loads(raw)
            mid, mtype = msg["id"], msg["type"]

            def result(payload):
                return json.dumps(
                    {"id": mid, "type": "result", "success": True, "result": payload}
                )

            if mtype == "echo":
                await ws.send(result({"value": msg.get("value")}))
            elif mtype == "slow_echo":

                async def _later(mid=mid, value=msg.get("value")):
                    await asyncio.sleep(0.1)
                    await ws.send(
                        json.dumps(
                            {"id": mid, "type": "result", "success": True,
                             "result": {"value": value}}
                        )
                    )

                asyncio.get_running_loop().create_task(_later())
            elif mtype == "boom":
                await ws.send(
                    json.dumps(
                        {"id": mid, "type": "result", "success": False,
                         "error": {"code": "invalid_format", "message": "kaboom"}}
                    )
                )
            elif mtype == "get_states":
                await ws.send(result([{"entity_id": "light.kitchen", "state": "off"}]))
            elif mtype == "subscribe_events":
                await ws.send(result(None))
                # fire two events on this subscription: kitchen -> on, hall -> on
                for entity in ("light.hall", "light.kitchen"):
                    await ws.send(
                        json.dumps(
                            {
                                "id": mid,
                                "type": "event",
                                "event": {
                                    "event_type": "state_changed",
                                    "data": {
                                        "entity_id": entity,
                                        "new_state": {"entity_id": entity, "state": "on"},
                                    },
                                },
                            }
                        )
                    )
            elif mtype == "unsubscribe_events":
                self.unsubscribed.append(msg.get("subscription"))
                await ws.send(result(None))
            elif mtype == "die":
                await ws.close()
                return
            else:
                await ws.send(
                    json.dumps(
                        {"id": mid, "type": "result", "success": False,
                         "error": {"code": "unknown_command", "message": mtype}}
                    )
                )


@pytest_asyncio.fixture
async def fake_ha():
    fake = FakeHa()
    server = await serve(fake.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}", fake
    finally:
        server.close()
        await server.wait_closed()


def make_client(url: str, token: str = GOOD_TOKEN, **kw) -> HaWsClient:
    kw.setdefault("backoff_initial", 0.05)
    kw.setdefault("max_connect_attempts", 3)
    return HaWsClient(url=url, token=token, **kw)


@pytest.mark.asyncio
async def test_auth_handshake_sends_token(fake_ha):
    url, fake = fake_ha
    client = make_client(url)
    result = await client.call("get_states")
    assert result == [{"entity_id": "light.kitchen", "state": "off"}]
    assert fake.auth_tokens == [GOOD_TOKEN]
    assert client.connected
    await client.aclose()


@pytest.mark.asyncio
async def test_auth_invalid_raises_without_retry(fake_ha):
    url, fake = fake_ha
    client = make_client(url, token="wrong-token")
    with pytest.raises(HaWsAuthError):
        await client.call("get_states")
    assert fake.auth_tokens == ["wrong-token"]  # exactly one attempt, no retry loop


@pytest.mark.asyncio
async def test_call_id_matching_out_of_order(fake_ha):
    url, _ = fake_ha
    client = make_client(url)
    # slow_echo answers ~100ms late, echo answers immediately -> replies arrive
    # out of order and must be matched back to their request ids.
    slow, fast = await asyncio.gather(
        client.call("slow_echo", value="first"),
        client.call("echo", value="second"),
    )
    assert slow == {"value": "first"}
    assert fast == {"value": "second"}
    await client.aclose()


@pytest.mark.asyncio
async def test_error_result_raises_ha_ws_error(fake_ha):
    url, _ = fake_ha
    client = make_client(url)
    with pytest.raises(HaWsError) as exc_info:
        await client.call("boom")
    assert exc_info.value.code == "invalid_format"
    assert "kaboom" in str(exc_info.value)
    # the connection is still usable after an error result
    assert await client.call("echo", value=1) == {"value": 1}
    await client.aclose()


@pytest.mark.asyncio
async def test_subscribe_yields_events_and_unsubscribes_on_aclose(fake_ha):
    url, fake = fake_ha
    client = make_client(url)
    sub = client.subscribe("state_changed")
    events = []
    async for event in sub:
        events.append(event)
        if len(events) == 2:
            break
    await sub.aclose()
    assert [e["data"]["entity_id"] for e in events] == ["light.hall", "light.kitchen"]
    assert fake.unsubscribed == [sub.subscription_id]
    # closed subscription stops iterating
    assert [e async for e in sub] == []
    await client.aclose()


@pytest.mark.asyncio
async def test_wait_for_state_via_events(fake_ha):
    url, _ = fake_ha
    client = make_client(url)
    # current state is off; the fake fires a state_changed -> on right after subscribe
    state = await client.wait_for_state("light.kitchen", "on", timeout=5)
    assert state["state"] == "on"
    assert state["entity_id"] == "light.kitchen"
    await client.aclose()


@pytest.mark.asyncio
async def test_wait_for_state_timeout(fake_ha):
    url, _ = fake_ha
    client = make_client(url)
    with pytest.raises(asyncio.TimeoutError):
        await client.wait_for_state("light.kitchen", "purple", timeout=0.3)
    await client.aclose()


@pytest.mark.asyncio
async def test_auto_reconnect_after_connection_drop(fake_ha):
    url, fake = fake_ha
    client = make_client(url)
    assert await client.call("echo", value=1) == {"value": 1}
    with pytest.raises(ConnectionError):
        await client.call("die")  # server hangs up before answering
    # next call transparently reconnects (fresh auth handshake)
    assert await client.call("echo", value=2) == {"value": 2}
    assert fake.connections == 2
    assert fake.auth_tokens == [GOOD_TOKEN, GOOD_TOKEN]
    await client.aclose()
