"""Shared test bootstrap (W0).

context.py resolves DATA_DIR / HA_CONFIG_ROOT from env AT IMPORT TIME, and
pytest imports every test module at collection before any fixture runs. If the
first-collected module imports ultimate_mcp without pinning these vars, the
container defaults (/data, /homeassistant) get baked into the cached modules
and later suites hit PermissionError. Pin writable sandbox defaults here —
conftest.py is imported before all test modules. setdefault keeps any values a
test module (or CI) sets explicitly.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

_SANDBOX = Path(tempfile.mkdtemp(prefix="umcp-conftest-"))
os.environ.setdefault("UMCP_DATA", str(_SANDBOX / "data"))
os.environ.setdefault("UMCP_HA_CONFIG", str(_SANDBOX / "config"))
(Path(os.environ["UMCP_HA_CONFIG"])).mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))


@pytest.fixture(autouse=True, scope="session")
def _sync_env_to_cached_context():
    """Collection imports every test module before tests run, so a module that
    hard-sets UMCP_* env AFTER another module already imported ultimate_mcp
    ends up with env pointing one place and the cached module constants
    another. Re-align env with whatever the cached context resolved."""
    ctx_mod = sys.modules.get("ultimate_mcp.context")
    if ctx_mod is not None:
        os.environ["UMCP_DATA"] = str(ctx_mod.DATA_DIR)
        os.environ["UMCP_HA_CONFIG"] = str(ctx_mod.HA_CONFIG_ROOT)
    yield
