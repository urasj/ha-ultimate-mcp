"""SafetyKernel unit tests (0.2.4): confirm-token TTL/args-hash binding,
checkpoint TTL, and canonical args hashing."""

import asyncio
import importlib
import sys
import time as _time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from ultimate_mcp.registry import RegisteredTool, Registry  # noqa: E402
from ultimate_mcp.spec import SurfaceSpec, Tier, ToolSpec  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _kernel(tmp_path, monkeypatch, options=None):
    """Fresh SafetyKernel with journal/undo rooted in tmp_path."""
    monkeypatch.setenv("UMCP_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("UMCP_HA_CONFIG", str(tmp_path / "ha"))
    (tmp_path / "ha").mkdir(exist_ok=True)
    import ultimate_mcp.context as context
    import ultimate_mcp.safety.kernel as kernel_mod

    importlib.reload(context)
    importlib.reload(kernel_mod)

    class StubCtx:
        pass

    ctx = StubCtx()
    ctx.options = {"destructive_enabled": True, **(options or {})}
    return kernel_mod, kernel_mod.SafetyKernel(ctx)


def _registry() -> Registry:
    surface = SurfaceSpec(name="fake", summary="", tools=(), impl_module="fake")
    reg = Registry()
    for name, tier in (
        ("t1_tool", Tier.T1_REVERSIBLE),
        ("t2_tool", Tier.T2_RISKY),
        ("t3_tool", Tier.T3_DESTRUCTIVE),
    ):
        spec = ToolSpec(name=name, summary="", tier=tier)
        reg.tools[name] = RegisteredTool(spec=spec, surface=surface)
    return reg


# ------------------------------------------------------------ args hashing

def test_canonical_args_hash_ignores_dry_run_and_key_order(tmp_path, monkeypatch):
    kernel_mod, _ = _kernel(tmp_path, monkeypatch)
    h = kernel_mod.canonical_args_hash
    assert h({"a": 1, "b": [2, 3]}) == h({"b": [2, 3], "a": 1})
    assert h({"a": 1, "dry_run": True}) == h({"a": 1, "dry_run": False}) == h({"a": 1})
    assert h({"a": 1}) != h({"a": 2})
    assert h({}) == h(None if False else {})  # stable for empty args


# ------------------------------------------------------------ confirm tokens

def test_token_roundtrip_consumes_token(tmp_path, monkeypatch):
    kernel_mod, k = _kernel(tmp_path, monkeypatch)
    reg = _registry()
    ahash = kernel_mod.canonical_args_hash({"x": 1})
    k._checkpoints.append({"checkpoint_id": "cp1", "created_at": _time.time(), "scope": "ha"})
    token = k.mint_token("t3_tool", ahash)
    run(k.authorize(reg, "t3_tool", False, token, None, args_hash=ahash))
    with pytest.raises(PermissionError, match="token_unknown"):
        run(k.authorize(reg, "t3_tool", False, token, None, args_hash=ahash))


def test_token_rejects_wrong_tool_and_wrong_args(tmp_path, monkeypatch):
    kernel_mod, k = _kernel(tmp_path, monkeypatch)
    reg = _registry()
    k._checkpoints.append({"checkpoint_id": "cp1", "created_at": _time.time(), "scope": "ha"})
    ahash = kernel_mod.canonical_args_hash({"x": 1})
    token = k.mint_token("t3_tool", ahash)
    other = kernel_mod.canonical_args_hash({"x": 2})
    with pytest.raises(PermissionError, match="token_args_mismatch"):
        run(k.authorize(reg, "t3_tool", False, token, None, args_hash=other))
    # a mismatch must NOT consume the token — the right args still work
    run(k.authorize(reg, "t3_tool", False, token, None, args_hash=ahash))


def test_token_ttl_expiry(tmp_path, monkeypatch):
    kernel_mod, k = _kernel(tmp_path, monkeypatch)
    reg = _registry()
    k._checkpoints.append({"checkpoint_id": "cp1", "created_at": _time.time(), "scope": "ha"})
    ahash = kernel_mod.canonical_args_hash({})
    token = k.mint_token("t3_tool", ahash)
    real = _time.time
    monkeypatch.setattr(
        kernel_mod.time, "time", lambda: real() + kernel_mod.TOKEN_TTL_SECONDS + 1
    )
    with pytest.raises(PermissionError, match="token_expired"):
        run(k.authorize(reg, "t3_tool", False, token, None, args_hash=ahash))


def test_token_unknown(tmp_path, monkeypatch):
    kernel_mod, k = _kernel(tmp_path, monkeypatch)
    reg = _registry()
    k._checkpoints.append({"checkpoint_id": "cp1", "created_at": _time.time(), "scope": "ha"})
    with pytest.raises(PermissionError, match="token_unknown"):
        run(k.authorize(reg, "t3_tool", False, "no-such-token", None,
                        args_hash=kernel_mod.canonical_args_hash({})))


# ------------------------------------------------------------ checkpoint TTL

def test_checkpoint_registers_with_timestamp_and_scope(tmp_path, monkeypatch):
    kernel_mod, k = _kernel(tmp_path, monkeypatch)

    class StubSup:
        async def post(self, path, body=None):
            return {"result": "ok", "data": {"slug": "abc123"}}

    k.ctx.supervisor = StubSup()
    out = run(k.checkpoint("homeassistant", "unit"))
    assert out["slug"] == "abc123"
    assert len(k._checkpoints) == 1
    cp = k._checkpoints[0]
    assert cp["checkpoint_id"] == "abc123"
    assert cp["scope"] == "homeassistant"
    assert abs(cp["created_at"] - _time.time()) < 5


def test_checkpoint_gate_passes_within_ttl_and_expires_after(tmp_path, monkeypatch):
    kernel_mod, k = _kernel(tmp_path, monkeypatch)
    reg = _registry()
    k._checkpoints.append({"checkpoint_id": "cp1", "created_at": _time.time(), "scope": "ha"})
    run(k.authorize(reg, "t2_tool", False, None, None))  # passes fresh
    k._checkpoints[0]["created_at"] -= k.checkpoint_ttl + 1
    with pytest.raises(PermissionError, match="checkpoint_required"):
        run(k.authorize(reg, "t2_tool", False, None, None))


def test_checkpoint_ttl_configurable_via_options(tmp_path, monkeypatch):
    kernel_mod, k = _kernel(tmp_path, monkeypatch, options={"checkpoint_ttl_seconds": 60})
    assert k.checkpoint_ttl == 60
    reg = _registry()
    k._checkpoints.append({"checkpoint_id": "cp1", "created_at": _time.time() - 120, "scope": "ha"})
    with pytest.raises(PermissionError, match="checkpoint_required"):
        run(k.authorize(reg, "t2_tool", False, None, None))


def test_external_checkpoint_ref_satisfies_gate(tmp_path, monkeypatch):
    kernel_mod, k = _kernel(tmp_path, monkeypatch)
    reg = _registry()
    run(k.authorize(reg, "t2_tool", False, None, "proxmox:snap-1"))  # no registry entry needed


def test_t1_never_needs_checkpoint_or_token(tmp_path, monkeypatch):
    kernel_mod, k = _kernel(tmp_path, monkeypatch)
    reg = _registry()
    run(k.authorize(reg, "t1_tool", False, None, None))


# --------------------------------------------- 0.2.5 write-ahead journal

def test_journal_open_update_fold(tmp_path, monkeypatch):
    kernel_mod, k = _kernel(tmp_path, monkeypatch)
    entry_id = k.journal_open("storage_edit", tool="storage_patch", undo_id="u-1")
    tail = k.journal_tail(10)
    entry = next(e for e in tail if e["id"] == entry_id)
    assert entry["status"] == "pending"
    assert entry["undo_id"] == "u-1"

    k.journal_update(entry_id, status="committed", backup_slug="abc")
    tail = k.journal_tail(10)
    entry = next(e for e in tail if e["id"] == entry_id)
    assert entry["status"] == "committed"
    assert entry["backup_slug"] == "abc"
    # raw update records are folded away, not surfaced as entries
    assert not any(e.get("action") == "journal_update" for e in tail)


def test_journal_find_folds_updates(tmp_path, monkeypatch):
    kernel_mod, k = _kernel(tmp_path, monkeypatch)
    entry_id = k.journal_open("fs_write_www", tool="fs_write_www")
    k.journal_update(entry_id, status="committed", target="/x/y")
    found = k._journal_find(entry_id)
    assert found["status"] == "committed"
    assert found["target"] == "/x/y"


def test_undo_works_on_pending_entry(tmp_path, monkeypatch):
    """A crash between write and commit leaves a pending entry; umcp_undo must
    still be able to restore from its undo copy."""
    kernel_mod, k = _kernel(tmp_path, monkeypatch)
    target = tmp_path / "ha" / "file.json"
    import json as _json
    target.write_text(_json.dumps({"v": 1}), encoding="utf-8")
    entry_id = k.journal_open("edit", tool="t")
    k.attach_undo_artifact(entry_id, target, str(target))
    target.write_text(_json.dumps({"v": 2}), encoding="utf-8")  # the "mutation"

    result = run(k.undo(entry_id))
    assert result["undoable"] is True, result
    assert _json.loads(target.read_text())["v"] == 1
    # entry was pending the whole time — never committed
    assert k._journal_find(entry_id)["status"] == "pending"
