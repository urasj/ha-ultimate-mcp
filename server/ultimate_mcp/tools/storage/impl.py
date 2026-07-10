"""storage/ surface implementation — .storage registry surgery (W2).

Reads go straight through ctx.fs; every write routes through
safety/storage_editor.StorageEditor (backup -> stop -> atomic edit -> verify -> start).
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from ultimate_mcp.context import Context
from ultimate_mcp.safety.storage_editor import StorageEditor

_MASK = "***MASKED***"
_SENSITIVE = ("token", "secret", "password", "psk", "api_key", "credential")
_ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_SKIP_DIRS = {"deps", "__pycache__", "custom_components"}


def _editor(ctx: Context) -> StorageEditor:
    """Factory so tests can monkeypatch editor construction if needed."""
    return StorageEditor(ctx)


# ------------------------------------------------------------------ T0 reads
def _mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _MASK if any(s in k.lower() for s in _SENSITIVE) else _mask(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask(v) for v in value]
    return value


async def storage_read(ctx: Context, key: str, **_: Any) -> dict[str, Any]:
    doc = ctx.fs.read_storage(key)
    return {"storage_key": key, "document": _mask(doc)}


async def storage_list(ctx: Context, **_: Any) -> list[dict[str, Any]]:
    storage_dir = ctx.fs.resolve(".storage")
    out: list[dict[str, Any]] = []
    for p in sorted(storage_dir.iterdir()):
        if not p.is_file():
            continue
        item: dict[str, Any] = {"file": p.name, "bytes": p.stat().st_size}
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            item["key"] = doc.get("key")
            item["version"] = doc.get("version")
        except (ValueError, UnicodeDecodeError):
            item["key"] = None
            item["version"] = None
            item["parse_error"] = True
        out.append(item)
    return out


async def storage_orphan_scan(ctx: Context, **_: Any) -> dict[str, Any]:
    """Report-only cross-reference of the three core registries."""
    entities = ctx.fs.read_storage("core.entity_registry")["data"].get("entities", [])
    devices = ctx.fs.read_storage("core.device_registry")["data"].get("devices", [])
    entries = ctx.fs.read_storage("core.config_entries")["data"].get("entries", [])

    device_ids = {d.get("id") for d in devices}
    entry_ids = {e.get("entry_id") for e in entries}

    orphan_entities = [
        {"entity_id": e.get("entity_id"), "platform": e.get("platform"), "missing_device": e.get("device_id")}
        for e in entities
        if e.get("device_id") and e.get("device_id") not in device_ids
    ]
    orphan_devices = [
        {"id": d.get("id"), "name": d.get("name"), "missing_config_entries": d.get("config_entries", [])}
        for d in devices
        if d.get("config_entries") and not any(ce in entry_ids for ce in d.get("config_entries", []))
    ]
    return {
        "orphan_entities": orphan_entities,
        "orphan_devices": orphan_devices,
        "counts": {
            "entities_scanned": len(entities),
            "devices_scanned": len(devices),
            "config_entries": len(entries),
            "orphan_entities": len(orphan_entities),
            "orphan_devices": len(orphan_devices),
        },
    }


# ------------------------------------------------- dependency graph (T0)
def _ref_re(entity_id: str) -> re.Pattern[str]:
    # Preceding "." allowed on purpose: template usage like states.light.kitchen.state.
    return re.compile(rf"(?<!\w){re.escape(entity_id)}(?!\w)")


def _scan_text(text: str, rx: re.Pattern[str]) -> tuple[int, str | None]:
    count = len(rx.findall(text))
    if not count:
        return 0, None
    for line in text.splitlines():
        if rx.search(line):
            return count, line.strip()[:160]
    return count, None


def _iter_yaml_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if p.suffix not in (".yaml", ".yml") or not p.is_file():
            continue
        rel_parts = p.relative_to(root).parts
        if any(part.startswith(".") or part in _SKIP_DIRS for part in rel_parts[:-1]):
            continue
        out.append(p)
    return out


def _find_refs(ctx: Context, entity_id: str) -> list[dict[str, Any]]:
    rx = _ref_re(entity_id)
    root = ctx.fs.root
    refs: list[dict[str, Any]] = []

    for p in _iter_yaml_files(root):
        rel = p.relative_to(root).as_posix()
        try:
            count, sample = _scan_text(ctx.fs.read_text(rel), rx)
        except (ValueError, OSError):
            continue
        if count:
            refs.append({"file": rel, "where": "yaml", "count": count, "sample_line": sample})

    storage_dir = ctx.fs.resolve(".storage")
    if storage_dir.is_dir():
        for p in sorted(storage_dir.iterdir()):
            if not p.is_file():
                continue
            try:
                count, sample = _scan_text(p.read_text(encoding="utf-8"), rx)
            except (OSError, UnicodeDecodeError):
                continue
            if count:
                where = (
                    "entity_registry"
                    if p.name == "core.entity_registry"
                    else ("dashboard" if p.name.startswith("lovelace") else "storage")
                )
                refs.append(
                    {"file": f".storage/{p.name}", "where": where, "count": count, "sample_line": sample}
                )
    return refs


async def dependency_graph(ctx: Context, entity_id: str, **_: Any) -> dict[str, Any]:
    refs = _find_refs(ctx, entity_id)
    return {
        "entity_id": entity_id,
        "references": refs,
        "total_references": sum(r["count"] for r in refs),
        "files": len(refs),
    }


# ------------------------------------------------------------- T2 writes
def _replace_in_doc(value: Any, rx: re.Pattern[str], new: str) -> Any:
    if isinstance(value, dict):
        return {k: _replace_in_doc(v, rx, new) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_in_doc(v, rx, new) for v in value]
    if isinstance(value, str):
        return rx.sub(new, value)
    return value


async def entity_rename_deep(
    ctx: Context,
    old_entity_id: str,
    new_entity_id: str,
    dry_run: bool = True,
    **_: Any,
) -> dict[str, Any]:
    for eid in (old_entity_id, new_entity_id):
        if not _ENTITY_ID_RE.match(eid):
            raise ValueError(f"invalid entity_id: {eid!r}")
    if old_entity_id == new_entity_id:
        raise ValueError("old and new entity_id are identical")

    registry = ctx.fs.read_storage("core.entity_registry")
    reg_hits = [e for e in registry["data"].get("entities", []) if e.get("entity_id") == old_entity_id]
    if not reg_hits:
        raise ValueError(f"{old_entity_id} not found in core.entity_registry")
    if any(e.get("entity_id") == new_entity_id for e in registry["data"].get("entities", [])):
        raise ValueError(f"{new_entity_id} already exists in core.entity_registry")

    rx = _ref_re(old_entity_id)
    refs = _find_refs(ctx, old_entity_id)

    def rename_registry(data: dict[str, Any]) -> dict[str, Any]:
        for e in data["data"].get("entities", []):
            if e.get("entity_id") == old_entity_id:
                e["entity_id"] = new_entity_id
        return data

    storage_edits: list[tuple[str, Any]] = [("core.entity_registry", rename_registry)]
    text_edits: list[tuple[str, Any]] = []
    plan_sites: list[dict[str, Any]] = [
        {
            "file": ".storage/core.entity_registry",
            "where": "entity_registry",
            "action": f"entity_id {old_entity_id} -> {new_entity_id}",
        }
    ]

    for ref in refs:
        if ref["file"] == ".storage/core.entity_registry":
            continue  # handled structurally above
        if ref["file"].startswith(".storage/"):
            key = ref["file"].removeprefix(".storage/")

            def rewrite_storage(data: dict[str, Any]) -> dict[str, Any]:
                return _replace_in_doc(data, rx, new_entity_id)

            storage_edits.append((key, rewrite_storage))
        else:
            text_edits.append((ref["file"], lambda text: rx.sub(new_entity_id, text)))
        plan_sites.append(
            {
                "file": ref["file"],
                "where": ref["where"],
                "action": f"rewrite {ref['count']} reference(s)",
                "sample_line": ref.get("sample_line"),
            }
        )

    if dry_run:
        preview = await _editor(ctx).edit_many(
            storage_edits, dry_run=True, name_hint="entity-rename", text_edits=text_edits
        )
        return {
            "old_entity_id": old_entity_id,
            "new_entity_id": new_entity_id,
            "plan": plan_sites,
            **preview,
        }

    result = await _editor(ctx).edit_many(
        storage_edits, dry_run=False, name_hint="entity-rename", text_edits=text_edits
    )
    return {"old_entity_id": old_entity_id, "new_entity_id": new_entity_id, "sites": plan_sites, **result}


async def storage_orphan_clean(ctx: Context, dry_run: bool = True, **_: Any) -> dict[str, Any]:
    scan = await storage_orphan_scan(ctx)
    orphan_eids = {o["entity_id"] for o in scan["orphan_entities"]}
    orphan_dids = {o["id"] for o in scan["orphan_devices"]}
    if not orphan_eids and not orphan_dids:
        return {"orphans": scan, "changed": {}, "note": "nothing to clean"}

    def clean_entities(data: dict[str, Any]) -> dict[str, Any]:
        data["data"]["entities"] = [
            e for e in data["data"].get("entities", []) if e.get("entity_id") not in orphan_eids
        ]
        return data

    def clean_devices(data: dict[str, Any]) -> dict[str, Any]:
        data["data"]["devices"] = [
            d for d in data["data"].get("devices", []) if d.get("id") not in orphan_dids
        ]
        return data

    edits: list[tuple[str, Any]] = []
    if orphan_eids:
        edits.append(("core.entity_registry", clean_entities))
    if orphan_dids:
        edits.append(("core.device_registry", clean_devices))

    result = await _editor(ctx).edit_many(edits, dry_run=dry_run, name_hint="orphan-clean")
    return {"orphans": scan, **result}


# ------------------------------------------------- RFC-6902 subset (T2)
def _pointer_walk(doc: Any, pointer: str) -> tuple[Any, str]:
    """Return (parent, last_token) for a JSON pointer like /data/entities/0/name."""
    if not pointer.startswith("/"):
        raise ValueError(f"invalid JSON pointer: {pointer!r}")
    tokens = [t.replace("~1", "/").replace("~0", "~") for t in pointer.split("/")[1:]]
    parent: Any = doc
    for tok in tokens[:-1]:
        if isinstance(parent, list):
            parent = parent[int(tok)]
        elif isinstance(parent, dict):
            if tok not in parent:
                raise KeyError(f"pointer segment {tok!r} not found ({pointer})")
            parent = parent[tok]
        else:
            raise TypeError(f"cannot descend into {type(parent).__name__} at {tok!r} ({pointer})")
    return parent, tokens[-1]


def _apply_patch(doc: dict[str, Any], patch: list[dict[str, Any]]) -> dict[str, Any]:
    for i, op in enumerate(patch):
        kind = op.get("op")
        path = op.get("path", "")
        parent, last = _pointer_walk(doc, path)
        if kind == "add":
            if isinstance(parent, list):
                idx = len(parent) if last == "-" else int(last)
                parent.insert(idx, op["value"])
            elif isinstance(parent, dict):
                parent[last] = op["value"]
            else:
                raise TypeError(f"op {i}: cannot add to {type(parent).__name__}")
        elif kind == "replace":
            if isinstance(parent, list):
                parent[int(last)] = op["value"]
            elif isinstance(parent, dict):
                if last not in parent:
                    raise KeyError(f"op {i}: replace target missing: {path}")
                parent[last] = op["value"]
            else:
                raise TypeError(f"op {i}: cannot replace in {type(parent).__name__}")
        elif kind == "remove":
            if isinstance(parent, list):
                del parent[int(last)]
            elif isinstance(parent, dict):
                if last not in parent:
                    raise KeyError(f"op {i}: remove target missing: {path}")
                del parent[last]
            else:
                raise TypeError(f"op {i}: cannot remove from {type(parent).__name__}")
        else:
            raise ValueError(f"op {i}: unsupported op {kind!r} (add/replace/remove only)")
    return doc


async def storage_patch(
    ctx: Context,
    key: str,
    json_patch: list[dict[str, Any]],
    dry_run: bool = True,
    **_: Any,
) -> dict[str, Any]:
    if not isinstance(json_patch, list) or not json_patch:
        raise ValueError("json_patch must be a non-empty list of {op, path[, value]}")

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        return _apply_patch(copy.deepcopy(data), copy.deepcopy(json_patch))

    return await _editor(ctx).edit(key, mutate, dry_run=dry_run, name_hint=f"patch-{key}")
