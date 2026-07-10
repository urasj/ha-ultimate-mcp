"""registries/ surface tests (W5) — StubWs records ctx.ha_ws.call(command, **kwargs).

Async tools are driven with asyncio.run() directly; sys.path bootstrap mirrors
tests/test_registry.py.
"""

import asyncio
import inspect
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Pin DATA_DIR/HA_CONFIG to a writable sandbox before any ultimate_mcp import.
# When the three W5 test files run together this module imports context first,
# so DATA_DIR (used by StorageEditor/filesystem undo + journal in the sibling
# suites) resolves to a temp dir rather than the un-writable default /data.
_SANDBOX = tempfile.mkdtemp(prefix="umcp-reg-test-")
os.environ.setdefault("UMCP_HA_CONFIG", str(Path(_SANDBOX) / "config"))
os.environ.setdefault("UMCP_DATA", str(Path(_SANDBOX) / "data"))

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from ultimate_mcp.tools.registries import impl  # noqa: E402
from ultimate_mcp.tools.registries.manifest import SURFACE  # noqa: E402
from ultimate_mcp.ws import HaWsError  # noqa: E402

ENTITIES = [
    {"entity_id": "light.kitchen", "name": "Kitchen", "platform": "hue",
     "device_id": "dev1", "area_id": "kitchen", "labels": ["fav"], "hidden_by": None},
    {"entity_id": "sensor.temp", "original_name": "Temp", "platform": "zha",
     "device_id": "dev2", "area_id": None, "labels": [], "disabled_by": "user"},
]
DEVICES = [
    {"id": "dev1", "name": "Hue Bulb", "manufacturer": "Signify", "area_id": "kitchen",
     "config_entries": ["ce1"], "labels": []},
]
AREAS = [{"area_id": "kitchen", "name": "Kitchen", "floor_id": "ground", "labels": []}]
FLOORS = [{"floor_id": "ground", "name": "Ground", "level": 0}]
LABELS = [{"label_id": "fav", "name": "Favourite", "color": "amber"}]
CATEGORIES = [{"category_id": "c1", "name": "Lighting"}]

RESPONSES = {
    "config/entity_registry/list": ENTITIES,
    "config/entity_registry/get": lambda kw: next(
        (e for e in ENTITIES if e["entity_id"] == kw.get("entity_id")), None
    ),
    "config/device_registry/list": DEVICES,
    "config/area_registry/list": AREAS,
    "config/floor_registry/list": FLOORS,
    "config/label_registry/list": LABELS,
    "config/category_registry/list": CATEGORIES,
    "config/entity_registry/update": lambda kw: {"entity_entry": {"entity_id": kw["entity_id"]}},
}


class StubWs:
    """Records .call(command, **kwargs); returns canned results per command."""

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


class StubCtx:
    def __init__(self, ws):
        self.ha_ws = ws


def ctx(**kw):
    return StubCtx(StubWs(**kw))


# --------------------------------------------------------------- contract
def test_manifest_impl_parity():
    for spec in SURFACE.tools:
        fn = getattr(impl, spec.name, None)
        assert fn is not None, f"missing impl for {spec.name}"
        assert inspect.iscoroutinefunction(fn), f"{spec.name} must be async"


def test_surface_has_no_gates():
    assert SURFACE.requires == ()


# --------------------------------------------------------------- T0 lists
def test_entity_list_parses():
    out = asyncio.run(impl.entity_list(ctx()))
    assert out["count"] == 2
    ids = [e["entity_id"] for e in out["entities"]]
    assert "light.kitchen" in ids
    # original_name is used when name is absent
    temp = next(e for e in out["entities"] if e["entity_id"] == "sensor.temp")
    assert temp["name"] == "Temp"
    assert temp["disabled_by"] == "user"


def test_entity_get_parses():
    out = asyncio.run(impl.entity_get(ctx(), "light.kitchen"))
    assert out["entity"]["platform"] == "hue"


