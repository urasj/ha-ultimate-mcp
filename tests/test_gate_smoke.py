"""Gate-layer smoke tests (0.2.4) — full dry-run/apply cycle for one tool per tier.

Exercises umcp_call's orchestration (ultimate_mcp.gateway.call_tool) with the
real Registry + SafetyKernel and a stubbed Context, per the acceptance criteria:

  T1 fs_write_www : dry_run -> apply -> journal entry exists -> undo restores
  T2 storage_patch: apply w/o checkpoint -> checkpoint_required; after
                    umcp_checkpoint -> apply succeeds -> journaled -> undo works;
                    external_checkpoint_ref path accepted + journaled
  T3 stats_clear  : dry-run returns confirm_token; apply w/o token -> clear
                    error; with token -> succeeds; reuse -> rejected;
                    expired -> token_expired
"""

import asyncio
import json
import os
import sys
import tempfile
import time as _time
from pathlib import Path

# Env must be pinned BEFORE any ultimate_mcp import: context.py resolves
# HA_CONFIG_ROOT / DATA_DIR at import time.
_SANDBOX = tempfile.mkdtemp(prefix="umcp-gate-test-")
os.environ.setdefault("UMCP_HA_CONFIG", str(Path(_SANDBOX) / "config"))
os.environ.setdefault("UMCP_DATA", str(Path(_SANDBOX) / "data"))

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import pytest  # noqa: E402

from ultimate_mcp import gateway  # noqa: E402
from ultimate_mcp.context import FsFacade  # noqa: E402
from ultimate_mcp.registry import Registry  # noqa: E402
from ultimate_mcp.safety import kernel as kernel_mod  # noqa: E402
from ultimate_mcp.safety.kernel import SafetyKernel  # noqa: E402

# ------------------------------------------------------------------ fixtures

CONFIG_ENTRIES = {
    "version": 1,
    "minor_version": 5,
    "key": "core.config_entries",
    "data": {"entries": [{"entry_id": "ce-hue", "domain": "hue", "title": "Philips Hue"}]},
}

PATCH_ARGS = {
    "key": "core.config_entries",
    "json_patch": [{"op": "replace", "path": "/data/entries/0/title", "value": "Renamed Hue"}],
}


def run(coro):
    return asyncio.run(coro)


class StubSupervisor:
    """Records every REST interaction; answers /core/info with RUNNING."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get(self, path: str) -> dict:
        self.calls.append(("GET", path))
        if path == "/core/info":
            return {"result": "ok", "data": {"state": "running"}}
        return {"result": "ok", "data": {}}

    async def post(self, path: str, body: dict | None = None) -> dict:
        self.calls.append(("POST", path))
        if path == "/backups/new/partial":
            return {"result": "ok", "data": {"slug": "cafe0001"}}
        return {"result": "ok", "data": {}}


class StubWs:
    """Records recorder WS commands; always succeeds."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, command: str, **kwargs) -> dict:
        self.calls.append((command, kwargs))
        return {"success": True}


class StubCtx:
    def __init__(self, root: Path, destructive: bool = False) -> None:
        self.fs = FsFacade(root=root)
        self.supervisor = StubSupervisor()
        self.ha_ws = StubWs()
        self.options = {"destructive_enabled": destructive}


def build_config(root: Path) -> None:
    storage = root / ".storage"
    storage.mkdir(parents=True)
    (storage / "core.config_entries").write_text(
        json.dumps(CONFIG_ENTRIES, indent=2), encoding="utf-8"
    )
    (root / "www").mkdir()
    (root / "www" / "note.txt").write_text("old content", encoding="utf-8")


@pytest.fixture()
def harness(tmp_path: Path):
    root = tmp_path / "config"
    build_config(root)
    ctx = StubCtx(root)
    registry = Registry()
    registry.load_manifests()
    safety = SafetyKernel(ctx)
    return ctx, registry, safety


@pytest.fixture()
def t3_harness(tmp_path: Path):
    root = tmp_path / "config"
    build_config(root)
    ctx = StubCtx(root, destructive=True)
    registry = Registry()
    registry.load_manifests()
    safety = SafetyKernel(ctx)
    # T3 also sits behind the T2+ checkpoint gate — satisfy it up front.
    run(safety.checkpoint("homeassistant", "t3-test"))
    return ctx, registry, safety


