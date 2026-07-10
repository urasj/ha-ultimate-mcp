"""network/ surface manifest — MQTT broker introspection + host networking (W4).

Pure data, imported at startup. The mqtt_* tools are gated on the Mosquitto
add-on and the mqtt service being present in the fingerprint; net_info / wifi_scan
only need the Supervisor network API, so they carry no per-tool gate.
"""

from ultimate_mcp.spec import SurfaceSpec, Tier, ToolSpec

# Gate shared by every broker-touching tool. The realtime/database surfaces gate
# at the surface level, but here two tools (net_info, wifi_scan) do NOT need MQTT,
# so the gate lives on the individual mqtt_* ToolSpecs instead of the surface.
_MQTT_GATE = ("addon:core_mosquitto", "service:mqtt")

_SECONDS = {
    "type": "number",
    "default": 3,
    "minimum": 0.1,
    "maximum": 60,
    "description": "Subscribe window in seconds (timeboxed)",
}
_TOPIC = {"type": "string", "description": "MQTT topic filter, supports + and # wildcards"}

SURFACE = SurfaceSpec(
    name="network",
    summary="MQTT broker introspection ($SYS metrics, discovery audits, retained dumps, "
    "live capture, request/response) plus host network info and Wi-Fi scans",
    impl_module="ultimate_mcp.tools.network.impl",
    requires=(),  # surface always loads; mqtt tools self-gate below
    tools=(
        ToolSpec(
            name="mqtt_broker_stats",
            summary="Subscribe $SYS/# for a few seconds and summarise broker health "
            "(clients, messages, bytes, uptime)",
            tier=Tier.T0_READ,
            requires=_MQTT_GATE,
            schema={
                "type": "object",
                "properties": {"seconds": {**_SECONDS, "default": 3}},
            },
            keywords=("mqtt", "broker", "mosquitto", "sys", "stats", "clients", "uptime", "metrics"),
        ),
        ToolSpec(
            name="mqtt_discovery_audit",
            summary="Scan retained homeassistant/# discovery configs; flag orphaned "
            "tombstones, invalid payloads and duplicate unique_ids",
            tier=Tier.T0_READ,
            requires=_MQTT_GATE,
            schema={
                "type": "object",
                "properties": {"seconds": {**_SECONDS, "default": 3}},
            },
            keywords=("mqtt", "discovery", "homeassistant", "orphan", "duplicate", "audit", "retained"),
        ),
        ToolSpec(
            name="mqtt_retained_dump",
            summary="Collect retained messages under a topic filter for N seconds",
            tier=Tier.T0_READ,
            requires=_MQTT_GATE,
            schema={
                "type": "object",
                "properties": {
                    "topic": {**_TOPIC, "default": "#"},
                    "seconds": {**_SECONDS, "default": 3},
                },
            },
            keywords=("mqtt", "retained", "dump", "topic", "snapshot"),
        ),
        ToolSpec(
            name="mqtt_subscribe_window",
            summary="Capture all live traffic (retained + fresh) on a topic filter for N seconds",
            tier=Tier.T0_READ,
            requires=_MQTT_GATE,
            schema={
                "type": "object",
                "properties": {
                    "topic": {**_TOPIC, "default": "#"},
                    "seconds": {**_SECONDS, "default": 5},
                    "max_messages": {"type": "integer", "default": 500, "maximum": 5000},
                },
                "required": ["topic"],
            },
            keywords=("mqtt", "subscribe", "capture", "sniff", "window", "traffic"),
        ),
        ToolSpec(
            name="net_info",
            summary="Supervisor host network info: interfaces, addresses, gateways, DNS",
            tier=Tier.T0_READ,
            schema={"type": "object", "properties": {}},
            keywords=("network", "interfaces", "ip", "gateway", "dns", "host"),
        ),
        ToolSpec(
            name="wifi_scan",
            summary="Scan Wi-Fi access points visible to the first wireless interface",
            tier=Tier.T0_READ,
            schema={"type": "object", "properties": {}},
            keywords=("wifi", "wireless", "scan", "accesspoints", "ssid", "signal"),
        ),
        ToolSpec(
            name="mqtt_publish_with_response",
            summary="Publish a message then wait on a response topic for a reply (request/response)",
            tier=Tier.T1_REVERSIBLE,
            requires=_MQTT_GATE,
            schema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Topic to publish to"},
                    "payload": {"type": ["string", "null"], "default": None},
                    "response_topic": {**_TOPIC, "description": "Topic filter to await a reply on"},
                    "timeout": {"type": "number", "default": 5, "minimum": 0.1, "maximum": 60},
                    "qos": {"type": "integer", "default": 0, "enum": [0, 1, 2]},
                    "retain": {"type": "boolean", "default": False},
                    "dry_run": {"type": "boolean", "default": True},
                },
                "required": ["topic", "response_topic"],
            },
            keywords=("mqtt", "publish", "request", "response", "rpc", "reply"),
        ),
    ),
)
