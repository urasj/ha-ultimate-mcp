"""realtime/ surface tests (W4).

Fakes: StubWs with a scripted subscribe async-generator + wait_for_state, a
StubSupervisor returning scripted log text, and a synthetic recorder sqlite DB
(built like tests/test_database.py) for the flatline scan. asyncio.run() drives
the async tools directly.
"""

import asyncio
import inspect
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from ultimate_mcp.context import DbFacade  # noqa: E402
from ultimate_mcp.tools.realtime import impl  # noqa: E402
from ultimate_mcp.tools.realtime.manifest import SURFACE  # noqa: E402

SCHEMA_SQL = (Path(__file__).parent / "fixtures" / "recorder_schema.sql").read_text(
    encoding="utf-8"
)
NOW = time.time()
HOUR = 3600


# --------------------------------------------------------------- fakes


class StubSub:
    """Scripted async-iterator subscription; records aclose()."""

    def __init__(self, events, raise_on_start=None) -> None:
        self._events = list(events)
        self._raise = raise_on_start
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._raise is not None:
            raise self._raise
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def aclose(self):
        self.closed = True


class StubWs:
    def __init__(self, events=None, wait_result=None, wait_exc=None,
                 sub_exc=None, call_results=None) -> None:
        self._events = events or []
        self._wait_result = wait_result
        self._wait_exc = wait_exc
        self._sub_exc = sub_exc
        self._call_results = call_results or {}
        self.subs: list = []

    def subscribe(self, event_type=None):
        sub = StubSub(self._events, raise_on_start=self._sub_exc)
        self.subs.append(sub)
        return sub

    async def wait_for_state(self, entity_id, to_state, timeout=30):
        if self._wait_exc is not None:
            raise self._wait_exc
        return self._wait_result

    async def call(self, command, **kwargs):
        val = self._call_results.get(command)
        if isinstance(val, Exception):
            raise val
        return val


class StubSupervisor:
    def __init__(self, logs=None) -> None:
        self._logs = logs or {}

    async def get(self, path):
        return self._logs.get(path, "")


class StubCtx:
    def __init__(self, ha_ws=None, supervisor=None, db_path=None) -> None:
        self.ha_ws = ha_ws
        self.supervisor = supervisor
        self.db = DbFacade(db_path=db_path) if db_path else None


# --------------------------------------------------------------- contract


def test_manifest_impl_parity():
    for spec in SURFACE.tools:
        fn = getattr(impl, spec.name, None)
        assert fn is not None, f"missing impl for {spec.name}"
        assert inspect.iscoroutinefunction(fn), f"{spec.name} must be async"


def test_flatline_gated_on_sqlite():
    by_name = {t.name: t for t in SURFACE.tools}
    assert by_name["state_flatline_scan"].requires == ("db:sqlite",)
    assert by_name["wait_for_state"].requires == ()


# --------------------------------------------------------------- wait_for_state


def test_wait_for_state_returns_on_change():
    ws = StubWs(wait_result={"entity_id": "light.kitchen", "state": "on"})
    res = asyncio.run(impl.wait_for_state(StubCtx(ha_ws=ws), "light.kitchen", "on", timeout=5))
    assert res["reached"] is True
    assert res["state"]["state"] == "on"


def test_wait_for_state_times_out_cleanly():
    ws = StubWs(wait_exc=asyncio.TimeoutError())
    res = asyncio.run(impl.wait_for_state(StubCtx(ha_ws=ws), "light.kitchen", "on", timeout=0.2))
    assert res["reached"] is False
    assert res["timed_out"] is True


def test_wait_for_state_ws_unavailable():
    ws = StubWs(wait_exc=ConnectionError("ws down"))
    res = asyncio.run(impl.wait_for_state(StubCtx(ha_ws=ws), "light.kitchen", "on"))
    assert res["error"] == "ws unavailable"


# --------------------------------------------------------------- event window


def test_event_window_collects_n_events():
    events = [
        {"event_type": "state_changed", "data": {"entity_id": f"light.{i}"}}
        for i in range(3)
    ]
    ws = StubWs(events=events)
    res = asyncio.run(impl.event_window_capture(StubCtx(ha_ws=ws), event_type=None, seconds=1))
    assert res["count"] == 3
    assert len(res["events"]) == 3
    assert ws.subs[0].closed is True  # subscription was closed


