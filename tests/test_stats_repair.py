"""stats_repair/ surface tests (W5).

StubWs records recorder/* WS calls; a synthetic statistics DB (built like
tests/test_database.py) drives stats_anomaly_scan. asyncio.run() executes the
async tools directly, so no pytest-asyncio config is needed.
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
from ultimate_mcp.tools.stats_repair import impl  # noqa: E402
from ultimate_mcp.tools.stats_repair.manifest import SURFACE  # noqa: E402

SCHEMA_SQL = (Path(__file__).parent / "fixtures" / "recorder_schema.sql").read_text(
    encoding="utf-8"
)
NOW = time.time()
HOUR = 3600


def _build_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_SQL)
    # sensor.energy: sum-type, valid unit. sensor.temp: mean-type, MISSING unit.
    con.executemany(
        "INSERT INTO statistics_meta (id, statistic_id, source, unit_of_measurement,"
        " has_mean, has_sum) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "sensor.energy", "recorder", "kWh", 0, 1),
            (2, "sensor.temp", "recorder", "", 1, 0),  # missing unit -> unit_issue
        ],
    )
    base = NOW - 10 * HOUR
    stat_rows = []
    # Energy sum: 10,20,30 then DROPS to 25 (sum_decrease) then a NEGATIVE sum.
    energy_sums = [10.0, 20.0, 30.0, 25.0, -5.0]
    for i, s in enumerate(energy_sums):
        stat_rows.append((1, base + i * HOUR, None, s))
    # Temp mean: 20,21,22 then 500 (spike: 500 > 10*22).
    temp_means = [20.0, 21.0, 22.0, 500.0]
    for i, m in enumerate(temp_means):
        stat_rows.append((2, base + i * HOUR, m, None))
    con.executemany(
        "INSERT INTO statistics (metadata_id, start_ts, mean, sum) VALUES (?, ?, ?, ?)",
        stat_rows,
    )
    con.commit()
    con.close()


class StubWs:
    """Records .call(command, **kwargs); returns scripted results."""

    def __init__(self, results=None, raise_exc=None) -> None:
        self.calls: list[tuple] = []
        self._results = results or {}
        self._raise = raise_exc

    async def call(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self._raise is not None:
            raise self._raise
        return self._results.get(command, [])


class StubSupervisor:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def core_api(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {}


class StubCtx:
    def __init__(self, db_path=None, ws=None) -> None:
        self.db = DbFacade(db_path=db_path) if db_path else None
        self.ha_ws = ws or StubWs()
        self.supervisor = StubSupervisor()


@pytest.fixture()
def ctx(tmp_path):
    db_path = tmp_path / "home-assistant_v2.db"
    _build_db(db_path)
    return StubCtx(db_path=db_path)


# ---------------------------------------------------------------- contract
def test_manifest_impl_parity():
    for spec in SURFACE.tools:
        fn = getattr(impl, spec.name, None)
        assert fn is not None, f"missing impl for {spec.name}"
        assert inspect.iscoroutinefunction(fn), f"{spec.name} impl must be async"


def test_surface_gate_is_recorder_not_db():
    assert SURFACE.requires == ("integration:recorder",)
    anomaly = {t.name: t for t in SURFACE.tools}["stats_anomaly_scan"]
    assert anomaly.requires == ("db:sqlite",)  # only this tool needs the DB
    assert int({t.name: t for t in SURFACE.tools}["stats_clear"].tier) == 3


# ---------------------------------------------------------------- reads
def test_stats_list_parses():
    ws = StubWs(results={"recorder/list_statistic_ids": [{"statistic_id": "sensor.energy"}]})
    ctx = StubCtx(ws=ws)
    res = asyncio.run(impl.stats_list(ctx))
    assert res["count"] == 1
    assert res["statistic_ids"][0]["statistic_id"] == "sensor.energy"
    assert ws.calls[0][0] == "recorder/list_statistic_ids"


def test_stats_list_degrades_on_ws_error():
    ws = StubWs(raise_exc=ConnectionError("ws down"))
    res = asyncio.run(impl.stats_list(StubCtx(ws=ws)))
    assert "error" in res and "note" in res


def test_anomaly_scan_flags_negative_spike_and_reset(ctx):
    res = asyncio.run(impl.stats_anomaly_scan(ctx, days=30))
    kinds = res["counts_by_kind"]
    assert kinds.get("negative_sum", 0) >= 1
    assert kinds.get("sum_decrease", 0) >= 1
    assert kinds.get("mean_spike", 0) >= 1
    unit_ids = {u["statistic_id"] for u in res["unit_issues"]}
    assert "sensor.temp" in unit_ids  # missing unit flagged


def test_anomaly_scan_degrades_without_statistics_table(tmp_path):
    db_path = tmp_path / "empty.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE placeholder (x INTEGER)")
    con.commit()
    con.close()
    res = asyncio.run(impl.stats_anomaly_scan(StubCtx(db_path=db_path)))
    assert "error" in res


# ---------------------------------------------------------------- writes
def test_stats_import_dry_run_no_ws_write():
    ws = StubWs()
    ctx = StubCtx(ws=ws)
    res = asyncio.run(
        impl.stats_import(ctx, metadata={"statistic_id": "sensor.energy"}, stats=[{"start": "x"}])
    )
    assert res["dry_run"] is True
    assert res["command"] == "recorder/import_statistics"
    assert ws.calls == []  # nothing hit the WS


def test_stats_import_wet_run_calls_ws():
    ws = StubWs(results={"recorder/import_statistics": {"ok": True}})
    ctx = StubCtx(ws=ws)
    res = asyncio.run(
        impl.stats_import(
            ctx, metadata={"statistic_id": "sensor.energy"}, stats=[{"start": "x"}], dry_run=False
        )
    )
    assert res["dry_run"] is False
    assert ws.calls and ws.calls[0][0] == "recorder/import_statistics"


def test_stats_clear_dry_run_default_and_no_ws():
    ws = StubWs()
    res = asyncio.run(impl.stats_clear(StubCtx(ws=ws), statistic_ids=["sensor.energy"]))
    assert res["dry_run"] is True
    assert "DESTRUCTIVE" in res["note"]
    assert ws.calls == []
