"""Gateway — the umcp_call orchestration around the SafetyKernel gate (0.2.4).

This layer owns everything between the MCP tool boundary and Registry.dispatch:

  * coerces stringified args (some client bridges serialize the nested object)
  * forwards the caller's dry_run flag INTO the tool args (the 0.2.3 bug: only
    dry_run=True was ever forwarded, so impls — which all default dry_run=True —
    re-ran their dry-run branch on apply and T2/T3 could never execute)
  * on T2+/T3 dry-runs, annotates the plan with exactly what the apply needs:
    live checkpoint status, and a freshly minted confirm_token for T3
  * journals every successful T1+ apply (with an undo artifact when the tool
    exposes one) so umcp_journal / umcp_undo cover the whole mutation surface
"""

from __future__ import annotations

import json
from pathlib import Path
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


def _journal_apply(
    ctx: Context,
    safety: SafetyKernel,
    name: str,
    external_checkpoint_ref: str | None,
    result: Any,
) -> Any:
    """Journal a successful apply so umcp_undo can replay the inverse.

    Tools that route through StorageEditor already journal themselves (they
    return a journal_id); for those we only add the external-ref record. For
    everything else we journal here, attaching the tool's own pre-change undo
    copy (undo_id + path, the _guarded_write convention) when it exposes one.
    """
    if not isinstance(result, dict) or "error" in result:
        return result
    if result.get("journal_id"):
        if external_checkpoint_ref:
            safety._journal(
                {
                    "action": "external_checkpoint_ref",
                    "tool": name,
                    "external_checkpoint_ref": external_checkpoint_ref,
                    "linked_journal_id": result["journal_id"],
                }
            )
        return result

    meta: dict[str, Any] = {"tool": name}
    if external_checkpoint_ref:
        meta["external_checkpoint_ref"] = external_checkpoint_ref
    if result.get("undo_id"):
        meta["undo_id"] = result["undo_id"]

    target = ""
    undo_artifact: Path | None = None
    rel = result.get("path")
    if isinstance(rel, str) and rel:
        try:
            target = str(ctx.fs.resolve(rel))
        except Exception:  # noqa: BLE001 — journaling must never fail an apply
            target = rel
        if result.get("undo_id"):
            candidate = UNDO_ROOT / str(result["undo_id"]) / rel.replace("/", "__")
            if candidate.exists():
                undo_artifact = candidate

    result["journal_id"] = safety.record(
        name, target, undo_artifact_path=undo_artifact, **meta
    )
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
    result = await registry.dispatch(ctx, name, args)

    tier = registry.tools[name].spec.tier
    if dry_run:
        if tier >= Tier.T2_RISKY:
            result = _annotate_dry_run(
                safety, name, tier, args_hash, external_checkpoint_ref, result
            )
        return result
    if tier >= Tier.T1_REVERSIBLE:
        result = _journal_apply(ctx, safety, name, external_checkpoint_ref, result)
    return result
