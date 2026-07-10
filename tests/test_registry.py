"""Registry contract smoke tests (W0)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from ultimate_mcp.registry import Registry  # noqa: E402


def _loaded() -> Registry:
    r = Registry()
    r.load_manifests()
    return r


def test_manifests_load():
    r = _loaded()
    assert "db_query" in r.tools


def test_capability_gating_enables():
    r = _loaded()
    r.apply_gates({"capabilities": ["db:sqlite"]})
    assert r.describe("db_query")["available"] is True


def test_capability_gating_disables_with_reason():
    r = _loaded()
    r.apply_gates({"capabilities": []})
    d = r.describe("db_query")
    assert d["available"] is False
    assert "db:sqlite" in d["unavailable_reason"]


def test_search_finds_by_keyword():
    r = _loaded()
    r.apply_gates({"capabilities": ["db:sqlite"]})
    names = [t["name"] for t in r.search("database bloat size")]
    assert "db_size_report" in names