def call(ctx, registry, safety, name, args=None, **kw):
    return run(gateway.call_tool(ctx, registry, safety, name, args=args, **kw))


# ------------------------------------------------------------------ T1 cycle

def test_t1_dry_run_then_apply_then_undo(harness):
    ctx, registry, safety = harness
    live = ctx.fs.root / "www" / "note.txt"
    args = {"path": "note.txt", "content": "new content"}

    dry = call(ctx, registry, safety, "fs_write_www", args)  # dry_run defaults True
    assert dry["dry_run"] is True
    assert live.read_text(encoding="utf-8") == "old content"  # untouched

    out = call(ctx, registry, safety, "fs_write_www", args, dry_run=False)
    assert out.get("written") is True
    assert live.read_text(encoding="utf-8") == "new content"

    # the apply is journaled, and undo restores the pre-change content
    assert out.get("journal_id"), f"T1 apply was not journaled: {out}"
    entry_ids = [e["id"] for e in safety.journal_tail(20)]
    assert out["journal_id"] in entry_ids
    undo = run(safety.undo(out["journal_id"]))
    assert undo["undoable"] is True, undo
    assert live.read_text(encoding="utf-8") == "old content"


# ------------------------------------------------------------------ T2 cycle

def test_t2_apply_without_checkpoint_is_blocked_with_remediation(harness):
    ctx, registry, safety = harness
    with pytest.raises(PermissionError) as ei:
        call(ctx, registry, safety, "storage_patch", dict(PATCH_ARGS), dry_run=False)
    msg = str(ei.value)
    assert "checkpoint_required" in msg
    assert "umcp_checkpoint" in msg
    assert "external_checkpoint_ref" in msg
    # nothing was mutated
    doc = ctx.fs.read_storage("core.config_entries")
    assert doc["data"]["entries"][0]["title"] == "Philips Hue"


def test_t2_dry_run_reports_checkpoint_status(harness):
    ctx, registry, safety = harness
    dry = call(ctx, registry, safety, "storage_patch", dict(PATCH_ARGS))
    assert dry["checkpoint"]["satisfied"] is False
    assert "checkpoint_required" in dry

    run(safety.checkpoint("homeassistant", "test"))
    dry2 = call(ctx, registry, safety, "storage_patch", dict(PATCH_ARGS))
    assert dry2["checkpoint"]["satisfied"] is True
    assert "checkpoint_required" not in dry2


def test_t2_full_cycle_with_checkpoint(harness):
    ctx, registry, safety = harness
    cp = run(safety.checkpoint("homeassistant", "test"))
    assert cp["slug"] == "cafe0001"

    out = call(ctx, registry, safety, "storage_patch", dict(PATCH_ARGS), dry_run=False)
    assert "changed" in out, f"apply did not execute: {out}"
    doc = ctx.fs.read_storage("core.config_entries")
    assert doc["data"]["entries"][0]["title"] == "Renamed Hue"

    # journaled + undoable
    assert out.get("journal_id")
    undo = run(safety.undo(out["journal_id"]))
    assert undo["undoable"] is True, undo
    doc = ctx.fs.read_storage("core.config_entries")
    assert doc["data"]["entries"][0]["title"] == "Philips Hue"


def test_t2_external_checkpoint_ref_passes_gate_and_is_journaled(harness):
    ctx, registry, safety = harness  # NO umcp_checkpoint call
    ref = "proxmox:vm100-snap-20260711"
    out = call(
        ctx, registry, safety, "storage_patch", dict(PATCH_ARGS),
        dry_run=False, external_checkpoint_ref=ref,
    )
    assert "changed" in out, f"apply did not execute: {out}"
    doc = ctx.fs.read_storage("core.config_entries")
    assert doc["data"]["entries"][0]["title"] == "Renamed Hue"
    # the external ref is recorded in the journal
    tail = json.dumps(safety.journal_tail(20))
    assert ref in tail


