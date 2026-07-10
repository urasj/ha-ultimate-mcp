"""assist/ surface implementation — lazy-imported on first call (W5).

Conversation agents are exercised over the REST core API (POST conversation/process
via ctx.supervisor.core_api) because that is the stable, documented entry point;
pipeline and registry inspection go over ctx.ha_ws. Every WS call is wrapped and
degraded to {"error": ..., "note": _WS_NOTE}; the pipeline run is timeboxed with
asyncio.wait_for so it always returns a JSON payload.

API assumptions flagged for review:
  * Conversation agents surface as conversation.* entities in the state machine
    (HA 2024+). We list via get_states filtered to the conversation domain, and
    also try the WS "conversation/agent/info" for the default agent.
  * conversation/process REST payload is {text, agent_id?, language?,
    conversation_id?}; response carries response.speech.plain.speech and
    response.data.{targets,success,failed} — shapes vary across releases, so we
    surface the raw response object too.
  * assist_pipeline/run is really a subscription that streams run events; calling
    it and awaiting the first result is best-effort and timeboxed. Verify the
    command + event shape against 2026.7.
  * Assist exposure lives in entity registry entries under
    options.conversation.should_expose (config/entity_registry/list). Verify.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ultimate_mcp.context import Context

_WS_NOTE = "verify conversation/assist_pipeline WS command against HA 2026.7"


async def _ws(ctx: Context, command: str, **kwargs: Any) -> Any:
    try:
        return await ctx.ha_ws.call(command, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "note": _WS_NOTE}


def _is_err(value: Any) -> bool:
    return isinstance(value, dict) and "error" in value


def _speech(response: Any) -> str | None:
    """Best-effort pull of the spoken text out of a conversation/process response."""
    if not isinstance(response, dict):
        return None
    resp = response.get("response", response)
    try:
        return resp["speech"]["plain"]["speech"]
    except (KeyError, TypeError):
        return None


# ------------------------------------------------------------------ T0
async def conversation_agents(ctx: Context, **_: Any) -> Any:
    """List conversation agents (conversation.* entities) plus default-agent info."""
    try:
        states = await ctx.ha_ws.call("get_states")
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "note": "ws unavailable; cannot enumerate agents"}
    agents = [
        {
            "entity_id": st.get("entity_id"),
            "name": (st.get("attributes") or {}).get("friendly_name"),
            "state": st.get("state"),
        }
        for st in (states or [])
        if isinstance(st.get("entity_id"), str) and st["entity_id"].startswith("conversation.")
    ]
    info = await _ws(ctx, "conversation/agent/info")
    return {
        "agents": agents,
        "count": len(agents),
        "default_agent_info": None if _is_err(info) else info,
        "note": "agents enumerated from conversation.* entities; verify against 2026.7",
    }


async def conversation_test(
    ctx: Context,
    text: str,
    agent_id: str | None = None,
    language: str | None = None,
    conversation_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """POST conversation/process and return the spoken response + raw intent data."""
    body: dict[str, Any] = {"text": text}
    if agent_id is not None:
        body["agent_id"] = agent_id
    if language is not None:
        body["language"] = language
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    try:
        resp = await ctx.supervisor.core_api("POST", "conversation/process", body)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "text": text, "agent_id": agent_id,
                "note": "conversation/process POST failed; verify core API path"}
    return {
        "text": text,
        "agent_id": agent_id,
        "speech": _speech(resp),
        "response": resp,
    }


async def pipeline_list(ctx: Context, **_: Any) -> Any:
    res = await _ws(ctx, "assist_pipeline/pipeline/list")
    if _is_err(res):
        return res
    pipelines = res.get("pipelines") if isinstance(res, dict) else res
    return {
        "pipelines": pipelines,
        "preferred_pipeline": res.get("preferred_pipeline") if isinstance(res, dict) else None,
        "count": len(pipelines) if isinstance(pipelines, list) else None,
    }


async def pipeline_run(
    ctx: Context,
    text: str,
    pipeline_id: str | None = None,
    timeout: float = 30,
    **_: Any,
) -> dict[str, Any]:
    """Run text through an Assist pipeline, timeboxed.

    assist_pipeline/run streams events; we call it and await the initial result
    under asyncio.wait_for. If it never returns we report timed_out rather than
    hang.
    """
    kwargs: dict[str, Any] = {
        "start_stage": "intent",
        "end_stage": "intent",
        "input": {"text": text},
    }
    if pipeline_id is not None:
        kwargs["pipeline"] = pipeline_id

    async def _run() -> Any:
        return await ctx.ha_ws.call("assist_pipeline/run", **kwargs)

    try:
        res = await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        return {"text": text, "pipeline_id": pipeline_id, "timed_out": True,
                "timeout": timeout, "note": _WS_NOTE}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "text": text, "pipeline_id": pipeline_id, "note": _WS_NOTE}
    return {"text": text, "pipeline_id": pipeline_id, "timed_out": False, "result": res}


async def agent_diff(
    ctx: Context,
    text: str,
    agent_id_a: str,
    agent_id_b: str,
    language: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Run one utterance through two agents and diff the spoken responses."""
    a = await conversation_test(ctx, text, agent_id=agent_id_a, language=language)
    b = await conversation_test(ctx, text, agent_id=agent_id_b, language=language)
    speech_a, speech_b = a.get("speech"), b.get("speech")
    return {
        "text": text,
        "agent_a": {"agent_id": agent_id_a, "speech": speech_a, "response": a.get("response"),
                    "error": a.get("error")},
        "agent_b": {"agent_id": agent_id_b, "speech": speech_b, "response": b.get("response"),
                    "error": b.get("error")},
        "identical": speech_a == speech_b,
    }


async def assist_exposure_lint(ctx: Context, domain: str | None = None, **_: Any) -> Any:
    """Categorise registry entities by Assist (conversation) exposure."""
    res = await _ws(ctx, "config/entity_registry/list")
    if _is_err(res):
        return res
    entities = res if isinstance(res, list) else []
    exposed: list[dict[str, Any]] = []
    not_exposed: list[dict[str, Any]] = []
    for e in entities:
        eid = e.get("entity_id")
        if not isinstance(eid, str):
            continue
        if domain and not eid.startswith(f"{domain}."):
            continue
        opts = e.get("options") or {}
        conv = opts.get("conversation") or {}
        should = conv.get("should_expose")
        rec = {"entity_id": eid, "should_expose": should}
        if should:
            exposed.append(rec)
        else:
            not_exposed.append(rec)
    return {
        "domain": domain,
        "counts": {
            "total": len(exposed) + len(not_exposed),
            "exposed": len(exposed),
            "not_exposed": len(not_exposed),
        },
        "exposed": exposed,
        "not_exposed": not_exposed,
        "note": "exposure read from entity_registry options.conversation.should_expose; verify",
    }
