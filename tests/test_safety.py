"""SafetyKernel journal/undo tests (W0)."""

import asyncio
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))


def _kernel(tmp_path, monkeypatch):
    """Fresh SafetyKernel with journal/undo rooted in tmp_path."""
    monkeypatch.setenv("UMCP_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("UMCP_HA_CONFIG", str(tmp_path / "ha"))
    (tmp_path / "ha").mkdir(exist_ok=True)
    import ultimate_mcp.context as context
    import ultimate_mcp.safety.kernel as kernel_mod

    importlib.reload(context)
    importlib.reload(kernel_mod)

    class StubCtx:
        options = {"destructive_enabled": False}

    return kernel_mod, kernel_mod.SafetyKernel(StubCtx())


def test_record_and_tail_roundtrip(tmp_path, monkeypatch):
    kernel_mod, k = _kernel(tmp_path, monkeypatch)
    entry_id = k.record("test_action", "some/target", note="hello")
    tail = k.journal_tail(5)
    assert tail[-1]["id"] == entry_id
    assert tail[-1]["action"] == "test_action"
    assert tail[-1]["note"] == "hello"


def test_record_copies_undo_artifact(tmp_path, monkeypatch):
    kernel_mod, k = _kernel(tmp_path, monkeypatch)
    target = tmp_path / "ha" / "file.json"
    target.write_text(json.dumps({"v": 1}), encoding="utf-8")
    entry_id = k.record("edit", str(target), undo_artifact_path=target)
    artifact = kernel_mod.UNDO_DIR / entry_id / "file.json"
    assert artifact.exists()
    assert json.loads(artifact.read_text()) == {"v": 1}


def test_undo_restores_prechange_content(tmp_path, monkeypatch):
    kernel_mod, k = _kernel(tmp_path, monkeypatch)
    target = tmp_path / "ha" / "file.json"
    target.write_text(json.dumps({"v": 1}), encoding="utf-8")
    entry_id = k.record("edit", str(target), undo_artifact_path=target)
    target.write_text(json.dumps({"v": 2}), encoding="utf-8")  # the "change"
    result = asyncio.run(k.undo(entry_id))
    assert result["undoable"] is True
    assert json.loads(target.read_text()) == {"v": 1}
    assert k.journal_tail(1)[0]["action"] == "undo"


def test_undo_without_artifact_reports_not_undoable(tmp_path, monkeypatch):
    kernel_mod, k = _kernel(tmp_path, monkeypatch)
    entry_id = k.record("service_call", "recorder.purge")
    result = asyncio.run(k.undo(entry_id))
    assert result["undoable"] is False
    assert "no undo artifact" in result["reason"]


def test_undo_unknown_entry(tmp_path, monkeypatch):
    kernel_mod, k = _kernel(tmp_path, monkeypatch)
    result = asyncio.run(k.undo("deadbeef0000"))
    assert result["undoable"] is False
    assert "no journal entry" in result["reason"]


def test_journal_tail_limit(tmp_path, monkeypatch):
    kernel_mod, k = _kernel(tmp_path, monkeypatch)
    for i in range(10):
        k.record("bulk", f"target-{i}")
    tail = k.journal_tail(3)
    assert len(tail) == 3
    assert tail[-1]["target"] == "target-9"
