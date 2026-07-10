"""SafetyKernel journal record/tail/undo tests (W0).

UMCP_DATA must point at a temp dir BEFORE ultimate_mcp.context is imported
(DATA_DIR / JOURNAL / UNDO_DIR are resolved at import time), so each test
purges cached modules and re-imports inside the fixture.
"""
import asyncio
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))


def _purge_ultimate_mcp_modules() -> None:
    for name in [m for m in list(sys.modules) if m.startswith("ultimate_mcp")]:
        del sys.modules[name]


@pytest.fixture
def kernel(tmp_path, monkeypatch):
    monkeypatch.setenv("UMCP_DATA", str(tmp_path / "data"))
    _purge_ultimate_mcp_modules()
    kernel_mod = importlib.import_module("ultimate_mcp.safety.kernel")
    yield kernel_mod.SafetyKernel(ctx=object())  # ctx only used by checkpoint/authorize
    # purge again so later test modules re-import with THEIR env, not our tmp dir
    _purge_ultimate_mcp_modules()


def test_record_and_tail_roundtrip(kernel, tmp_path):
    entry_id = kernel.record("storage_edit", "/homeassistant/x.yaml", surface="filesystem")
    tail = kernel.journal_tail(limit=5)
    assert len(tail) == 1
    entry = tail[0]
    assert entry["id"] == entry_id
    assert entry["action"] == "storage_edit"
    assert entry["target"] == "/homeassistant/x.yaml"
    assert entry["surface"] == "filesystem"
    assert "ts" in entry


def test_record_copies_undo_artifact(kernel, tmp_path):
    target = tmp_path / "cfg" / "automations.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("original: true\n", encoding="utf-8")

    entry_id = kernel.record("edit", str(target), undo_artifact_path=target)

    artifact = tmp_path / "data" / "undo" / entry_id / "automations.yaml"
    assert artifact.exists()
    assert artifact.read_text(encoding="utf-8") == "original: true\n"
    assert kernel.journal_tail(1)[0]["undo_artifact"] == str(artifact)


def test_undo_restores_pre_change_file(kernel, tmp_path):
    target = tmp_path / "cfg" / "configuration.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("version: 1\n", encoding="utf-8")

    entry_id = kernel.record("edit", str(target), undo_artifact_path=target)
    target.write_text("version: 2  # botched edit\n", encoding="utf-8")

    result = asyncio.run(kernel.undo(entry_id))

    assert result["undoable"] is True
    assert result["restored"] == str(target)
    assert target.read_text(encoding="utf-8") == "version: 1\n"
    # the undo itself is journaled
    last = kernel.journal_tail(1)[0]
    assert last["action"] == "undo"
    assert last["undid"] == entry_id
    assert last["id"] == result["journal_id"]


def test_undo_without_artifact_reports_not_undoable(kernel):
    entry_id = kernel.record("service_call", "light.kitchen", service="light.turn_on")
    result = asyncio.run(kernel.undo(entry_id))
    assert result["undoable"] is False
    assert "no undo artifact" in result["reason"]


def test_undo_unknown_entry_reports_not_undoable(kernel):
    result = asyncio.run(kernel.undo("does-not-exist"))
    assert result["undoable"] is False
    assert "no journal entry" in result["reason"]


def test_jo