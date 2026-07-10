"""database/ surface tests (W3) — synthetic 2026-era recorder DB in tmp_path.

Async tools are driven with asyncio.run() directly so no pytest-asyncio
configuration is required.
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
from ultimate_mcp.tools.database import impl  # noqa: E402
from ultimate_mcp.tools.database.manifest import SURFACE  # noqa: E402

SCHEMA_SQL = (Path(__file__).parent / "fixtures" / "recorder_schema.sql").read_text(
    encoding="utf-8"
)
NOW = time.time()
HOUR = 3600


def _build_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_SQL)

    con.executemany(
        "INSERT INTO states_meta (metadata_id, entity_id) VALUES (?, ?)",
        [(1, "sensor.noisy_power"), (2, "sensor.quiet_temp"), (3, "light.old_lamp")],
    )

    big_attrs = '{"friendly_name": "Noisy Power", "history": [' + ",".join(["1"] * 500) + "]}"
    con.executemany(
        "INSERT INTO state_attributes (attributes_id, hash, shared_attrs) VALUES (?, ?, ?)",
        [(1, 111, big_attrs), (2, 222, '{"friendly_name": "Quiet Temp"}')],
    )

    # sensor.noisy_power: 300 rows in the last ~50 minutes (inside any recent window)
    rows = [(1, str(i), 1, NOW - i * 10) for i in range(300)]
    # sensor.quiet_temp: 10 rows over the last ~100 minutes
    rows += [(2, str(i), 2, NOW - i * 600) for i in range(10)]
    # light.old_lamp: 50 rows ~100 hours ago (outside a 24 h window, purgeable at keep_days=2)
    rows += [(3, "on", None, NOW - 100 * HOUR - i * 60) for i in range(50)]
    con.executemany(
        "INSERT INTO states (metadata_id, state, attributes_id, last_updated_ts)"
        " VALUES (?, ?, ?, ?)",
        rows,
    )

    con.executemany(
        "INSERT INTO event_types (event_type_id, event_type) VALUES (?, ?)",
        [(1, "state_changed"), (2, "call_service")],
    )
    events = [(1, NOW - i * 20) for i in range(200)]  # recent state_changed
    events += [(2, NOW - i * 300) for i in range(20)]  # recent call_service
    events += [(1, NOW - 90 * HOUR - i) for i in range(30)]  # old, outside 24 h
    con.executemany(
        "INSERT INTO events (event_type_id, time_fired_ts) VALUES (?, ?)", events
    )

    con.execute(
        "INSERT INTO statistics_meta (id, statistic_id, source, unit_of_measurement,"
        " has_mean, has_sum) VALUES (1, 'sensor.energy', 'recorder', 'kWh', 0, 1)"
    )
    # Hourly rows over the last 48 h, hours 20-22 missing (a 3-hour gap),
    # hours 40-41 present but fully NULL (a NULL run).
    base = NOW - 48 * HOUR
    stat_rows = []
    for h in range(48):
        if 20 <= h < 23:
            continue
        if h in (40, 41):
            stat_rows.append((1, base + h * HOUR, None, None))
        else:
            stat_rows.append((1, base + h * HOUR, None, float(h)))
    con.executemany(
        "INSERT INTO statistics (metadata_id, start_ts, mean, sum) VALUES (?, ?, ?, ?)",
        stat_rows,
    )

    con.executemany(
        'INSERT INTO recorder_runs (run_id, start, "end", closed_incorrectly, created)'
        " VALUES (?, ?, ?, ?, ?)",
        [
            (1, "2026-07-01 00:00:00", "2026-07-02 00:00:00", 0, "2026-07-01 00:00:00"),
            (2, "2026-07-02 00:00:01", None, 1, "2026-07-02 00:00:01"),
            (3, "2026-07-03 00:00:00", None, 0, "2026-07-03 00:00:00"),
        ],
    )
    con.execute(
        "INSERT INTO schema_changes (schema_version, changed) VALUES (50, '2026-07-01')"
    )
    con.commit()
    con.close()


class StubSupervisor:
    """Records core_api calls instead of hitting the Supervisor."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def core_api(self, method, path, body=None):
        self.calls.append((method, path, body))
        return []


class StubCtx:
    def __init__(self, db_path: Path) -> None:
        self.db = DbFacade(db_path=db_path)
        self.supervisor = StubSupervisor()


@pytest.fixture()
def ctx(tmp_path):
    db_path = tmp_path / "home-assistant_v2.db"
    _build_db(db_path)
    return StubCtx(db_path)


# ---------------------------------------------------------------- contract


def test_manifest_impl_parity():
    """Every ToolSpec in the manifest has a matching async impl function."""
    for spec in SURFACE.tools:
        fn = getattr(impl, spec.name, None)
        assert fn is not None, f"missing impl for {spec.name}"
        assert inspect.iscoroutinefunction(fn), f"{spec.name} impl must be async"


