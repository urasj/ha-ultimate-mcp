"""StorageEditor — the backup→stop→journal-first→atomic-edit→validate→start
protocol (W2, apply ordering reworked in 0.2.5).

Single implementation every .storage-mutating tool routes through
(architecture.md §4 ".storage edit protocol"):

  1. partial backup (homeassistant: true) via ctx.supervisor (generous timeout)
  2. POST /core/stop
  3. copy target file(s) to /data/undo/<undo_id>/ (pre-images, BEFORE any write)
  4. write-ahead journal entry: status "pending", referencing the undo copies —
     from here the change is discoverable and reversible even if we die mid-write
  5. edit as tmp-file + os.replace (atomic), json re-parse + schema sanity
     ("version"/"key" intact, "data" present); on failure: restore copies, mark
     the entry rolled_back, fire core start, re-raise
  6. mark the journal entry "committed" — the mutation is now done
  7. POST /core/start with a SHORT timeout and return immediately. Core takes
     60-120 s to boot and production sits behind a 90 s proxy, so the response
     never blocks on the boot: `core_restart` reports started / in_progress /
     start_failed, and a start hiccup is NEVER reported as a mutation failure
     (the write already committed).

All Supervisor interaction goes through ctx.supervisor and all filesystem
paths resolve through ctx.fs, so tests can stub both uniformly.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from ultimate_mcp.context import DATA_DIR, Context

JOURNAL = DATA_DIR / "journal.jsonl"
UNDO_ROOT = DATA_DIR / "undo"

_MAX_DIFF_PATHS = 200

MutateFn = Callable[[dict[str, Any]], dict[str, Any]]
TextFn = Callable[[str], str]


# ---------------------------------------------------------------- diff helper
def diff_summary(old: Any, new: Any) -> dict[str, list[str]]:
    """Structured diff: JSON-pointer-ish paths that were added/removed/changed."""
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []

    def walk(a: Any, b: Any, path: str) -> None:
        if len(added) + len(removed) + len(changed) >= _MAX_DIFF_PATHS:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for k in a.keys() | b.keys():
                p = f"{path}/{k}"
                if k not in b:
                    removed.append(p)
                elif k not in a:
                    added.append(p)
                else:
                    walk(a[k], b[k], p)
        elif isinstance(a, list) and isinstance(b, list):
            for i in range(max(len(a), len(b))):
                p = f"{path}/{i}"
                if i >= len(b):
                    removed.append(p)
                elif i >= len(a):
                    added.append(p)
                else:
                    walk(a[i], b[i], p)
        elif a != b:
            changed.append(path or "/")

    walk(old, new, "")
    return {"added": sorted(added), "removed": sorted(removed), "changed": sorted(changed)}


# ---------------------------------------------------------------- the editor
class StorageEditor:
    """Guarded editor for /config/.storage/* JSON files (and companion YAML)."""

    def __init__(
        self,
        ctx: Context,
        *,
        backup_timeout: float = 300.0,
        stop_timeout: float = 90.0,
        start_post_timeout: float = 10.0,
    ) -> None:
        self.ctx = ctx
        self.backup_timeout = backup_timeout
        self.stop_timeout = stop_timeout
        self.start_post_timeout = start_post_timeout

    # -- public API -----------------------------------------------------
    async def edit(
        self,
        storage_key: str,
        mutate_fn: MutateFn,
        dry_run: bool = True,
        name_hint: str = "storage-edit",
    ) -> dict[str, Any]:
        """Edit one .storage file. Delegates to edit_many (shared protocol)."""
        return await self.edit_many([(storage_key, mutate_fn)], dry_run=dry_run, name_hint=name_hint)

    async def edit_many(
        self,
        edits: list[tuple[str, MutateFn]],
        dry_run: bool = True,
        name_hint: str = "storage-edit",
        text_edits: list[tuple[str, TextFn]] | None = None,
    ) -> dict[str, Any]:
        """Apply several .storage mutations (plus optional text-file rewrites,
        e.g. YAML reference updates) inside ONE backup/stop/start window.

        Each edits item is (storage_key, mutate_fn(data)->data); each text_edits
        item is (rel_path_from_config_root, transform(text)->text).
        """
        text_edits = text_edits or []

        # 1. compute plans (pure, no side effects)
        storage_plans: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for key, fn in edits:
            original = self.ctx.fs.read_storage(key)
            mutated = fn(copy.deepcopy(original))
            if mutated is None:
                raise ValueError(f"mutate_fn for {key!r} returned None; must return the data dict")
            self._sanity(key, original, mutated)
            storage_plans.append((key, original, mutated))

        text_plans: list[tuple[str, str, str]] = []
        for rel, tfn in text_edits:
            before = self.ctx.fs.read_text(rel)
            after = tfn(before)
            text_plans.append((rel, before, after))

        diff = {f".storage/{k}": diff_summary(o, m) for k, o, m in storage_plans}
        for rel, before, after in text_plans:
            n = sum(1 for a, b in zip(before.splitlines(), after.splitlines()) if a != b)
            n += abs(len(before.splitlines()) - len(after.splitlines()))
            diff[rel] = {"added": [], "removed": [], "changed": [f"{n} line(s)"]}

        if dry_run:
            return {"would_change": diff, "checkpoint_required": "partial:homeassistant"}

        # Idempotency backstop: a retried apply whose change already landed
        # must not stop core / write / journal again.
        if all(o == m for _, o, m in storage_plans) and all(b == a for _, b, a in text_plans):
            return {
                "applied": True,
                "no_op": True,
                "changed": {},
                "note": "document(s) already reflect this change; nothing was written",
            }

        # 1. checkpoint: partial backup scoped to homeassistant (can be slow)
        backup = await self.ctx.supervisor.post(
            "/backups/new/partial",
            {"name": f"umcp-{name_hint}-{int(time.time())}", "homeassistant": True},
            timeout=self.backup_timeout,
        )
        slug = (backup.get("data") or {}).get("slug", "")

        # 2. stop core before touching .storage (failure here is a clean abort:
        # nothing has been written yet)
        await self.ctx.supervisor.post("/core/stop", timeout=self.stop_timeout)

        undo_id = f"{int(time.time())}-{secrets.token_hex(4)}"
        undo_dir = UNDO_ROOT / undo_id
        undo_dir.mkdir(parents=True, exist_ok=True)
        restored: list[tuple[Path, Path]] = []  # (undo_copy, live_path)

        # 3. pre-image copies for EVERY file, before any write
        for key, _original, _mutated in storage_plans:
            live = self.ctx.fs.resolve(f".storage/{key}")
            undo_copy = undo_dir / f".storage__{key}"
            shutil.copy2(live, undo_copy)
            restored.append((undo_copy, live))
        for rel, _before, _after in text_plans:
            live = self.ctx.fs.resolve(rel)
            undo_copy = undo_dir / rel.replace("/", "__")
            shutil.copy2(live, undo_copy)
            restored.append((undo_copy, live))

        # 4. write-ahead journal: pending entry referencing the undo copies.
        # From here on, a crash at any point leaves a discoverable, undoable
        # record — the entry is only marked committed after the writes land.
        entry_id = self._journal(
            {
                "action": "storage_edit",
                "status": "pending",
                "hint": name_hint,
                "backup_slug": slug,
                "undo_id": undo_id,
                "files": [f".storage/{k}" for k, _, _ in storage_plans]
                + [rel for rel, _, _ in text_plans],
                "diff": diff,
            }
        )

        try:
            # 5. atomic writes + re-parse sanity of what actually landed
            for (key, _original, mutated), (_copy, live) in zip(
                storage_plans, restored[: len(storage_plans)]
            ):
                self._atomic_write(live, json.dumps(mutated, ensure_ascii=False, indent=2))
                reparsed = json.loads(live.read_text(encoding="utf-8"))
                self._sanity(key, _original, reparsed)
            for (rel, _before, after), (_copy, live) in zip(
                text_plans, restored[len(storage_plans):]
            ):
                self._atomic_write(live, after)
        except Exception as exc:
            self._rollback(restored)
            self._journal_update(entry_id, status="rolled_back", error=str(exc))
            await self._start_core()  # never raises
            raise RuntimeError(
                f"storage edit failed and was rolled back (undo {undo_id}): {exc}"
            ) from exc

        # 6. commit BEFORE core start — the mutation is done; anything after
        # this is a post-step whose failure must not read as a failed apply
        self._journal_update(entry_id, status="committed")

        # 7. fire core start and return promptly (no boot poll in-path)
        core_restart = await self._start_core()
        return {
            "applied": True,
            "changed": diff,
            "undo_id": undo_id,
            "backup_slug": slug,
            "journal_id": entry_id,
            "core_restart": core_restart,
        }

    # -- internals --------------------------------------------------------
    @staticmethod
    def _sanity(key: str, original: dict[str, Any], mutated: Any) -> None:
        """Envelope invariants: version/key intact, data payload still present."""
        if not isinstance(mutated, dict):
            raise ValueError(f"{key}: mutated document is not a JSON object")
        for field in ("version", "key"):
            if field in original and mutated.get(field) != original.get(field):
                raise ValueError(
                    f"{key}: envelope field {field!r} changed "
                    f"({original.get(field)!r} -> {mutated.get(field)!r})"
                )
        if "data" in original and "data" not in mutated:
            raise ValueError(f"{key}: 'data' payload missing after mutation")

    @staticmethod
    def _atomic_write(target: Path, content: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    @staticmethod
    def _rollback(restored: list[tuple[Path, Path]]) -> None:
        for undo_copy, live in restored:
            shutil.copy2(undo_copy, live)

    async def _start_core(self) -> str:
        """Request core start without blocking on the boot. Never raises: the
        mutation (if any) is already committed, so a start hiccup is reported
        as structured status, not as an apply failure."""
        try:
            await self.ctx.supervisor.post("/core/start", timeout=self.start_post_timeout)
            return "started"
        except httpx.TimeoutException:
            return (
                "in_progress (start requested; core boot outlived the HTTP read "
                "timeout — poll core status if you need confirmation)"
            )
        except Exception as exc:  # noqa: BLE001 — post-commit; report, don't fail
            return f"start_failed: {exc} — POST /core/start via the Supervisor manually"

    @staticmethod
    def _journal_update(entry_id: str, **fields: Any) -> None:
        StorageEditor._journal({"action": "journal_update", "ref": entry_id, **fields})

    @staticmethod
    def _journal(entry: dict[str, Any]) -> str:
        entry = {"id": secrets.token_hex(6), "ts": time.time(), **entry}
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return entry["id"]
