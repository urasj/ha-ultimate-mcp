"""StorageEditor — the stop→backup→atomic-edit→validate→start protocol (W2).

Single implementation every .storage-mutating tool routes through
(architecture.md §4 ".storage edit protocol"):

  1. partial backup (homeassistant: true) via ctx.supervisor
  2. POST /core/stop
  3. copy target file(s) to /data/undo/<undo_id>/
  4. edit as tmp-file + os.replace (atomic)
  5. json re-parse + schema sanity ("version"/"key" intact, "data" present)
  6. POST /core/start, poll /core/info until RUNNING (120s guard)
  7. journal entry
On any failure after stop: restore copies, start core, re-raise with context.

All Supervisor interaction goes through ctx.supervisor and all filesystem
paths resolve through ctx.fs, so tests can stub both uniformly.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import secrets
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

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
        start_timeout: float = 120.0,
        poll_interval: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.ctx = ctx
        self.start_timeout = start_timeout
        self.poll_interval = poll_interval
        self._sleep = sleep

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

        # 2. checkpoint: partial backup scoped to homeassistant
        backup = await self.ctx.supervisor.post(
            "/backups/new/partial",
            {"name": f"umcp-{name_hint}-{int(time.time())}", "homeassistant": True},
        )
        slug = (backup.get("data") or {}).get("slug", "")

        # 3. stop core before touching .storage
        await self.ctx.supervisor.post("/core/stop")

        undo_id = f"{int(time.time())}-{secrets.token_hex(4)}"
        undo_dir = UNDO_ROOT / undo_id
        undo_dir.mkdir(parents=True, exist_ok=True)
        restored: list[tuple[Path, Path]] = []  # (undo_copy, live_path)

        try:
            # 4. undo copies + atomic writes
            for key, _original, mutated in storage_plans:
                live = self.ctx.fs.resolve(f".storage/{key}")
                undo_copy = undo_dir / f".storage__{key}"
                shutil.copy2(live, undo_copy)
                restored.append((undo_copy, live))
                self._atomic_write(live, json.dumps(mutated, ensure_ascii=False, indent=2))
                # 5. re-parse + sanity check what actually landed on disk
                reparsed = json.loads(live.read_text(encoding="utf-8"))
                self._sanity(key, _original, reparsed)

            for rel, _before, after in text_plans:
                live = self.ctx.fs.resolve(rel)
                undo_copy = undo_dir / rel.replace("/", "__")
                shutil.copy2(live, undo_copy)
                restored.append((undo_copy, live))
                self._atomic_write(live, after)
        except Exception as exc:
            self._rollback(restored)
            await self.ctx.supervisor.post("/core/start")
            raise RuntimeError(f"storage edit failed and was rolled back (undo {undo_id}): {exc}") from exc

        # 6. start core and verify it comes back
        await self.ctx.supervisor.post("/core/start")
        try:
            await self._wait_core_running()
        except Exception as exc:
            # timeout guard: auto-restore the copies and start again
            self._rollback(restored)
            await self.ctx.supervisor.post("/core/start")
            raise RuntimeError(
                f"core did not return to RUNNING after edit; files restored (undo {undo_id}): {exc}"
            ) from exc

        # 7. journal
        entry_id = self._journal(
            {
                "action": "storage_edit",
                "hint": name_hint,
                "backup_slug": slug,
                "undo_id": undo_id,
                "files": [f".storage/{k}" for k, _, _ in storage_plans]
                + [rel for rel, _, _ in text_plans],
                "diff": diff,
            }
        )
        return {"changed": diff, "undo_id": undo_id, "backup_slug": slug, "journal_id": entry_id}

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

    async def _wait_core_running(self) -> None:
        deadline = time.monotonic() + self.start_timeout
        last_state = "unknown"
        while time.monotonic() < deadline:
            try:
                info = await self.ctx.supervisor.get("/core/info")
                last_state = str((info.get("data") or {}).get("state", "unknown"))
                if last_state.lower() == "running":
                    return
            except Exception:  # noqa: BLE001 — supervisor may 502 while core boots
                last_state = "unreachable"
            await self._sleep(self.poll_interval)
        raise TimeoutError(f"core not RUNNING after {self.start_timeout}s (last state: {last_state})")

    @staticmethod
    def _journal(entry: dict[str, Any]) -> str:
        entry = {"id": secrets.token_hex(6), "ts": time.time(), **entry}
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return entry["id"]
