"""Context — the facades every tool implementation receives (W0 contract).

Tool impls NEVER make raw HTTP calls; they use these facades so recorded-fixture
tests can replace them uniformly.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from ultimate_mcp.ws import HaWsClient  # re-export for tool modules

__all__ = [
    "SUPERVISOR_URL",
    "HA_CONFIG_ROOT",
    "DATA_DIR",
    "SupervisorClient",
    "HaWsClient",
    "FsFacade",
    "DbFacade",
    "Context",
]

SUPERVISOR_URL = "http://supervisor"
HA_CONFIG_ROOT = Path(os.environ.get("UMCP_HA_CONFIG", "/homeassistant"))
DATA_DIR = Path(os.environ.get("UMCP_DATA", "/data"))


class SupervisorClient:
    """Thin async wrapper over the Supervisor REST API."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        self._client = client or httpx.AsyncClient(
            base_url=SUPERVISOR_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    async def get(self, path: str) -> Any:
        """GET a Supervisor endpoint. Most return JSON, but the log endpoints
        (/addons/<slug>/logs, /core/logs, …) return text/plain — decode by
        content-type instead of assuming JSON."""
        r = await self._client.get(path)
        r.raise_for_status()
        content_type = r.headers.get("content-type", "")
        if "json" in content_type:
            return r.json()
        if not content_type:
            try:
                return r.json()
            except ValueError:
                return r.text
        return r.text

    async def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        r = await self._client.post(path, json=body or {})
        r.raise_for_status()
        return r.json() if r.content else {}

    async def core_api(self, method: str, path: str, body: dict | None = None) -> Any:
        """Proxy to core REST: /core/api/<path>."""
        r = await self._client.request(method, f"/core/api/{path.lstrip('/')}", json=body)
        r.raise_for_status()
        return r.json() if r.content else None


class FsFacade:
    """Guarded filesystem access rooted at the HA config mount."""

    def __init__(self, root: Path = HA_CONFIG_ROOT) -> None:
        self.root = root

    def resolve(self, rel: str) -> Path:
        p = (self.root / rel.lstrip("/")).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise PermissionError(f"path escapes config root: {rel}")
        return p

    def read_text(self, rel: str, max_bytes: int = 2_000_000) -> str:
        p = self.resolve(rel)
        if p.stat().st_size > max_bytes:
            raise ValueError(f"{rel} exceeds {max_bytes} bytes; use fs_tree/fs_grep instead")
        return p.read_text(encoding="utf-8", errors="replace")

    def read_storage(self, key: str) -> dict[str, Any]:
        """Read a .storage JSON file by key, e.g. 'core.entity_registry'."""
        return json.loads(self.resolve(f".storage/{key}").read_text(encoding="utf-8"))


class DbFacade:
    """Read-only access to the recorder database (SQLite mode)."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or (HA_CONFIG_ROOT / "home-assistant_v2.db")

    def query(self, sql: str, params: tuple = (), limit: int = 500) -> list[dict[str, Any]]:
        uri = f"file:{self.db_path}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            con.execute("PRAGMA query_only = ON")
            con.row_factory = sqlite3.Row
            rows = con.execute(sql, params).fetchmany(limit)
            return [dict(r) for r in rows]
        finally:
            con.close()


class Context:
    """Bundle handed to every tool impl."""

    def __init__(self) -> None:
        self.supervisor = SupervisorClient()
        self.ha_ws = HaWsClient()
        self.fs = FsFacade()
        self.db = DbFacade()
        self.fingerprint: dict[str, Any] = {}
        self.options: dict[str, Any] = self._load_options()

    @staticmethod
    def _load_options() -> dict[str, Any]:
        opts = DATA_DIR / "options.json"
        if opts.exists():
            return json.loads(opts.read_text(encoding="utf-8"))
        return {"log_level": "info", "auth_token": "", "destructive_enabled": False}
