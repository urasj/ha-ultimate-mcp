"""storage/ surface + StorageEditor protocol tests (W2)."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# Env must be pinned BEFORE any ultimate_mcp import: context.py resolves
# HA_CONFIG_ROOT / DATA_DIR at import time.
_SANDBOX = tempfile.mkdtemp(prefix="umcp-test-")
os.environ["UMCP_HA_CONFIG"] = str(Path(_SANDBOX) / "config")
os.environ["UMCP_DATA"] = str(Path(_SANDBOX) / "data")

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import pytest  # noqa: E402

from ultimate_mcp.context import FsFacade  # noqa: E402
from ultimate_mcp.safety.storage_editor import JOURNAL, StorageEditor, diff_summary  # noqa: E402
from ultimate_mcp.tools.storage import impl  # noqa: E402

# ------------------------------------------------------------------ fixtures

ENTITY_REGISTRY = {
    "version": 1,
    "minor_version": 17,
    "key": "core.entity_registry",
    "data": {
        "entities": [
            {
                "entity_id": "light.kitchen",
                "device_id": "dev-hue-1",
                "platform": "hue",
                "unique_id": "hue-bulb-1",
            },
            {
                "entity_id": "sensor.hallway_temp",
                "device_id": "dev-gone",  # orphan: device no longer exists
                "platform": "zha",
                "unique_id": "zha-temp-9",
            },
            {
                "entity_id": "switch.fan",
                "device_id": None,
                "platform": "mqtt",
                "unique_id": "mqtt-fan-3",
            },
        ],
        "deleted_entities": [],
    },
}

DEVICE_REGISTRY = {
    "version": 1,
    "minor_version": 11,
    "key": "core.device_registry",
    "data": {
        "devices": [
            {"id": "dev-hue-1", "name": "Hue Bulb", "config_entries": ["ce-hue"]},
            {"id": "dev-ghost", "name": "Ghost Device", "config_entries": ["ce-removed"]},
        ],
        "deleted_devices": [],
    },
}

CONFIG_ENTRIES = {
    "version": 1,
    "minor_version": 5,
    "key": "core.config_entries",
    "data": {"entries": [{"entry_id": "ce-hue", "domain": "hue", "title": "Philips Hue"}]},
}

LOVELACE = {
    "version": 1,
    "minor_version": 1,
    "key": "lovelace",
    "data": {
        "config": {
            "views": [
                {
                    "title": "Home",
                    "cards": [
                        {"type": "light", "entity": "light.kitchen"},
                        {"type": "entities", "entities": ["switch.fan"]},
                    ],
                }
            ]
        }
    },
}

CLOUD_STORE = {
    "version": 1,
    "key": "cloud",
    "data": {
        "access_token": "very-secret-jwt",
        "nested": {"password": "hunter2", "note": "keep me"},
        "refresh_token": "also-secret",
    },
}

AUTOMATIONS_YAML = """\
- id: 'kitchen_night'
  alias: Kitchen light at night
  trigger:
    - platform: state
      entity_id: light.kitchen
      to: 'on'
  condition: []
  action:
    - service: light.turn_off
      target:
        entity_id: light.kitchen
"""


def build_config(root: Path) -> None:
    storage = root / ".storage"
    storage.mkdir(parents=True)
    for name, doc in (
        ("core.entity_registry", ENTITY_REGISTRY),
        ("core.device_registry", DEVICE_REGISTRY),
        ("core.config_entries", CONFIG_ENTRIES),
        ("lovelace", LOVELACE),
        ("cloud", CLOUD_STORE),
    ):
        (storage / name).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    (root / "automations.yaml").write_text(AUTOMATIONS_YAML, encoding="utf-8")


class StubSupervisor:
    """Records every REST interaction; answers /core/info with RUNNING."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get(self, path: str, **_kw) -> dict:
        self.calls.append(("GET", path))
        if path == "/core/info":
            return {"result": "ok", "data": {"state": "running"}}
        return {"result": "ok", "data": {}}

    async def post(self, path: str, body: dict | None = None, **_kw) -> dict:
        self.calls.append(("POST", path))
        if path == "/backups/new/partial":
            return {"result": "ok", "data": {"slug": "aa11bb22"}}
        return {"result": "ok", "data": {}}

    def posts(self) -> list[str]:
        return [p for verb, p in self.calls if verb == "POST"]


class StubCtx:
    def __init__(self, root: Path) -> None:
        self.fs = FsFacade(root=root)
        self.supervisor = StubSupervisor()
        self.options = {"destructive_enabled": False}


@pytest.fixture()
def ctx(tmp_path: Path) -> StubCtx:
    root = tmp_path / "config"
    build_config(root)
    return StubCtx(root)


# ------------------------------------------------------------------ T0 reads

def test_storage_read_masks_secrets(ctx):
    out = asyncio.run(impl.storage_read(ctx, "cloud"))
    doc = out["document"]
    assert doc["data"]["access_token"] == "***MASKED***"
    assert doc["data"]["refresh_token"] == "***MASKED***"
    assert doc["data"]["nested"]["password"] == "***MASKED***"
    assert doc["data"]["nested"]["note"] == "keep me"  # non-secrets untouched
    # on-disk file untouched
    assert ctx.fs.read_storage("cloud")["data"]["access_token"] == "very-secret-jwt"


