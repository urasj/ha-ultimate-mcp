"""Fingerprint collector — profiles the installation, derives capability strings.

Capabilities feed Registry.apply_gates(): "integration:zha", "addon:core_mosquitto",
"service:mqtt", "db:sqlite", "os:haos", "vm:kvm", ...
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ultimate_mcp.context import DATA_DIR, Context

log = logging.getLogger("umcp.fingerprint")

CACHE = DATA_DIR / "fingerprint.json"


async def collect_fingerprint(ctx: Context) -> dict[str, Any]:
    fp: dict[str, Any] = {"capabilities": []}
    caps: list[str] = fp["capabilities"]

    async def safe(section: str, coro):
        try:
            fp[section] = await coro
        except Exception as exc:  # noqa: BLE001 — degrade gracefully per section
            fp[section] = {"error": str(exc)}
            log.warning("fingerprint section %s failed: %s", section, exc)

    await safe("core", ctx.supervisor.get("/core/info"))
    await safe("supervisor", ctx.supervisor.get("/supervisor/info"))
    await safe("os", ctx.supervisor.get("/os/info"))
    await safe("host", ctx.supervisor.get("/host/info"))
    await safe("hardware", ctx.supervisor.get("/hardware/info"))
    await safe("addons", ctx.supervisor.get("/addons"))
    await safe("network", ctx.supervisor.get("/network/info"))

    # Derive capabilities
    addons = (fp.get("addons") or {}).get("data", {}).get("addons", [])
    for addon in addons:
        slug = addon.get("slug", "")
        if slug:
            caps.append(f"addon:{slug}")

    # Database engine: MariaDB service registered? else SQLite file
    try:
        mysql = await ctx.supervisor.get("/services/mysql")
        if mysql.get("data"):
            caps.append("db:mariadb")
    except Exception:  # noqa: BLE001
        pass
    if "db:mariadb" not in caps and ctx.db.db_path.exists():
        caps.append("db:sqlite")
        fp["database"] = {"engine": "sqlite", "size_bytes": ctx.db.db_path.stat().st_size}

    # MQTT service
    try:
        mqtt = await ctx.supervisor.get("/services/mqtt")
        if mqtt.get("data"):
            caps.append("service:mqtt")
    except Exception:  # noqa: BLE001
        pass

    # Custom components + integrations (W0: extend via WS config_entries/get)
    cc_dir = ctx.fs.root / "custom_components"
    if cc_dir.is_dir():
        fp["custom_components"] = sorted(p.name for p in cc_dir.iterdir() if p.is_dir())
        for name in fp["custom_components"]:
            caps.append(f"custom_component:{name}")

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(fp, indent=2, default=str), encoding="utf-8")
    return fp
