"""Safety kernel — tier enforcement, checkpoints, confirm tokens, undo journal.

0.2.4: the gate state is no longer write-only. Checkpoints register with a
timestamp and are honored within a TTL; T3 confirm tokens are minted by the
gateway on dry-run, bound to (tool, canonical args hash), single-use, and
TTL-limited, with distinct rejection reasons (token_missing / token_unknown /
token_expired / token_args_mismatch).
"""

from __future__ import annotations

import hashlib
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

TOKEN_TTL_SECONDS = 900  # 15 min — a confirm_token must be used within this window
CHECKPOINT_TTL_SECONDS_DEFAULT = 1800  # 30 min; override with checkpoint_ttl_seconds option


def canonical_args_hash(args: dict[str, Any] | None) -> str:
    """Order-independent hash of the tool args, ignoring the dry_run flag
    (the dry-run and apply calls differ only in that flag)."""
    basis = {k: v for k, v in (args or {}).items() if k != "dry_run"}
    encoded = json.dumps(basis, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SafetyKernel:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        # token -> {"tool": name, "args_hash": hash, "expires_at": epoch}
        self._pending_tokens: dict[str, dict[str, Any]] = {}
        # {"checkpoint_id": slug, "created_at": epoch, "scope": scope}
        # Process-wide (the kernel is a module-level singleton in app.py), NOT
        # per-MCP-session: streamable-HTTP clients routinely re-initialize
        # sessions between the checkpoint call and the apply.
        self._checkpoints: list[dict[str, Any]] = []

    # ------------------------------------------------------------ gate state
    @property
    def checkpoint_ttl(self) -> float:
        return float(self.ctx.options.get("checkpoint_ttl_seconds", CHECKPOINT_TTL_SECONDS_DEFAULT))

    def _live_checkpoints(self) -> list[dict[str, Any]]:
        now = time.time()
        ttl = self.checkpoint_ttl
        return [c for c in self._checkpoints if now - c["created_at"] <= ttl]

    def checkpoint_remediation(self) -> str:
        return (
            "checkpoint_required: no live checkpoint (TTL "
            f"{int(self.checkpoint_ttl)}s). Either call umcp_checkpoint to create a "
            "Supervisor partial backup, or pass external_checkpoint_ref with your own "
            "checkpoint reference (e.g. a Proxmox snapshot name of the HA VM)."
        )

    def checkpoint_status(self, external_checkpoint_ref: str | None = None) -> dict[str, Any]:
        """Gate-visible checkpoint state, embedded in T2+/T3 dry-run responses."""
        if external_checkpoint_ref:
            return {"satisfied": True, "source": "external", "ref": external_checkpoint_ref}
        live = self._live_checkpoints()
        if live:
            latest = max(live, key=lambda c: c["created_at"])
            return {
                "satisfied": True,
                "source": "umcp_checkpoint",
                "checkpoint_id": latest["checkpoint_id"],
                "age_seconds": round(time.time() - latest["created_at"], 1),
                "ttl_seconds": int(self.checkpoint_ttl),
            }
        return {
            "satisfied": False,
            "ttl_seconds": int(self.checkpoint_ttl),
            "remediation": self.checkpoint_remediation(),
        }

    # ------------------------------------------------------------ authorize
    async def authorize(
        self,
        registry: Any,
        name: str,
        dry_run: bool,
        confirm_token: str | None,
        external_checkpoint_ref: str | None,
        args_hash: str | None = None,
    ) -> None:
        rt = registry.tools.get(name)
        if rt is None:
            raise KeyError(f"unknown tool: {name}")
        tier = rt.spec.tier
        if tier == Tier.T0_READ or dry_run:
            return
        if tier == Tier.T3_DESTRUCTIVE and not self.ctx.options.get("destructive_enabled", False):
            raise PermissionError("T3 disabled: set destructive_enabled: true in add-on options")
        if tier >= Tier.T2_RISKY and not external_checkpoint_ref and not self._live_checkpoints():
            raise PermissionError(self.checkpoint_remediation())
        if tier == Tier.T3_DESTRUCTIVE:
            self._consume_token(name, confirm_token, args_hash)

    def _consume_token(self, name: str, token: str | None, args_hash: str | None) -> None:
        if not token:
            raise PermissionError(
                "token_missing: T3 requires the confirm_token from this tool's dry-run "
                "response — call again with dry_run=true first"
            )
        info = self._pending_tokens.get(token)
        if info is None:
            raise PermissionError(
                "token_unknown: confirm_token not recognized (already used, or minted "
                "before a server restart) — re-run dry_run=true for a fresh token"
            )
        if time.time() > info["expires_at"]:
            del self._pending_tokens[token]
            raise PermissionError(
                f"token_expired: confirm_token outlived its {TOKEN_TTL_SECONDS}s TTL — "
                "re-run dry_run=true for a fresh token"
            )
        if info["tool"] != name or (args_hash is not None and info["args_hash"] != args_hash):
            # NOT consumed: the caller may retry with the args the token was minted for
            raise PermissionError(
                "token_args_mismatch: confirm_token was minted for a different tool or "
                "args — re-run with exactly the dry-run's name/args, or dry-run again"
            )
        del self._pending_tokens[token]  # single-use

    def mint_token(self, tool_name: str, args_hash: str) -> str:
        now = time.time()
        # opportunistic prune so abandoned dry-runs don't accumulate
        self._pending_tokens = {
            t: i for t, i in self._pending_tokens.items() if i["expires_at"] >= now
        }
        token = secrets.token_urlsafe(12)
        self._pending_tokens[token] = {
            "tool": tool_name,
            "args_hash": args_hash,
            "expires_at": now + TOKEN_TTL_SECONDS,
        }
        return token

    # ------------------------------------------------------------ checkpoint
    async def checkpoint(self, scope: str, name_hint: str) -> dict[str, Any]:
        body = {"name": f"umcp-{name_hint}-{int(time.time())}", "homeassistant": scope != "addons"}
        resp = await self.ctx.supervisor.post("/backups/new/partial", body)
        slug = (resp.get("data") or {}).get("slug", "")
        if slug:
            self._checkpoints.append(
                {"checkpoint_id": slug, "created_at": time.time(), "scope": scope}
            )
            self._journal({"action": "checkpoint", "slug": slug, "scope": scope})
        return {
            "slug": slug,
            "scope": scope,
            "registered": bool(slug),
            "ttl_seconds": int(self.checkpoint_ttl),
        }

    # ------------------------------------------------------------ journal
    # The journal is append-only JSONL. 0.2.5 adds write-ahead semantics:
    # a mutation FIRST appends a base entry with status "pending", then later
    # appends {"action": "journal_update", "ref": <id>, ...} records that are
    # folded into the base entry on read (status "committed" / "failed" / ...).
    # Append-only means a crash mid-flight always leaves the pending record.

    def _journal(self, entry: dict[str, Any]) -> str:
        entry = {"id": secrets.token_hex(6), "ts": time.time(), **entry}
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return entry["id"]

    def journal_open(self, action: str, **meta: Any) -> str:
        """Append a write-ahead entry (status pending) BEFORE mutating."""
        return self._journal({"action": action, "status": "pending", **meta})

    def journal_update(self, entry_id: str, **fields: Any) -> None:
        """Append an update record for an existing entry (folded on read)."""
        self._journal({"action": "journal_update", "ref": entry_id, **fields})

    def attach_undo_artifact(
        self, entry_id: str, src_path: str | os.PathLike, target: str
    ) -> None:
        """Copy a pre-change file into this entry's undo dir and record it."""
        src = Path(src_path)
        dest_dir = UNDO_DIR / entry_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        shutil.copy2(src, dest)
        self.journal_update(entry_id, undo_artifact=str(dest), target=target)

    def _read_entries(self) -> list[dict[str, Any]]:
        """All journal entries with journal_update records folded in."""
        if not JOURNAL.exists():
            return []
        base: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for ln in JOURNAL.read_text(encoding="utf-8").strip().splitlines():
            entry = json.loads(ln)
            if entry.get("action") == "journal_update" and entry.get("ref"):
                target = base.get(entry["ref"])
                if target is not None:
                    target.update(
                        {k: v for k, v in entry.items() if k not in ("id", "ts", "action", "ref")}
                    )
                continue
            base[entry["id"]] = entry
            order.append(entry["id"])
        return [base[i] for i in order]

    def journal_tail(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._read_entries()[-limit:]

    def _journal_find(self, entry_id: str) -> dict[str, Any] | None:
        for entry in self._read_entries():
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

    # ------------------------------------------------------------ undo
    async def undo(self, entry_id: str) -> dict[str, Any]:
        """Restore the pre-change artifact(s) for a journaled entry.

        Handles both single-artifact entries (record()) and StorageEditor
        entries, which carry undo_id + files instead of one undo_artifact.
        Entries with neither (service calls, checkpoints, …) are reported as
        not undoable rather than raising.
        """
        entry = self._journal_find(entry_id)
        if entry is None:
            return {"undoable": False, "reason": f"no journal entry with id {entry_id!r}"}
        if entry.get("status") == "superseded":
            return {
                "undoable": False,
                "reason": (
                    f"entry {entry_id!r} was superseded by journal entry "
                    f"{entry.get('superseded_by')!r} — undo that entry instead"
                ),
            }
        artifact = entry.get("undo_artifact")
        if not artifact:
            if entry.get("undo_id") and entry.get("files"):
                return await self._undo_fileset(entry_id, entry)
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
        self._restore(artifact_path, target)
        undo_journal_id = self._journal(
            {"action": "undo", "target": str(target), "undid": entry_id}
        )
        return {
            "undoable": True,
            "restored": str(target),
            "entry_id": entry_id,
            "journal_id": undo_journal_id,
        }

    async def _undo_fileset(self, entry_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        """Undo a StorageEditor entry: restore every file from its undo dir."""
        fs = getattr(self.ctx, "fs", None)
        if fs is None:
            return {"undoable": False, "reason": "no filesystem facade available for undo"}
        undo_dir = UNDO_DIR / str(entry["undo_id"])
        restored: list[str] = []
        for rel in entry["files"]:
            undo_copy = undo_dir / str(rel).replace("/", "__")
            if not undo_copy.exists():
                return {"undoable": False, "reason": f"undo artifact missing: {undo_copy}"}
            live = fs.resolve(str(rel))
            self._restore(undo_copy, live)
            restored.append(str(live))
        undo_journal_id = self._journal(
            {"action": "undo", "target": restored, "undid": entry_id}
        )
        return {
            "undoable": True,
            "restored": restored,
            "entry_id": entry_id,
            "journal_id": undo_journal_id,
            "note": "restart HA core if these were .storage files (core caches them in memory)",
        }

    @staticmethod
    def _restore(artifact_path: Path, target: Path) -> None:
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
