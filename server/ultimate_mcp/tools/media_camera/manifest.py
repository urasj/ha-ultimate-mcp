"""media_camera/ surface manifest — cameras, go2rtc, LLM Vision, media (W5).

Pure data. The go2rtc add-on slug varies per install, so the surface itself is
UNGATED; each tool degrades to {"error": ..., "note": ...} when its backend
(go2rtc HTTP API, the camera_proxy image endpoint, the llmvision service, or a
mapped /media path) is unreachable, rather than being hidden by a capability gate.
"""

from ultimate_mcp.spec import SurfaceSpec, Tier, ToolSpec

_ENTITY_ID = {"type": "string", "pattern": r"^camera\.[a-z0-9_]+$"}

SURFACE = SurfaceSpec(
    name="media_camera",
    summary="Cameras, go2rtc streams and LLM Vision: list cameras, plan snapshots, read go2rtc "
    "stream health, analyse a frame with LLM Vision, and inventory /media + /share",
    impl_module="ultimate_mcp.tools.media_camera.impl",
    requires=(),  # slug-dependent backends: gate per-tool via graceful degrade
    tools=(
        ToolSpec(
            name="camera_list",
            summary="List camera entities with state and key attributes",
            tier=Tier.T0_READ,
            keywords=("camera", "list", "cctv", "video", "entities"),
        ),
        ToolSpec(
            name="camera_snapshot",
            summary="Plan/return a still from a camera via camera_proxy (image content)",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {"entity_id": _ENTITY_ID},
                "required": ["entity_id"],
            },
            keywords=("snapshot", "still", "frame", "image", "camera", "proxy", "grab"),
        ),
        ToolSpec(
            name="go2rtc_streams",
            summary="List configured go2rtc streams (go2rtc /api/streams)",
            tier=Tier.T0_READ,
            keywords=("go2rtc", "streams", "webrtc", "rtsp", "restream", "sources"),
        ),
        ToolSpec(
            name="stream_health_report",
            summary="Online/offline health per go2rtc stream (parsed from /api/streams)",
            tier=Tier.T0_READ,
            keywords=("go2rtc", "health", "online", "offline", "producers", "stream", "report"),
        ),
        ToolSpec(
            name="media_index",
            summary="Inventory of the /media and /share directories (top-level)",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "subpath": {
                        "type": ["string", "null"],
                        "default": None,
                        "description": "Optional relative subpath under the media root",
                    }
                },
            },
            keywords=("media", "share", "files", "index", "inventory", "recordings", "clips"),
        ),
        ToolSpec(
            name="llm_vision_analyze",
            summary="Analyse a camera frame with LLM Vision (llmvision.image_analyzer)",
            tier=Tier.T1_REVERSIBLE,
            schema={
                "type": "object",
                "properties": {
                    "entity_id": _ENTITY_ID,
                    "prompt": {"type": "string", "description": "Question for the vision model"},
                    "provider": {
                        "type": ["string", "null"],
                        "default": None,
                        "description": "LLM Vision provider/config entry id (optional)",
                    },
                    "max_tokens": {"type": "integer", "default": 100, "minimum": 1},
                    "dry_run": {"type": "boolean", "default": True,
                                "description": "Preview the service call only (default true)"},
                },
                "required": ["entity_id", "prompt"],
            },
            keywords=("llm", "vision", "analyze", "describe", "detect", "image", "ai", "frame"),
        ),
    ),
)