def test_event_window_respects_max_events():
    events = [{"i": i} for i in range(10)]
    ws = StubWs(events=events)
    res = asyncio.run(
        impl.event_window_capture(StubCtx(ha_ws=ws), seconds=1, max_events=4)
    )
    assert res["count"] == 4
    assert res["truncated"] is True


def test_event_window_ws_unavailable():
    ws = StubWs(sub_exc=ConnectionError("no ws"))
    res = asyncio.run(impl.event_window_capture(StubCtx(ha_ws=ws), seconds=0.3))
    assert res["error"] == "ws unavailable"


# --------------------------------------------------------------- log follow


def test_log_follow_until_match():
    sup = StubSupervisor(logs={"/core/logs": "line one\nERROR boom\nline three\n"})
    res = asyncio.run(
        impl.log_follow(StubCtx(supervisor=sup), source="core",
                        seconds=2, until_match="ERROR")
    )
    assert res["matched"] is True
    assert "ERROR boom" in res["matched_line"]


def test_log_follow_snapshot_no_pattern():
    sup = StubSupervisor(logs={"/supervisor/logs": "a\nb\nc\n"})
    res = asyncio.run(
        impl.log_follow(StubCtx(supervisor=sup), source="supervisor", seconds=0.3)
    )
    assert res["matched"] is False
    assert res["tail"] == ["a", "b", "c"]


# --------------------------------------------------------------- flatline scan


def _build_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_SQL)
    con.executemany(
        "INSERT INTO states_meta (metadata_id, entity_id) VALUES (?, ?)",
        [(1, "sensor.alive"), (2, "sensor.dead")],
    )
    rows = [
        (1, "22.0", NOW - 60),          # alive: last row 1 min ago
        (2, "on", NOW - 100 * HOUR),    # dead: last row 100 h ago
        (2, "on", NOW - 101 * HOUR),
    ]
    con.executemany(
        "INSERT INTO states (metadata_id, state, last_updated_ts) VALUES (?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def test_flatline_scan_flags_stale(tmp_path):
    db_path = tmp_path / "home-assistant_v2.db"
    _build_db(db_path)
    res = asyncio.run(impl.state_flatline_scan(StubCtx(db_path=db_path), threshold_hours=24))
    ids = [r["entity_id"] for r in res["flatlined"]]
    assert ids == ["sensor.dead"]  # only the stale one
    assert res["flatlined_count"] == 1
    assert res["flatlined"][0]["stale_hours"] >= 100


def test_flatline_scan_degrades_without_tables(tmp_path):
    db_path = tmp_path / "empty.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE placeholder (x INTEGER)")
    con.commit()
    con.close()
    res = asyncio.run(impl.state_flatline_scan(StubCtx(db_path=db_path)))
    assert "error" in res


# --------------------------------------------------------------- trace_next_run


def test_trace_next_run_returns_trace():
    trigger = {"event_type": "automation_triggered",
               "data": {"entity_id": "automation.morning", "name": "Morning"}}
    ws = StubWs(
        events=[
            {"event_type": "automation_triggered", "data": {"entity_id": "automation.other"}},
            trigger,
        ],
        call_results={
            "get_states": [
                {"entity_id": "automation.morning", "attributes": {"id": "1699"}}
            ],
            "trace/list": [{"run_id": "abc"}, {"run_id": "xyz"}],
            "trace/get": {"run_id": "xyz", "trace": {"steps": 3}},
        },
    )
    res = asyncio.run(impl.trace_next_run(StubCtx(ha_ws=ws), "automation.morning", timeout=2))
    assert res["fired"] is True
    assert res["item_id"] == "1699"
    assert res["run_id"] == "xyz"
    assert res["trace"]["trace"]["steps"] == 3


def test_trace_next_run_timeout():
    ws = StubWs(events=[])  # nothing ever fires -> generator exhausts
    res = asyncio.run(impl.trace_next_run(StubCtx(ha_ws=ws), "automation.morning", timeout=0.3))
    # empty stream ends -> treated as ws error/degraded; must not raise
    assert res.get("fired") in (False, None)
