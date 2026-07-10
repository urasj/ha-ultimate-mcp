"""Safety kernel — tier enforcement, checkpoints, confirm tokens, undo journal (W0).

Full storage_editor (stop→backup→atomic-edit→validate→start) lands in W2 and
routes through this kernel.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from ultimate_mcp.context import DATA_DIR, Context
from ultimate_mcp.spec import Tier

JOURNAL = DATA_DIR / "journal.jsonl"


class SafetyKernel:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self._pending_tokens: dict[str, str] = {}  # token -> tool name
        self._session_checkpoints: list[str] = []

    async def authorize(
        self,
        registry: Any,
        name: str,
        dry_run: bool,
        confirm_token: str | None,
        external_checkpoint_ref: str | None,
    ) -> None:
        rt = registry.tools.get(name)
        if rt is None:
            raise KeyError(f"unknown tool: {name}")
        tier = rt.spec.tier
        if tier == Tier.T0_READ or dry_run:
            return
        if tier >= Tier.T2_RISKY and not self._session_checkpoints and not external_checkpoint_ref:
            raise PermissionError(
                "T2+ tool requires a checkpoint first: call umcp_checkpoint "
                "(or pass external_checkpoint_ref, e.g. a Proxmox snapshot of vmid 100)"
            )
        if tier == Tier.T3_DESTRUCTIVE:
            if not self.ctx.options.get("destructive_enabled", False):
                raise PermissionError("T3 disabled: set destructive_enabled: true in add-on options")
            if not confirm_token or self._pending_tokens.pop(confirm_token, None) != name:
                raise PermissionError(
                    "T3 requires confirm_token from this tool's dry-run response"
                )

    def mint_token(self, tool_name: str) -> str:
        token = secrets.token_urlsafe(12)
        self._pending_tokens[token] = tool_name
        return token

    async def checkpoint(self, scope: str, name_hint: str) -> dict[str, Any]:
        body = {"name": f"umcp-{name_hint}-{int(time.time())}", "homeassistant": scope != "addons"}
        resp = await self.ctx.supervisor.post("/backups/new/partial", body)
        slug = (resp.get("data") or {}).get("slug", "")
        if slug:
            self._session_checkpoints.append(slug)
            self._journal({"action": "checkpoint", "slug": slug, "scope": scope})
        return {"slug": slug, "scope": scope}

    def _journal(self, entry: dict[str, Any]) -> str:
        entry = {"id": secrets.token_hex(6), "ts": time.time(), **entry}
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return entry["id"]

    def journal_tail(self, limit: int = 20) -> list[dict[str, Any]]:
        if not JOURNAL.exists():
            return []
        lines = JOURNAL.read_text(encoding="utf-8").strip().splitlines()
        return [json.loads(ln) for ln in lines[-limit:]]

    async def undo(self, entry_id: str) -> dict[str, Any]:
        # W0: replay inverse ops from /data/undo/<entry_id>/ artifacts
        raise NotImplementedError("undo replay lands with storage_editor (W2)")
