"""realtime/ surface manifest — the cross-time tier (W4).

Pure data. These tools block for a bounded window on the HA WebSocket event bus
(ctx.ha_ws) or poll Supervisor log endpoints. No surface-level gate; only
state_flatline_scan needs the recorder DB, so it carries a per-tool gate.
"""

from ultimate_mcp.spec import SurfaceSpec, Tier, ToolSpec

_ENTITY_ID = {"type": "string", "pattern": r"^[a-z0-9_]+\.[a-z0-9_]+$"}

SURFACE = SurfaceSpec(
    name="realtime",
    summary="Cross-time tier: block for state changes, capture the event bus, tail logs, "
    "arm automation traces, and scan for dead (flatlined) sensors",
    impl_module="ultimate_mcp.tools.realtime.impl",
    requires=(),
    tools=(
        ToolSpec(
            name="wait_for_state",
            summary="Block until an entity reaches a target state (or timeout)",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "entity_id": _ENTITY_ID,
                    "to_state": {"type": "string", "description": "State to wait for, e.g. 'on'"},
                    "timeout": {"type": "number", "default": 30, "minimum": 0.1, "maximum": 300},
                },
                "required": ["entity_id", "to_state"],
            },
            keywords=("wait", "state", "block", "until", "change", "watch"),
        ),
        ToolSpec(
            name="event_window_capture",
            summary="Subscribe to the event bus and return every event over N seconds",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "event_type": {
                        "type": ["string", "null"],
                        "default": None,
                        "description": "Event type filter (null = all events)",
                    },
                    "seconds": {"type": "number", "default": 5, "minimum": 0.1, "maximum": 120},
                    "max_events": {"type": "integer", "default": 1000, "maximum": 10000},
                },
            },
            keywords=("events", "capture", "bus", "subscribe", "window", "record"),
        ),
        ToolSpec(
            name="log_follow",
            summary="Tail core / supervisor / add-on logs for N seconds or until a regex matches",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "default": "core",
                        "description": "'core', 'supervisor', or an add-on slug",
                    },
                    "seconds": {"type": "number", "default": 5, "minimum": 0.1, "maximum": 120},
                    "until_match": {
                        "type": ["string", "null"],
                        "default": None,
                        "description": "Stop early when this regex matches a new log line",
                    },
                    "poll_interval": {"type": "number", "default": 1, "minimum": 0.1, "maximum": 10},
                    "tail_lines": {"type": "integer", "default": 200, "maximum": 5000},
                },
            },
            keywords=("logs", "tail", "follow", "journal", "addon", "supervisor", "core", "grep"),
        ),
        ToolSpec(
            name="state_flatline_scan",
            summary="Recorder entities whose most recent state row is older than a threshold "
            "(dead / stuck sensors)",
            tier=Tier.T0_READ,
            requires=("db:sqlite",),
            schema={
                "type": "object",
                "properties": {
                    "threshold_hours": {"type": "number", "default": 24, "minimum": 0.1},
                    "top": {"type": "integer", "default": 100, "maximum": 1000},
                },
            },
            keywords=("flatline", "dead", "stale", "stuck", "sensor", "stopped", "updating", "silent"),
        ),
        ToolSpec(
            name="trace_next_run",
            summary="Arm on an automation, wait for its next trigger, and return the full trace",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "entity_id": _ENTITY_ID,
                    "timeout": {"type": "number", "default": 60, "minimum": 0.1, "maximum": 600},
                },
                "required": ["entity_id"],
            },
            keywords=("automation", "trace", "arm", "next", "trigger", "debug", "fire"),
        ),
    ),
)
