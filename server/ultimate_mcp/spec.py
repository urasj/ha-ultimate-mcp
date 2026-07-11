"""ToolSpec — the frozen contract every surface codes against (W0).

A surface package (ultimate_mcp/tools/<surface>/) ships:
  manifest.py  -> SURFACE: SurfaceSpec (pure data, imported at startup)
  impl.py      -> async def <tool_name>(ctx, **args) (lazy-imported on first call)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Tier(IntEnum):
    T0_READ = 0          # no side effects
    T1_REVERSIBLE = 1    # reversible write; requires explicit dry_run=false
    T2_RISKY = 2         # requires a live checkpoint (process-wide registry, TTL'd)
    T3_DESTRUCTIVE = 3   # requires destructive_enabled + confirm_token + checkpoint


@dataclass(frozen=True)
class ToolSpec:
    """One virtual tool. Schema is standard JSON Schema for the args object."""

    name: str
    summary: str
    tier: Tier
    schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    # Capability predicates evaluated against the fingerprint, e.g.
    # "integration:zha", "addon:core_mosquitto", "service:mqtt", "db:sqlite"
    requires: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()  # extra search terms


@dataclass(frozen=True)
class SurfaceSpec:
    name: str                    # e.g. "database"
    summary: str
    tools: tuple[ToolSpec, ...]
    impl_module: str             # e.g. "ultimate_mcp.tools.database.impl"
    requires: tuple[str, ...] = ()  # surface-level gates
