"""Registry — manifest loading, capability gating, search, lazy dispatch (W0)."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any

from ultimate_mcp.spec import SurfaceSpec, Tier, ToolSpec

# Surfaces registered here; each module exposes SURFACE: SurfaceSpec.
SURFACE_MODULES = [
    "ultimate_mcp.tools.database.manifest",
    "ultimate_mcp.tools.storage.manifest",
    "ultimate_mcp.tools.supervisor.manifest",
    "ultimate_mcp.tools.hacs.manifest",
    "ultimate_mcp.tools.filesystem.manifest",
    "ultimate_mcp.tools.dashboards.manifest",
    "ultimate_mcp.tools.diagnostics.manifest",
    "ultimate_mcp.tools.stats_repair.manifest",
    "ultimate_mcp.tools.network.manifest",
    "ultimate_mcp.tools.zigbee.manifest",
    "ultimate_mcp.tools.realtime.manifest",
    "ultimate_mcp.tools.registries.manifest",
    "ultimate_mcp.tools.assist.manifest",
    "ultimate_mcp.tools.media_camera.manifest",
]


@dataclass
class RegisteredTool:
    spec: ToolSpec
    surface: SurfaceSpec
    available: bool = True
    unavailable_reason: str | None = None


@dataclass
class Registry:
    tools: dict[str, RegisteredTool] = field(default_factory=dict)
    _impl_cache: dict[str, Any] = field(default_factory=dict)

    def load_manifests(self) -> None:
        for mod_name in SURFACE_MODULES:
            surface: SurfaceSpec = importlib.import_module(mod_name).SURFACE
            for spec in surface.tools:
                self.tools[spec.name] = RegisteredTool(spec=spec, surface=surface)

    def apply_gates(self, fingerprint: dict[str, Any]) -> None:
        caps: set[str] = set(fingerprint.get("capabilities", []))
        for rt in self.tools.values():
            missing = [
                req
                for req in (*rt.surface.requires, *rt.spec.requires)
                if req not in caps
            ]
            if missing:
                rt.available = False
                rt.unavailable_reason = f"missing: {', '.join(missing)}"

    def search(
        self, query: str, surface: str | None = None, max_results: int = 10
    ) -> list[dict[str, Any]]:
        terms = [t for t in query.lower().split() if t]
        scored: list[tuple[int, RegisteredTool]] = []
        for rt in self.tools.values():
            if not rt.available:
                continue
            if surface and rt.surface.name != surface:
                continue
            hay = " ".join(
                [rt.spec.name, rt.spec.summary, *rt.spec.keywords, rt.surface.name]
            ).lower()
            score = sum(hay.count(t) for t in terms)
            if score:
                scored.append((score, rt))
        scored.sort(key=lambda s: -s[0])
        return [self.describe(rt.spec.name) for _, rt in scored[:max_results]]

    def describe(self, name: str) -> dict[str, Any]:
        rt = self.tools[name]
        return {
            "name": rt.spec.name,
            "surface": rt.surface.name,
            "summary": rt.spec.summary,
            "tier": int(rt.spec.tier),
            "readOnlyHint": rt.spec.tier == Tier.T0_READ,
            "destructiveHint": rt.spec.tier >= Tier.T2_RISKY,
            "schema": rt.spec.schema,
            "available": rt.available,
            "unavailable_reason": rt.unavailable_reason,
        }

    async def dispatch(self, ctx: Any, name: str, args: dict[str, Any]) -> Any:
        rt = self.tools.get(name)
        if rt is None:
            raise KeyError(f"unknown tool: {name}")
        if not rt.available:
            raise PermissionError(f"tool unavailable ({rt.unavailable_reason})")
        mod = self._impl_cache.get(rt.surface.impl_module)
        if mod is None:
            mod = importlib.import_module(rt.surface.impl_module)
            self._impl_cache[rt.surface.impl_module] = mod
        fn = getattr(mod, name)
        return await fn(ctx, **args)