def test_storage_list_inventory(ctx):
    items = asyncio.run(impl.storage_list(ctx))
    by_file = {i["file"]: i for i in items}
    assert by_file["core.entity_registry"]["key"] == "core.entity_registry"
    assert by_file["core.entity_registry"]["version"] == 1
    assert by_file["lovelace"]["bytes"] > 0


def test_orphan_scan_finds_orphans(ctx):
    scan = asyncio.run(impl.storage_orphan_scan(ctx))
    assert [o["entity_id"] for o in scan["orphan_entities"]] == ["sensor.hallway_temp"]
    assert [o["id"] for o in scan["orphan_devices"]] == ["dev-ghost"]
    assert scan["counts"]["entities_scanned"] == 3


def test_dependency_graph_finds_yaml_and_dashboard_refs(ctx):
    graph = asyncio.run(impl.dependency_graph(ctx, "light.kitchen"))
    files = {r["file"]: r for r in graph["references"]}
    assert "automations.yaml" in files
    assert files["automations.yaml"]["count"] == 2
    assert "light.kitchen" in files["automations.yaml"]["sample_line"]
    assert ".storage/lovelace" in files
    assert files[".storage/lovelace"]["where"] == "dashboard"
    assert ".storage/core.entity_registry" in files
    # boundary safety: light.kitchen must not match a longer id
    assert asyncio.run(impl.dependency_graph(ctx, "light.kitchen_2"))["references"] == []


# --------------------------------------------------------- StorageEditor

def test_editor_dry_run_returns_diff_and_no_side_effects(ctx):
    def mutate(data):
        data["data"]["entries"].append({"entry_id": "ce-new", "domain": "x", "title": "X"})
        return data

    ed = StorageEditor(ctx)
    out = asyncio.run(ed.edit("core.config_entries", mutate, dry_run=True))
    assert out["checkpoint_required"] == "partial:homeassistant"
    assert "/data/entries/1" in out["would_change"][".storage/core.config_entries"]["added"]
    assert ctx.supervisor.calls == []  # dry run touched nothing
    assert len(ctx.fs.read_storage("core.config_entries")["data"]["entries"]) == 1


def test_editor_execute_call_order_and_journal(ctx):
    def mutate(data):
        data["data"]["entries"][0]["title"] = "Hue Bridge"
        return data

    ed = StorageEditor(ctx)
    out = asyncio.run(ed.edit("core.config_entries", mutate, dry_run=False))
    # protocol order: backup -> stop -> start; no boot poll in the request path
    assert ctx.supervisor.posts() == ["/backups/new/partial", "/core/stop", "/core/start"]
    assert ("GET", "/core/info") not in ctx.supervisor.calls
    assert out["applied"] is True
    assert out["backup_slug"] == "aa11bb22"
    assert out["core_restart"] == "started"
    assert ctx.fs.read_storage("core.config_entries")["data"]["entries"][0]["title"] == "Hue Bridge"
    # undo copy exists and holds the ORIGINAL content
    undo = Path(os.environ["UMCP_DATA"]) / "undo" / out["undo_id"] / ".storage__core.config_entries"
    assert json.loads(undo.read_text())["data"]["entries"][0]["title"] == "Philips Hue"
    # write-ahead journal: pending base entry + a committed update record
    lines = [json.loads(ln) for ln in JOURNAL.read_text().strip().splitlines()]
    entry = next(e for e in lines if e.get("action") == "storage_edit" and e["id"] == out["journal_id"])
    assert entry["status"] == "pending"  # as originally appended, pre-write
    assert entry["undo_id"] == out["undo_id"]
    assert any(
        e.get("action") == "journal_update"
        and e.get("ref") == out["journal_id"]
        and e.get("status") == "committed"
        for e in lines
    )


def test_editor_sanity_guard_blocks_envelope_damage(ctx):
    def bad(data):
        data["key"] = "something.else"
        return data

    ed = StorageEditor(ctx)
    with pytest.raises(ValueError):
        asyncio.run(ed.edit("core.config_entries", bad, dry_run=True))


def test_editor_rolls_back_and_restarts_on_failure(ctx):
    """Failure mid-write (after stop) must restore the file and start core."""
    boom = RuntimeError("disk full")

    def mutate(data):
        data["data"]["entries"][0]["title"] = "changed"
        return data

    ed = StorageEditor(ctx)
    original_write = ed._atomic_write

    calls = {"n": 0}

    def failing_write(target, content):
        calls["n"] += 1
        original_write(target, content)
        raise boom

    ed._atomic_write = failing_write
    with pytest.raises(RuntimeError, match="rolled back"):
        asyncio.run(ed.edit("core.config_entries", mutate, dry_run=False))
    # file restored to original
    assert ctx.fs.read_storage("core.config_entries")["data"]["entries"][0]["title"] == "Philips Hue"
    # core was started again after the failure
    assert ctx.supervisor.posts() == ["/backups/new/partial", "/core/stop", "/core/start"]


