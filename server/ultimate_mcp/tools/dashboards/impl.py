"""dashboards/ surface implementation — lazy-imported on first call (W5).

Reads use the lovelace/* WS commands (wrapped so a bad command name degrades to
{"error": ...}). The write tool saves storage-mode dashboards via the
lovelace/config/save WS command — the exact path the frontend uses. Direct
.storage writes are wrong for dashboards: core caches the store in memory,
ignores external edits, and rewrites the file on its next flush — and the
storage key is 'lovelace.<id>', which url_path does not reliably give you
(0.2.5 guessed '.storage/lovelace[.<url_path>]' and broke on every dashboard
whose id != url_path, including a default dashboard stored as
'lovelace.lovelace'; fixed in 0.2.6 by not touching files at all).
YAML-mode dashboards still go through an atomic file write via ctx.fs.

WS command spellings (cross-referenced against homeassistant.components.lovelace
websocket_api; confirmed against a live 2026.7 box):
  lovelace/dashboards/list   -> list dashboards
  lovelace/config            -> get a dashboard config (url_path=..., force=false)
  lovelace/resources         -> list resources
  lovelace/config/save       -> save a dashboard config (url_path=..., config=...)
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

from ultimate_mcp.context import Context
from ultimate_mcp.safety.storage_editor import UNDO_ROOT, StorageEditor, diff_summary

_NOTE = "verify lovelace/* WS command for 2026.7"


async def _safe_ws(ctx: Context, command: str, **kwargs: Any) -> tuple[Any, dict | None]:
    try:
        return await ctx.ha_ws.call(command, **kwargs), None
    except Exception as exc:  # noqa: BLE001 — degrade, never raise from a tool
        return None, {"error": str(exc), "note": _NOTE, "command": command}


# ------------------------------------------------------------------ T0 reads
async def dashboard_list(ctx: Context, **_: Any) -> Any:
    result, err = await _safe_ws(ctx, "lovelace/dashboards/list")
    if err is not None:
        return err
    rows = result if isinstance(result, list) else (result or {}).get("dashboards", [])
    slim = [
        {
            "url_path": d.get("url_path"),
            "title": d.get("title"),
            "mode": d.get("mode"),
            "icon": d.get("icon"),
            "require_admin": d.get("require_admin"),
            "show_in_sidebar": d.get("show_in_sidebar"),
            "id": d.get("id"),
        }
        for d in (rows or [])
    ]
    return {"count": len(slim), "dashboards": slim}


async def dashboard_get_config(ctx: Context, url_path: str | None = None, **_: Any) -> Any:
    kwargs: dict[str, Any] = {"force": False}
    if url_path is not None:
        kwargs["url_path"] = url_path
    result, err = await _safe_ws(ctx, "lovelace/config", **kwargs)
    if err is not None:
        return err
    views = (result or {}).get("views", []) if isinstance(result, dict) else []
    return {
        "url_path": url_path,
        "view_count": len(views),
        "config": result,
    }


async def dashboard_resources(ctx: Context, **_: Any) -> Any:
    result, err = await _safe_ws(ctx, "lovelace/resources")
    if err is not None:
        return err
    rows = result if isinstance(result, list) else (result or {}).get("resources", [])
    return {"count": len(rows or []), "resources": rows}


# ------------------------------------------------------------------ lint
def _lint_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    views = config.get("views") if isinstance(config, dict) else None
    if not isinstance(views, list):
        return [{"where": "root", "issue": "config has no 'views' list"}]

    def check_card(card: Any, loc: str) -> None:
        if not isinstance(card, dict):
            warnings.append({"where": loc, "issue": "card is not a mapping"})
            return
        ctype = card.get("type")
        if not ctype:
            warnings.append({"where": loc, "issue": "card is missing 'type'"})
        # Common structural expectations by card type.
        if ctype in ("entities", "glance") and not card.get("entities"):
            warnings.append({"where": loc, "issue": f"{ctype} card has no 'entities'"})
        if ctype in ("entity", "button", "light", "sensor", "gauge") and not (
            card.get("entity") or card.get("entities")
        ):
            warnings.append({"where": loc, "issue": f"{ctype} card has no 'entity'"})
        # Recurse into nested cards (vertical-stack / horizontal-stack / grid).
        for nested_key in ("cards", "card"):
            nested = card.get(nested_key)
            if isinstance(nested, list):
                for j, c in enumerate(nested):
                    check_card(c, f"{loc}/{nested_key}/{j}")
            elif isinstance(nested, dict):
                check_card(nested, f"{loc}/{nested_key}")

    for i, view in enumerate(views):
        if not isinstance(view, dict):
            warnings.append({"where": f"views/{i}", "issue": "view is not a mapping"})
            continue
        if not (view.get("title") or view.get("path")):
            warnings.append({"where": f"views/{i}", "issue": "view has neither title nor path"})
        cards = view.get("cards", [])
        if not isinstance(cards, list):
            warnings.append({"where": f"views/{i}/cards", "issue": "'cards' is not a list"})
            continue
        for k, card in enumerate(cards):
            check_card(card, f"views/{i}/cards/{k}")
    return warnings


async def dashboard_card_lint(ctx: Context, url_path: str | None = None, **_: Any) -> Any:
    got = await dashboard_get_config(ctx, url_path=url_path)
    if "error" in got:
        return got
    config = got.get("config") or {}
    warnings = _lint_config(config)
    return {
        "url_path": url_path,
        "view_count": got.get("view_count", 0),
        "warning_count": len(warnings),
        "warnings": warnings,
        "ok": not warnings,
    }


# --------------------------------------------------------------- T2 save
def _atomic_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
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


def _config_summary(config: dict[str, Any]) -> dict[str, Any]:
    views = config.get("views", []) if isinstance(config, dict) else []
    view_count = len(views) if isinstance(views, list) else 0
    card_count = 0
    if isinstance(views, list):
        for v in views:
            if isinstance(v, dict) and isinstance(v.get("cards"), list):
                card_count += len(v["cards"])
    return {"view_count": view_count, "card_count": card_count}


async def dashboard_config_save(
    ctx: Context,
    config: dict[str, Any],
    url_path: str | None = None,
    mode: str = "storage",
    yaml_path: str | None = None,
    dry_run: bool = True,
    **_: Any,
) -> Any:
    if not isinstance(config, dict):
        return {"error": "config must be a mapping (a full Lovelace config object)"}
    summary = _config_summary(config)

    # -------- YAML-mode dashboards: atomic file write ----------------------
    if mode == "yaml":
        if not yaml_path:
            return {"error": "mode=yaml requires yaml_path (the dashboard's YAML file)"}
        try:
            import yaml as _yaml

            body = _yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"cannot serialise config to YAML: {exc}"}
        try:
            live = ctx.fs.resolve(yaml_path)
        except PermissionError as exc:
            return {"error": str(exc)}
        if dry_run:
            return {
                "dry_run": True,
                "mode": "yaml",
                "yaml_path": yaml_path,
                "summary": summary,
                "bytes": len(body.encode("utf-8")),
                "note": "re-run with dry_run=false to atomically write the dashboard YAML",
            }
        try:
            _atomic_write(live, body)
        except OSError as exc:
            return {"error": f"write failed: {exc}", "yaml_path": yaml_path}
        return {"dry_run": False, "mode": "yaml", "yaml_path": yaml_path, "written": True, "summary": summary}

    # -------- storage-mode dashboards: the lovelace/config/save WS command --
    # No file surgery and no core stop/start: HA validates, persists to the
    # correct .storage store, and hot-reloads the dashboard itself.
    kwargs: dict[str, Any] = {}
    if url_path is not None:
        kwargs["url_path"] = url_path

    prev, prev_err = await _safe_ws(ctx, "lovelace/config", force=False, **kwargs)
    prev_config = prev if isinstance(prev, dict) else None

    if dry_run:
        out: dict[str, Any] = {
            "dry_run": True,
            "mode": "storage",
            "url_path": url_path,
            "current": _config_summary(prev_config) if prev_config is not None else None,
            "summary": summary,
            "note": "re-run with dry_run=false to save via the lovelace/config/save WS command",
        }
        if prev_config is not None:
            out["would_change"] = {"config": diff_summary(prev_config, config)}
        else:
            out["note"] = (
                f"no existing config to diff ({(prev_err or {}).get('error', 'empty store')}); "
                "dry_run=false would create it via lovelace/config/save"
            )
        return out

    # Pre-image undo artifact + write-ahead journal entry. This save bypasses
    # the StorageEditor spine (deliberately), so journal here to keep every
    # mutation discoverable and reversible.
    undo_id: str | None = None
    if prev_config is not None:
        undo_id = f"{int(time.time())}-{secrets.token_hex(4)}"
        undo_dir = UNDO_ROOT / undo_id
        undo_dir.mkdir(parents=True, exist_ok=True)
        (undo_dir / "dashboard_config.json").write_text(
            json.dumps({"url_path": url_path, "config": prev_config}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    entry_id = StorageEditor._journal(
        {
            "action": "dashboard_ws_save",
            "status": "pending",
            "hint": "dashboard-save",
            "url_path": url_path,
            "undo_id": undo_id,
            "diff": diff_summary(prev_config or {}, config),
            "undo_hint": "to revert, dashboard_config_save the config stored in "
            "undo/<undo_id>/dashboard_config.json",
        }
    )

    _saved, err = await _safe_ws(ctx, "lovelace/config/save", config=config, **kwargs)
    if err is not None:
        StorageEditor._journal_update(entry_id, status="failed", error=err.get("error"))
        return err
    StorageEditor._journal_update(entry_id, status="committed")

    verify, _verify_err = await _safe_ws(ctx, "lovelace/config", force=True, **kwargs)
    return {
        "applied": True,
        "mode": "storage",
        "url_path": url_path,
        "summary": summary,
        "verified": isinstance(verify, dict) and _config_summary(verify) == summary,
        "undo_id": undo_id,
        "journal_id": entry_id,
    }
