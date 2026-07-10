"""hacs/ surface tests (W1).

FakeFs stands in for ctx.fs.read_storage; asyncio.run drives each coroutine.
sys.path insertion mirrors tests/test_registry.py.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from ultimate_mcp.tools.hacs import impl  # noqa: E402
from ultimate_mcp.tools.hacs.manifest import SURFACE  # noqa: E402

# hacs.repositories layout: data maps id -> repo fields directly.
HACS_REPOSITORIES = {
    "version": 1,
    "key": "hacs.repositories",
    "data": {
        "111": {
            "full_name": "thomasloven/lovelace-card-mod",
            "category": "plugin",
            "installed": True,
            "installed_version": "3.4.0",
            "available_version": "3.4.2",
        },
        "222": {
            "full_name": "hacs/integration",
            "category": "integration",
            "installed": True,
            "installed_version": "1.34.0",
            "available_version": "1.34.0",
        },
        "333": {
            "full_name": "someone/uninstalled-theme",
            "category": "theme",
            "installed": False,
            "installed_version": None,
            "available_version": "0.1.0",
        },
    },
}

# hacs.data layout: data.repositories maps id -> {"data": {repo fields}}.
HACS_DATA = {
    "version": 1,
    "key": "hacs.data",
    "data": {
        "repositories": {
            "999": {
                "data": {
                    "full_name": "custom-cards/button-card",
                    "category": "plugin",
                    "installed": True,
                    "installed_version": "4.0.0",
                    "available_version": "4.1.0",
                }
            }
        }
    },
}


class FakeFs:
    def __init__(self, storage: dict) -> None:
        self.storage = storage

    def read_storage(self, key: str) -> dict:
        if key in self.storage:
            return self.storage[key]
        raise FileNotFoundError(f".storage/{key}")


class StubCtx:
    def __init__(self, storage: dict) -> None:
        self.fs = FakeFs(storage)


def test_hacs_inventory_parses_installed_only():
    ctx = StubCtx({"hacs.repositories": HACS_REPOSITORIES})
    out = asyncio.run(impl.hacs_inventory(ctx))
    assert out["storage_key"] == "hacs.repositories"
    assert out["installed_count"] == 2  # the uninstalled theme is excluded
    names = {r["name"] for r in out["repositories"]}
    assert names == {"thomasloven/lovelace-card-mod", "hacs/integration"}
    assert out["by_category"] == {"plugin": 1, "integration": 1}


def test_hacs_pending_updates_flags_version_mismatch():
    ctx = StubCtx({"hacs.repositories": HACS_REPOSITORIES})
    out = asyncio.run(impl.hacs_pending_updates(ctx))
    assert out["pending_count"] == 1
    pending = out["pending"][0]
    assert pending["name"] == "thomasloven/lovelace-card-mod"
    assert pending["installed_version"] == "3.4.0"
    assert pending["available_version"] == "3.4.2"


def test_hacs_data_fallback_layout():
    ctx = StubCtx({"hacs.data": HACS_DATA})
    inv = asyncio.run(impl.hacs_inventory(ctx))
    assert inv["storage_key"] == "hacs.data"
    assert inv["installed_count"] == 1
    assert inv["repositories"][0]["name"] == "custom-cards/button-card"
    pend = asyncio.run(impl.hacs_pending_updates(ctx))
    assert pend["pending_count"] == 1


def test_hacs_degrades_when_storage_missing():
    ctx = StubCtx({})  # neither key present
    out = asyncio.run(impl.hacs_inventory(ctx))
    assert "error" in out


def test_surface_gated_on_hacs_integration():
    assert SURFACE.requires == ("integration:hacs",)


def test_manifest_impl_name_parity():
    for t in SURFACE.tools:
        fn = getattr(impl, t.name, None)
        assert fn is not None, f"missing impl for {t.name}"
        assert asyncio.iscoroutinefunction(fn), f"{t.name} is not async"