def test_device_area_floor_label_category_lists():
    assert asyncio.run(impl.device_list(ctx()))["devices"][0]["name"] == "Hue Bulb"
    assert asyncio.run(impl.area_list(ctx()))["areas"][0]["area_id"] == "kitchen"
    assert asyncio.run(impl.floor_list(ctx()))["floors"][0]["floor_id"] == "ground"
    assert asyncio.run(impl.label_list(ctx()))["labels"][0]["label_id"] == "fav"
    cat = asyncio.run(impl.category_list(ctx(), scope="automation"))
    assert cat["scope"] == "automation"
    assert cat["categories"][0]["name"] == "Lighting"


def test_list_wrapped_shape_is_normalised():
    # Some HA versions wrap the list; _as_list must accept {"entities": [...]}.
    ws = StubWs(responses={"config/entity_registry/list": {"entities": ENTITIES}})
    out = asyncio.run(impl.entity_list(StubCtx(ws)))
    assert out["count"] == 2


def test_ws_failure_degrades():
    out = asyncio.run(impl.entity_list(ctx(raise_for=["config/entity_registry/list"])))
    assert "error" in out
    assert out["note"] == "verify WS command for 2026.7"
    assert out["command"] == "config/entity_registry/list"


# --------------------------------------------------------------- T1 writes
def test_entity_update_dry_run_makes_no_call():
    c = ctx()
    out = asyncio.run(impl.entity_update(c, "light.kitchen", {"name": "Cocina", "area_id": "kit"}))
    assert out["dry_run"] is True
    assert out["command"] == "config/entity_registry/update"
    assert out["payload"] == {"entity_id": "light.kitchen", "name": "Cocina", "area_id": "kit"}
    assert c.ha_ws.calls == []  # dry run touched nothing


def test_entity_update_execute_calls_ws():
    c = ctx()
    out = asyncio.run(
        impl.entity_update(c, "light.kitchen", {"name": "Cocina"}, dry_run=False)
    )
    assert out["updated"] is True
    cmd, kwargs = c.ha_ws.calls[0]
    assert cmd == "config/entity_registry/update"
    assert kwargs == {"entity_id": "light.kitchen", "name": "Cocina"}


def test_entity_update_execute_degrades_on_ws_error():
    out = asyncio.run(
        impl.entity_update(
            ctx(raise_for=["config/entity_registry/update"]),
            "light.kitchen",
            {"name": "x"},
            dry_run=False,
        )
    )
    assert "error" in out


def test_area_and_label_create_dry_run():
    a = asyncio.run(impl.area_create(ctx(), "Garage", floor_id="ground"))
    assert a["dry_run"] is True
    assert a["payload"]["name"] == "Garage" and a["payload"]["floor_id"] == "ground"
    lbl = asyncio.run(impl.label_create(ctx(), "Critical", color="red"))
    assert lbl["payload"]["name"] == "Critical" and lbl["payload"]["color"] == "red"


def test_entity_expose_assist_dry_run_defaults_conversation():
    out = asyncio.run(impl.entity_expose_assist(ctx(), ["light.kitchen"], True))
    assert out["dry_run"] is True
    assert out["payload"]["assistants"] == ["conversation"]
    assert out["payload"]["should_expose"] is True


# --------------------------------------------------------------- T2 removes
def test_entity_remove_dry_run_and_execute():
    dry = asyncio.run(impl.entity_remove(ctx(), "light.gone"))
    assert dry["dry_run"] is True and dry["command"] == "config/entity_registry/remove"
    c = ctx(responses={**RESPONSES, "config/entity_registry/remove": None})
    ex = asyncio.run(impl.entity_remove(c, "light.gone", dry_run=False))
    assert ex["removed"] is True
    assert c.ha_ws.calls[0][0] == "config/entity_registry/remove"


def test_device_remove_dry_run_uses_config_entry_command():
    out = asyncio.run(impl.device_remove(ctx(), "dev1", config_entry_id="ce1"))
    assert out["dry_run"] is True
    assert out["command"] == "config/device_registry/remove_config_entry"
    assert out["payload"] == {"device_id": "dev1", "config_entry_id": "ce1"}