def test_t2_checkpoint_expires_after_ttl(harness):
    ctx, registry, safety = harness
    run(safety.checkpoint("homeassistant", "test"))
    # age the registered checkpoint past its TTL
    for cp in safety._checkpoints:
        cp["created_at"] -= safety.checkpoint_ttl + 1
    with pytest.raises(PermissionError, match="checkpoint_required"):
        call(ctx, registry, safety, "storage_patch", dict(PATCH_ARGS), dry_run=False)


# ------------------------------------------------------------------ T3 cycle

def test_t3_dry_run_returns_confirm_token(t3_harness):
    ctx, registry, safety = t3_harness
    args = {"statistic_ids": ["sensor.dead_1", "sensor.dead_2"]}
    dry = call(ctx, registry, safety, "stats_clear", args)
    assert isinstance(dry.get("confirm_token"), str) and dry["confirm_token"], (
        f"T3 dry-run did not mint a confirm_token: {dry}"
    )
    assert dry.get("confirm_token_ttl_seconds", 0) > 0
    assert ctx.ha_ws.calls == []  # dry run made no WS call


def test_t3_apply_without_token_is_clear_error(t3_harness):
    ctx, registry, safety = t3_harness
    args = {"statistic_ids": ["sensor.dead_1"]}
    with pytest.raises(PermissionError, match="token_missing"):
        call(ctx, registry, safety, "stats_clear", args, dry_run=False)
    assert ctx.ha_ws.calls == []


def test_t3_full_cycle_and_single_use(t3_harness):
    ctx, registry, safety = t3_harness
    args = {"statistic_ids": ["sensor.dead_1", "sensor.dead_2"]}
    dry = call(ctx, registry, safety, "stats_clear", args)
    token = dry["confirm_token"]

    out = call(ctx, registry, safety, "stats_clear", args, dry_run=False, confirm_token=token)
    assert out.get("executed") is True, out
    assert ctx.ha_ws.calls == [
        ("recorder/clear_statistics", {"statistic_ids": ["sensor.dead_1", "sensor.dead_2"]})
    ]
    assert out.get("journal_id")  # T3 applies are journaled too

    # token is single-use: replay is rejected
    with pytest.raises(PermissionError, match="token_unknown"):
        call(ctx, registry, safety, "stats_clear", args, dry_run=False, confirm_token=token)
    assert len(ctx.ha_ws.calls) == 1


def test_t3_token_bound_to_args(t3_harness):
    ctx, registry, safety = t3_harness
    dry = call(ctx, registry, safety, "stats_clear", {"statistic_ids": ["sensor.a"]})
    token = dry["confirm_token"]
    with pytest.raises(PermissionError, match="token_args_mismatch"):
        call(
            ctx, registry, safety, "stats_clear",
            {"statistic_ids": ["sensor.b"]}, dry_run=False, confirm_token=token,
        )
    assert ctx.ha_ws.calls == []


def test_t3_token_expired(t3_harness, monkeypatch):
    ctx, registry, safety = t3_harness
    args = {"statistic_ids": ["sensor.a"]}
    dry = call(ctx, registry, safety, "stats_clear", args)
    token = dry["confirm_token"]

    real_time = _time.time
    monkeypatch.setattr(
        kernel_mod.time, "time", lambda: real_time() + kernel_mod.TOKEN_TTL_SECONDS + 60
    )
    with pytest.raises(PermissionError, match="token_expired"):
        call(ctx, registry, safety, "stats_clear", args, dry_run=False, confirm_token=token)
    assert ctx.ha_ws.calls == []


def test_t3_disabled_master_switch(tmp_path):
    root = tmp_path / "config"
    build_config(root)
    ctx = StubCtx(root, destructive=False)
    registry = Registry()
    registry.load_manifests()
    safety = SafetyKernel(ctx)
    run(safety.checkpoint("homeassistant", "test"))
    with pytest.raises(PermissionError, match="destructive_enabled"):
        call(ctx, registry, safety, "stats_clear",
             {"statistic_ids": ["sensor.a"]}, dry_run=False, confirm_token="whatever")


# ------------------------------------------------- regression: T0 untouched

def test_t0_read_needs_no_gate(harness):
    ctx, registry, safety = harness
    out = call(ctx, registry, safety, "storage_read", {"key": "core.config_entries"})
    assert out["document"]["key"] == "core.config_entries"
