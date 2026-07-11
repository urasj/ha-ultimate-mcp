"""Import guard — every ultimate_mcp module must import cleanly.

Sync corruption has truncated core files in this repo twice before (commit
b14fbff, and the 0.2.1 release). A truncated module usually still parses up to
the cut, so `compileall` alone is not enough — actually importing each module
catches half-written functions that reference names defined below the cut.
"""

import importlib
import pkgutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))


def test_every_module_imports():
    import ultimate_mcp

    failures: list[tuple[str, str]] = []
    for info in pkgutil.walk_packages(ultimate_mcp.__path__, "ultimate_mcp."):
        try:
            importlib.import_module(info.name)
        except Exception as exc:  # noqa: BLE001 — report every broken module at once
            failures.append((info.name, repr(exc)))
    assert not failures, f"modules failed to import: {failures}"