def test_diff_summary_paths():
    d = diff_summary({"a": 1, "b": [1, 2]}, {"a": 2, "b": [1], "c": True})
    assert d["changed"] == ["/a"]
    assert d["removed"] == ["/b/1"]
    assert d["added"] == ["/c"]


# --------------------------------------------------------- T2 tools

def test_entity_rename_deep_dry_run_lists_all_sites(ctx):
    out = asyncio.run(impl.entity_rename_deep(ctx, "light.kitchen", "light.cocina"))
    files = {s["file"] for s in out["plan"]}
    assert files == {".storage/core.entity_registry", ".storage/lovelace", "automations.yaml"}
    assert out["checkpoint_required"] == "partial:homeassistant"
    assert ctx.supervisor.calls == []
    # nothing changed on disk
    assert "light.kitchen" in ctx.fs.read_text("automations.yaml")


def test_entity_rename_deep_execute(ctx):
    out = asyncio.run(
        impl.entity_rename_deep(ctx, "light.kitchen", "light.cocina", dry_run=False)
    )
    assert ctx.supervisor.posts() == ["/backups/new/partial", "/core/stop", "/core/start"]
    reg = ctx.fs.read_storage("core.entity_registry")
    ids = [e["entity_id"] for e in reg["data"]["entities"]]
    assert "light.cocina" in ids and "light.kitchen" not in ids
    lovelace = json.dumps(ctx.fs.read_storage("lovelace"))
    assert "light.cocina" in lovelace and "light.kitchen" not in lovelace
    yaml_text = ctx.fs.read_text("automations.yaml")
    assert yaml_text.count("light.cocina") == 2 and "light.kitchen" not in yaml_text
    assert "light.turn_off" in yaml_text  # service call untouched
    assert out["undo_id"]


def test_entity_rename_rejects_unknown_or_colliding(ctx):
    with pytest.raises(ValueError, match="not found"):
        asyncio.run(impl.entity_rename_deep(ctx, "light.nope", "light.other"))
    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(impl.entity_rename_deep(ctx, "light.kitchen", "switch.fan"))


def test_orphan_clean_execute(ctx):
    out = asyncio.run(impl.storage_orphan_clean(ctx, dry_run=False))
    assert [o["entity_id"] for o in out["orphans"]["orphan_entities"]] == ["sensor.hallway_temp"]
    reg = ctx.fs.read_storage("core.entity_registry")
    assert [e["entity_id"] for e in reg["data"]["entities"]] == ["light.kitchen", "switch.fan"]
    devs = ctx.fs.read_storage("core.device_registry")
    assert [d["id"] for d in devs["data"]["devices"]] == ["dev-hue-1"]
    assert ctx.supervisor.posts() == ["/backups/new/partial", "/core/stop", "/core/start"]


def test_storage_patch_add_replace_remove(ctx):
    patch = [
        {"op": "add", "path": "/data/entries/-", "value": {"entry_id": "ce-2", "domain": "mqtt"}},
        {"op": "replace", "path": "/data/entries/0/title", "value": "Renamed Hue"},
        {"op": "remove", "path": "/data/entries/0/domain"},
    ]
    preview = asyncio.run(impl.storage_patch(ctx, "core.config_entries", patch))
    diff = preview["would_change"][".storage/core.config_entries"]
    assert "/data/entries/1" in diff["added"]
    assert "/data/entries/0/title" in diff["changed"]
    assert "/data/entries/0/domain" in diff["removed"]

    asyncio.run(impl.storage_patch(ctx, "core.config_entries", patch, dry_run=False))
    doc = ctx.fs.read_storage("core.config_entries")
    assert doc["data"]["entries"][1]["entry_id"] == "ce-2"
    assert doc["data"]["entries"][0]["title"] == "Renamed Hue"
    assert "domain" not in doc["data"]["entries"][0]


def test_storage_patch_rejects_unsupported_op(ctx):
    with pytest.raises(ValueError, match="unsupported op"):
        asyncio.run(impl.storage_patch(ctx, "core.config_entries", [{"op": "move", "path": "/x"}]))


# --------------------------------------------------------- manifest contract

def test_manifest_tiers_and_gates():
    from ultimate_mcp.spec import Tier
    from ultimate_mcp.tools.storage.manifest import SURFACE

    assert SURFACE.requires == ()
    tiers = {t.name: t.tier for t in SURFACE.tools}
    assert tiers["storage_read"] == Tier.T0_READ
    assert tiers["storage_list"] == Tier.T0_READ
    assert tiers["storage_orphan_scan"] == Tier.T0_READ
    assert tiers["dependency_graph"] == Tier.T0_READ
    assert tiers["entity_rename_deep"] == Tier.T2_RISKY
    assert tiers["storage_orphan_clean"] == Tier.T2_RISKY
    assert tiers["storage_patch"] == Tier.T2_RISKY
    # every tool name resolves to a coroutine in impl (registry dispatch contract)
    for t in SURFACE.tools:
        assert asyncio.iscoroutinefunction(getattr(impl, t.name))
