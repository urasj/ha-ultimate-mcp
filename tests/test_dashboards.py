"""dashboards/ surface tests (W5).

StubWs records ctx.ha_ws.call for the lovelace/* read commands; the save tool is
exercised through a real FsFacade + StubSupervisor so the StorageEditor spine
runs end to end. Env is pinned before importing ultimate_mcp (DATA_DIR is used
for undo copies / journal).
"""

import asyncio
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

_SANDBOX = tempfile.mkdtemp(prefix="umcp-dash-test-")
os.environ["UMCP_HA_CONFIG"] = str(Path(_SANDBOX) / "config")
os.environ["UMCP_DATA"] = str(Path(_SANDBOX) / "data")

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from ultimate_mcp.context import FsFacade  # noqa: E402
from ultimate_mcp.tools.dashboards import impl  # noqa: E402
from ultimate_mcp.tools.dashboards.manifest import SURFACE  # noqa: E402
from ultimate_mcp.ws import HaWsError  # noqa: E402

DASHBOARDS = [
    {"url_path": None, "title": "Overview", "mode": "storage", "id": "default"},
    {"url_path": "admin", "title": "Admin", "mode": "yaml", "require_admin": True},
]
DASH_CONFIG = {
    "views": [
        {
            "title": "Home",
            "cards": [
                {"type": "light", "entity": "light.kitchen"},
                {"type": "entities", "entities": ["switch.fan"]},
                {"missing": "type"},  # a card with no type -> lint warning
            ],
        }
    ]
}
RESOURCES = [{"url": "/local/foo.js", "type": "module"}]

RESPONSES = {
    "lovelace/dashboards/list": DASHBOARDS,
    "lovelace/config": DASH_CONFIG,
    "lovelace/resources": RESOURCES,
}

LOVELACE_STORE = {
    "version": 1,
    "minor_version": 1,
    "key": "lovelace",
    "data": {"config": {"views": [{"title": "Old", "cards": []}]}},
}


class StubWs:
    def __init__(self, responses=None, raise_for=None):
        self.calls: list[tuple] = []
        self.responses = responses if responses is not None else RESPONSES
        self.raise_for = set(raise_for or ())

    async def call(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command in self.raise_for or command not in self.responses:
            raise HaWsError("unknown_command", f"unknown WS command {command}")
        canned = self.responses[command]
        return canned(kwargs) if callable(canned) else canned


class StubSupervisor:
    def __init__(self):
        self.calls: list[tuple] = []

    async def get(self, path):
        self.calls.append(("GET", path))
        if path == "/core/info":
            return {"data": {"state": "running"}}
        return {"data": {}}

    async def post(self, path, body=None):
        self.calls.append(("POST", path))
        if path == "/backups/new/partial":
            return {"data": {"slug": "slug1"}}
        return {"data": {}}

    def posts(self):
        return [p for verb, p in self.calls if verb == "POST"]


class StubCtx:
    def __init__(self, ws=None, root: Path | None = None):
        self.ha_ws = ws
        self.fs = FsFacade(root=root) if root else None
        self.supervisor = StubSupervisor()


def _build_store(root: Path) -> None:
    storage = root / ".storage"
    storage.mkdir(parents=True)
    (storage / "lovelace").write_text(json.dumps(LOVELACE_STORE, indent=2), encoding="utf-8")


# --------------------------------------------------------------- contract
def test_manifest_impl_parity():
    for spec in SURFACE.tools:
        fn = getattr(impl, spec.name, None)
        assert fn is not None, f"missing impl for {spec.name}"
        assert inspect.iscoroutinefunction(fn), f"{spec.name} must be async"


# --------------------------------------------------------------- T0 reads
def test_dashboard_list_parses():
    out = asyncio.run(impl.dashboard_list(StubCtx(ws=StubWs())))
    assert out["count"] == 2
    assert out["dashboards"][0]["title"] == "Overview"


def test_dashboard_get_config_parses():
    out = asyncio.run(impl.dashboard_get_config(StubCtx(ws=StubWs()), url_path="admin"))
    assert out["view_count"] == 1
    assert out["config"]["views"][0]["title"] == "Home"


def test_dashboard_resources_parses():
    out = asyncio.run(impl.dashboard_resources(StubCtx(ws=StubWs())))
    assert out["count"] == 1
    assert out["resources"][0]["url"] == "/local/foo.js"


def test_read_degrades_on_ws_error():
    out = asyncio.run(impl.dashboard_list(StubCtx(ws=StubWs(raise_for=["lovelace/dashboards/list"]))))
    assert "error" in out and out["command"] == "lovelace/dashboards/list"


# --------------------------------------------------------------- lint
def test_dashboard_card_lint_flags_missing_type():
    out = asyncio.run(impl.dashboard_card_lint(StubCtx(ws=StubWs())))
    assert out["warning_count"] >= 1
    issues = [w["issue"] for w in out["warnings"]]
    assert any("missing 'type'" in i for i in issues)


# --------------------------------------------------------------- T2 save
def test_dashboard_config_save_dry_run(tmp_path):
    root = tmp_path / "config"
    root.mkdir()
    _build_store(root)
    ctx = StubCtx(root=root)
    new_config = {"views": [{"title": "Fresh", "cards": [{"type": "markdown", "content": "hi"}]}]}
    out = asyncio.run(impl.dashboard_config_save(ctx, new_config, url_path=None))
    assert out["dry_run"] is True
    assert out["mode"] == "storage"
    assert out["storage_key"] == "lovelace"
    assert out["summary"] == {"view_count": 1, "card_count": 1}
    assert out["checkpoint_required"] == "partial:homeassistant"
    # dry run touched nothing
    assert ctx.supervisor.calls == []
    assert ctx.fs.read_storage("lovelace")["data"]["config"]["views"][0]["title"] == "Old"


def test_dashboard_config_save_execute_via_editor(tmp_path):
    root = tmp_path / "config"
    root.mkdir()
    _build_store(root)
    ctx = StubCtx(root=root)
    new_config = {"views": [{"title": "Fresh", "cards": []}]}
    out = asyncio.run(impl.dashboard_config_save(ctx, new_config, url_path=None, dry_run=False))
    assert ctx.supervisor.posts() == ["/backups/new/partial", "/core/stop", "/core/start"]
    assert ctx.fs.read_storage("lovelace")["data"]["config"]["views"][0]["title"] == "Fresh"


def test_dashboard_config_save_yaml_mode(tmp_path):
    root = tmp_path / "config"
    root.mkdir()
    ctx = StubCtx(root=root)
    cfg = {"views": [{"title": "Y", "cards": []}]}
    dry = asyncio.run(
        impl.dashboard_config_save(ctx, cfg, mode="yaml", yaml_path="ui-admin.yaml")
    )
    assert dry["dry_run"] is True and dry["mode"] == "yaml"
    ex = asyncio.run(
        impl.dashboard_config_save(ctx, cfg, mode="yaml", yaml_path="ui-admin.yaml", dry_run=False)
    )
    assert ex["written"] is True
    assert "title: Y" in ctx.fs.read_text("ui-admin.yaml")