def test_purge_execute_is_t2():
    spec = {t.name: t for t in SURFACE.tools}["db_purge_execute"]
    assert int(spec.tier) == 2


# ---------------------------------------------------------------- read tools


def test_entity_cost_ordering(ctx):
    rows = asyncio.run(impl.db_entity_cost(ctx, top=10))
    assert [r["entity_id"] for r in rows[:2]] == ["sensor.noisy_power", "light.old_lamp"]
    counts = [r["state_rows"] for r in rows]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] == 300


def test_churn_window_filter(ctx):
    res = asyncio.run(impl.db_churn_top(ctx, hours=24, top=10))
    ents = {r["entity_id"]: r["state_rows"] for r in res["entities"]}
    assert ents["sensor.noisy_power"] == 300
    assert ents["sensor.quiet_temp"] == 10
    assert "light.old_lamp" not in ents  # its rows are ~100 h old


def test_churn_degrades_without_states_table(tmp_path):
    db_path = tmp_path / "empty.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE placeholder (x INTEGER)")
    con.commit()
    con.close()
    res = asyncio.run(impl.db_churn_top(StubCtx(db_path)))
    assert "error" in res  # degraded, not raised


def test_event_firehose_window(ctx):
    res = asyncio.run(impl.db_event_firehose(ctx, hours=24, top=10))
    vols = {r["event_type"]: r["events"] for r in res["event_types"]}
    assert vols["state_changed"] == 200  # 30 old rows excluded by the window
    assert vols["call_service"] == 20


def test_stats_gaps_and_null_runs(ctx):
    res = asyncio.run(impl.db_stats_gaps(ctx, days=7))
    assert len(res["gaps"]) == 1
    gap = res["gaps"][0]
    assert gap["statistic_id"] == "sensor.energy"
    assert gap["missing_hours"] == 3
    nulls = {r["statistic_id"]: r["null_rows"] for r in res["null_value_rows"]}
    assert nulls["sensor.energy"] == 2


def test_stats_gaps_filter_by_statistic_id(ctx):
    res = asyncio.run(impl.db_stats_gaps(ctx, statistic_id="sensor.does_not_exist"))
    assert res["gaps"] == []


def test_attr_bloat(ctx):
    res = asyncio.run(impl.db_attr_bloat(ctx, top=5))
    top_attr = res["attributes"][0]
    assert top_attr["attributes_id"] == 1
    assert top_attr["used_by_states"] == 300
    assert top_attr["example_entity"] == "sensor.noisy_power"
    assert top_attr["attr_bytes"] > res["attributes"][1]["attr_bytes"]


def test_recorder_advisor_yaml(ctx):
    res = asyncio.run(impl.db_recorder_advisor(ctx, threshold_rows=100))
    ids = [c["entity_id"] for c in res["candidates"]]
    assert ids == ["sensor.noisy_power"]  # 300 rows >= 100; others below threshold
    assert res["candidates"][0]["rows_last_24h"] == 300
    assert res["projected_row_savings"] == 300
    yaml = res["recorder_yaml"]
    assert "recorder:" in yaml and "exclude:" in yaml
    assert "- sensor.noisy_power" in yaml
    assert "sensor.quiet_temp" not in yaml


def test_recorder_advisor_no_candidates(ctx):
    res = asyncio.run(impl.db_recorder_advisor(ctx, threshold_rows=10_000))
    assert res["candidates"] == []
    assert "nothing to exclude" in res["recorder_yaml"]


def test_integrity_check_ok(ctx):
    res = asyncio.run(impl.db_integrity_check(ctx))
    assert res["ok"] is True
    assert res["integrity_check"] == ["ok"]
    assert isinstance(res["page_count"], int) and res["page_count"] > 0
    assert res["db_bytes"] == res["page_count"] * res["page_size"]


def test_restart_history(ctx):
    res = asyncio.run(impl.db_restart_history(ctx, limit=20))
    assert len(res["runs"]) == 3
    assert res["runs"][0]["run_id"] == 3  # newest first
    assert res["unclean_shutdowns"] == 1


# ---------------------------------------------------------------- purge (T2)


def test_purge_execute_dry_run_no_service_call(ctx):
    res = asyncio.run(impl.db_purge_execute(ctx, keep_days=2, dry_run=True))
    assert res["dry_run"] is True
    assert res["service"] == "recorder.purge"
    assert res["purgeable_state_rows"] == 50  # only light.old_lamp's ~100 h old rows
    assert ctx.supervisor.calls == []  # nothing hit the Supervisor


def test_purge_execute_calls_service(ctx):
    res = asyncio.run(
        impl.db_purge_execute(
            ctx, keep_days=2, entity_ids=["light.old_lamp"], dry_run=False
        )
    )
    assert res["executed"] is True
    assert ctx.supervisor.calls == [
        ("POST", "services/recorder/purge_entities",
         {"keep_days": 2, "entity_id": ["light.old_lamp"]})
    ]
    assert res["per_entity"][0]["purgeable_rows"] == 50
