"""dashboards/ surface tests (W5).

StubWs records ctx.ha_ws.call for the lovelace/* commands. The save tool is
WS-based as of 0.2.6 (lovelace/config/save — the frontend's own save path), so
the storage-mode tests assert the WS call, the journal entry, and the pre-image
undo artifact instead of the old StorageEditor file-surgery spine. YAML-mode
still goes through a real FsFacade. Env is pinned before importing ultimate_mcp
(DATA_DIR is used for undo copies / journal).
"""

import asyncio
import inspect
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
from ultimate_mcp.safety.storage_editor import UNDO_ROOT  # noqa: E402
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

    async def post(self, path, body=None, **_kw):
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
def test_dashboard_config_save_dry_run_diffs_live_config():
    ws = StubWs(responses={**RESPONSES, "lovelace/config/save": {"success": True}})
    ctx = StubCtx(ws=ws)
    new_config = {"views": [{"title": "Fresh", "cards": [{"type": "markdown", "content": "hi"}]}]}
    out = asyncio.run(impl.dashboard_config_save(ctx, new_config, url_path=None))
    assert out["dry_run"] is True
    assert out["mode"] == "storage"
    assert out["summary"] == {"view_count": 1, "card_count": 1}
    assert out["current"] == {"view_count": 1, "card_count": 3}
    assert "would_change" in out
    # dry run is read-only: no save command was issued, no supervisor calls
    assert all(cmd != "lovelace/config/save" for cmd, _ in ws.calls)
    assert ctx.supervisor.calls == []


def test_dashboard_config_save_execute_via_ws():
    saves: list[dict] = []

    def record_save(kwargs):
        saves.append(kwargs)
        return {"success": True}

    ws = StubWs(responses={**RESPONSES, "lovelace/config/save": record_save})
    ctx = StubCtx(ws=ws)
    new_config = {"views": [{"title": "Fresh", "cards": []}]}
    out = asyncio.run(impl.dashboard_config_save(ctx, new_config, url_path="admin", dry_run=False))
    assert out["applied"] is True
    assert saves == [{"config": new_config, "url_path": "admin"}]
    # the WS path replaces the old file-surgery spine: no backup, no core stop/start
    assert ctx.supervisor.calls == []
    # journaled, with a pre-image undo artifact of the previous config
    assert out.get("journal_id")
    assert out.get("undo_id")
    undo_file = UNDO_ROOT / out["undo_id"] / "dashboard_config.json"
    assert undo_file.is_file()
    assert "Home" in undo_file.read_text(encoding="utf-8")


def test_dashboard_config_save_ws_error_degrades():
    ws = StubWs()  # no lovelace/config/save in responses -> raises
    ctx = StubCtx(ws=ws)
    out = asyncio.run(
        impl.dashboard_config_save(ctx, {"views": []}, url_path="admin", dry_run=False)
    )
    assert "error" in out and out["command"] == "lovelace/config/save"
    assert ctx.supervisor.calls == []


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
