"""Safety kernel — tier enforcement, checkpoints, confirm tokens, undo journal (W0).

Full storage_editor (stop→backup→atomic-edit→validate→start) lands in W2 and
routes through this kernel.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from ultimate_mcp.context import DATA_DIR, Context
from ultimate_mcp.spec import Tier

JOURNAL = DATA_DIR / "journal.jsonl"
UNDO_DIR = DATA_DIR / "undo"


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

    def _journal_find(self, entry_id: str) -> dict[str, Any] | None:
        if not JOURNAL.exists():
            return None
        for ln in JOURNAL.read_text(encoding="utf-8").strip().splitlines():
            entry = json.loads(ln)
            if entry.get("id") == entry_id:
                return entry
        return None

    def record(
        self,
        action: str,
        target: str,
        undo_artifact_path: str | os.PathLike | None = None,
        **meta: Any,
    ) -> str:
        """Journal a mutation. Tool impls call this BEFORE changing `target`.

        If undo_artifact_path is given (usually the target file itself, pre-change,
        or a saved copy of it), it is copied to /data/undo/<entry_id>/ so
        undo(entry_id) can atomically restore it to `target` later. Returns the
        journal entry id.
        """
        entry_id = secrets.token_hex(6)
        entry: dict[str, Any] = {"id": entry_id, "action": action, "target": target, **meta}
        if undo_artifact_path is not None:
            src = Path(undo_artifact_path)
            dest_dir = UNDO_DIR / entry_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src.name
            shutil.copy2(src, dest)
            entry["undo_artifact"] = str(dest)
        self._journal(entry)
        return entry_id

    async def undo(self, entry_id: str) -> dict[str, Any]:
        """Restore the pre-change artifact for a journaled entry (atomic os.replace).

        Entries recorded without an undo artifact (service calls, checkpoints, …)
        are reported as not undoable rather than raising.
        """
        entry = self._journal_find(entry_id)
        if entry is None:
            return {"undoable": False, "reason": f"no journal entry with id {entry_id!r}"}
        artifact = entry.get("undo_artifact")
        if not artifact:
            return {
                "undoable": False,
                "reason": f"entry {entry_id!r} ({entry.get('action')}) has no undo artifact",
            }
        artifact_path = Path(artifact)
        if not artifact_path.exists():
            return {"undoable": False, "reason": f"undo artifact missing: {artifact}"}
        target = Path(str(entry.get("target", "")))
        if not str(target):
            return {"undoable": False, "reason": f"entry {entry_id!r} has no restore target"}
        # /data and the target mount may be different filesystems, so a direct
        # os.replace(artifact, target) could fail with EXDEV. Copy the artifact
        # to a temp file NEXT TO the target, then os.replace — still atomic.
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{target.name}.umcp-undo-{secrets.token_hex(4)}"
        try:
            shutil.copy2(artifact_path, tmp)
            os.replace(tmp, target)
        finally:
            if tmp.exists():  # only on failure between copy and replace
                tmp.unlink(missing_ok=True)
        undo_journal_id = self._journal(
            {"action": "undo", "target": str(target), "undid": entry_id}
        )
        return {
            "undoable": True,
            "restored": str(target),
            "entry_id": entry_id,
            "journal_id": undo_journal_id,
        }
