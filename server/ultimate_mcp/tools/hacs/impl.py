"""hacs/ surface implementation — lazy-imported on first call (W1).

HACS persists its repository catalog in .storage. The key/layout has changed
across HACS versions, so we probe both known keys and normalise the two common
shapes, degrading to {"error": ...} rather than raising when neither is present.

Shapes handled:
  hacs.repositories -> {"data": {"<id>": {<repo fields>}}}
  hacs.data         -> {"data": {"repositories": {"<id>": {"data": {<repo fields>}}}}}
"""

from __future__ import annotations

from typing import Any

from ultimate_mcp.context import Context

_STORAGE_KEYS = ("hacs.repositories", "hacs.data")


def _load_repos(ctx: Context) -> tuple[str | None, dict[str, Any]]:
    """Return (storage_key, {repo_id: repo_dict}) or (None, {}) if unreadable."""
    for key in _STORAGE_KEYS:
        try:
            doc = ctx.fs.read_storage(key)
        except Exception:  # noqa: BLE001 — file may not exist for this key
            continue
        if not isinstance(doc, dict):
            continue
        data = doc.get("data", doc)
        if not isinstance(data, dict):
            continue
        # hacs.data nests the catalog under "repositories"; hacs.repositories is the map itself.
        repos = data.get("repositories", data)
        if isinstance(repos, dict):
            return key, repos
    return None, {}


def _norm(repo_id: str, repo: Any) -> dict[str, Any]:
    """Normalise one repo entry across HACS layouts."""
    r = repo.get("data", repo) if isinstance(repo, dict) else {}
    if not isinstance(r, dict):
        r = {}
    return {
        "id": repo_id,
        "name": r.get("full_name") or r.get("name"),
        "category": r.get("category"),
        "installed": bool(r.get("installed")),
        "installed_version": r.get("installed_version") or r.get("version_installed"),
        "available_version": r.get("available_version") or r.get("last_version"),
    }


async def hacs_inventory(ctx: Context, **_: Any) -> dict[str, Any]:
    key, repos = _load_repos(ctx)
    if key is None:
        return {"error": "no readable HACS storage (tried hacs.repositories, hacs.data)"}
    installed = [_norm(rid, r) for rid, r in repos.items()]
    installed = [r for r in installed if r["installed"]]
    categories: dict[str, int] = {}
    for r in installed:
        cat = r["category"] or "unknown"
        categories[cat] = categories.get(cat, 0) + 1
    return {
        "storage_key": key,
        "installed_count": len(installed),
        "by_category": categories,
        "repositories": installed,
    }


async def hacs_pending_updates(ctx: Context, **_: Any) -> dict[str, Any]:
    key, repos = _load_repos(ctx)
    if key is None:
        return {"error": "no readable HACS storage (tried hacs.repositories, hacs.data)"}
    pending = []
    for rid, r in repos.items():
        norm = _norm(rid, r)
        if not norm["installed"]:
            continue
        inst, avail = norm["installed_version"], norm["available_version"]
        if avail and inst and inst != avail:
            pending.append(norm)
    return {"storage_key": key, "pending_count": len(pending), "pending": pending}
