"""Gateway — the umcp_call orchestration around the SafetyKernel gate (0.2.4).

This layer owns everything between the MCP tool boundary and Registry.dispatch:

  * coerces stringified args (some client bridges serialize the nested object)
  * forwards the caller's dry_run flag INTO the tool args (the 0.2.3 bug: only
    dry_run=True was ever forwarded, so impls — which all default dry_run=True —
    re-ran their dry-run branch on apply and T2/T3 could never execute)
  * on T2+/T3 dry-runs, annotates the plan with exactly what the apply needs:
    live checkpoint status, and a freshly minted confirm_token for T3
  * write-ahead journals every T1+ apply (0.2.5): a pending entry is appended
    BEFORE dispatch and resolved to committed / failed / no_op / superseded
    after, with an undo artifact attached when the tool exposes one — so
    umcp_journal / umcp_undo cover the whole mutation surface even when a
    dispatch dies mid-flight
"""

from __future__ import annotations

import json
from typing import Any

from ultimate_mcp.context import DATA_DIR, Context
from ultimate_mcp.registry import Registry
from ultimate_mcp.safety.kernel import TOKEN_TTL_SECONDS, SafetyKernel, canonical_args_hash
from ultimate_mcp.spec import Tier

UNDO_ROOT = DATA_DIR / "undo"


def _coerce_args(args: dict[str, Any] | str | None) -> dict[str, Any]:
    if isinstance(args, str):
        s = args.strip()
        args = json.loads(s) if s else {}
    args = dict(args or {})
    if not isinstance(args, dict):
        raise ValueError(f"args must be an object, got {type(args).__name__}")
    return args


def _annotate_dry_run(
    safety: SafetyKernel,
    name: str,
    tier: Tier,
    args_hash: str,
    external_checkpoint_ref: str | None,
    result: Any,
) -> Any:
    """Tell the caller exactly what the apply will require (spec §3 'Both')."""
    if not isinstance(result, dict):
        result = {"result": result}
    status = safety.checkpoint_status(external_checkpoint_ref)
    result["checkpoint"] = status
    if status["satisfied"]:
        result.pop("checkpoint_required", None)
    else:
        result["checkpoint_required"] = safety.checkpoint_remediation()
    if tier == Tier.T3_DESTRUCTIVE:
        result["confirm_token"] = safety.mint_token(name, args_hash)
        result["confirm_token_ttl_seconds"] = TOKEN_TTL_SECONDS
        result["apply_with"] = "re-call with dry_run=false and this confirm_token (single-use)"
    return result


def _finalize_apply(
    ctx: Context,
    safety: SafetyKernel,
    name: str,
    pending_id: str,
    external_checkpoint_ref: str | None,
    result: Any,
) -> Any:
    """Resolve the write-ahead entry after a dispatch that returned.

    Tools that route through StorageEditor journal themselves write-ahead (the
    result carries their journal_id) — our gateway entry is then superseded.
    For everything else the gateway entry IS the record: mark it committed /
    failed / no_op, attaching the tool's pre-change undo copy (undo_id + path,
    the _guarded_write convention) when it exposes one.
    """
    if not isinstance(result, dict):
        safety.journal_update(pending_id, status="committed", result=str(result)[:200])
        return result

    if result.get("journal_id") and result["journal_id"] != pending_id:
        safety.journal_update(
            pending_id, status="superseded", superseded_by=result["journal_id"]
        )
        if external_checkpoint_ref:
            safety.journal_update(
                result["journal_id"], external_checkpoint_ref=external_checkpoint_ref
            )
        return result

    result["journal_id"] = pending_id
    if result.get("error") or result.get("executed") is False:
        safety.journal_update(
            pending_id,
            status="failed",
            error=str(result.get("error") or result.get("result") or "executed=false")[:300],
        )
        return result
    if result.get("no_op"):
        safety.journal_update(pending_id, status="no_op")
        return result

    updates: dict[str, Any] = {"status": "committed"}
    if result.get("undo_id"):
        updates["undo_id"] = result["undo_id"]
    rel = result.get("path")
    if isinstance(rel, str) and rel:
        try:
            target = str(ctx.fs.resolve(rel))
        except Exception:  # noqa: BLE001 — journaling must never fail an apply
            target = rel
        updates["target"] = target
        if result.get("undo_id"):
            candidate = UNDO_ROOT / str(result["undo_id"]) / rel.replace("/", "__")
            if candidate.exists():
                safety.attach_undo_artifact(pending_id, candidate, target)
    safety.journal_update(pending_id, **updates)
    return result


async def call_tool(
    ctx: Context,
    registry: Registry,
    safety: SafetyKernel,
    name: str,
    args: dict[str, Any] | str | None = None,
    dry_run: bool = True,
    confirm_token: str | None = None,
    external_checkpoint_ref: str | None = None,
) -> Any:
    args = _coerce_args(args)
    args_hash = canonical_args_hash(args)
    await safety.authorize(
        registry, name, dry_run, confirm_token, external_checkpoint_ref, args_hash=args_hash
    )
    args["dry_run"] = dry_run  # the caller's flag always wins over impl defaults
    tier = registry.tools[name].spec.tier

    if dry_run or tier < Tier.T1_REVERSIBLE:
        result = await registry.dispatch(ctx, name, args)
        if dry_run and tier >= Tier.T2_RISKY:
            result = _annotate_dry_run(
                safety, name, tier, args_hash, external_checkpoint_ref, result
            )
        return result

    # T1+ apply: write-ahead journal entry BEFORE the mutation, for every
    # mutating path — a crash mid-dispatch must leave a discoverable record.
    meta: dict[str, Any] = {"tool": name, "args_hash": args_hash}
    if external_checkpoint_ref:
        meta["external_checkpoint_ref"] = external_checkpoint_ref
    pending_id = safety.journal_open(name, **meta)
    try:
        result = await registry.dispatch(ctx, name, args)
    except Exception as exc:
        safety.journal_update(pending_id, status="unknown", error=str(exc))
        raise RuntimeError(
            f"{name} apply raised {exc!r} — mutation state UNKNOWN. Write-ahead "
            f"journal entry {pending_id} recorded (status unknown); inspect state "
            f"with a read tool / umcp_journal before retrying."
        ) from exc
    return _finalize_apply(ctx, safety, name, pending_id, external_checkpoint_ref, result)
