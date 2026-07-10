"""zigbee/ surface manifest — ZHA deep ops via the core WebSocket API (W4b).

Pure data; every tool routes through ctx.ha_ws.call(<zha/* command>). Surface is
gated on integration:zha so it stays invisible on non-ZHA installs. The exact
2026.7 ZHA WS command spellings are not exhaustively documented — impl.py wraps
every call and degrades to {"error": ..., "note": "verify zha/* WS command name
for 2026.7"} rather than raising.
"""

from ultimate_mcp.spec import SurfaceSpec, Tier, ToolSpec

_IEEE = {
    "type": "string",
    "description": "Device IEEE address, e.g. 00:0d:6f:00:0a:bc:de:f0",
}
_DRY_RUN = {"type": "boolean", "default": True, "description": "Preview only (default true)"}

_CLUSTER_TARGET = {
    "ieee": _IEEE,
    "endpoint_id": {"type": "integer", "description": "Zigbee endpoint id, e.g. 1"},
    "cluster_id": {"type": "integer", "description": "Cluster id (decimal), e.g. 6 (On/Off)"},
    "cluster_type": {
        "type": "string",
        "enum": ["in", "out"],
        "default": "in",
        "description": "Server (in) or client (out) cluster",
    },
}

SURFACE = SurfaceSpec(
    name="zigbee",
    summary="ZHA deep ops: device inventory, neighbor/link-quality topology, cluster "
    "attribute read/write, bindings, reconfigure, and coordinator NVM backup",
    impl_module="ultimate_mcp.tools.zigbee.impl",
    requires=("integration:zha",),
    tools=(
        # ------------------------------------------------------------- T0 reads
        ToolSpec(
            name="zha_devices",
            summary="List all ZHA devices with ieee, nwk, lqi, rssi, last_seen, power source",
            tier=Tier.T0_READ,
            keywords=("zigbee", "devices", "inventory", "lqi", "rssi", "nwk", "list"),
        ),
        ToolSpec(
            name="zha_device_detail",
            summary="Full detail for one ZHA device: endpoints, clusters, signature, quirks",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {"ieee": _IEEE},
                "required": ["ieee"],
            },
            keywords=("zigbee", "device", "detail", "endpoints", "clusters", "quirk", "signature"),
        ),
        ToolSpec(
            name="zha_topology_graph",
            summary="Zigbee mesh map: neighbor tables + link quality edges (from zigbee.db "
            "route/neighbor tables if reachable, else derived from device neighbors)",
            tier=Tier.T0_READ,
            keywords=("zigbee", "topology", "mesh", "map", "neighbors", "routes", "lqi", "graph"),
        ),
        ToolSpec(
            name="zha_cluster_read",
            summary="Read a single cluster attribute value from a device endpoint",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    **_CLUSTER_TARGET,
                    "attribute": {
                        "type": ["integer", "string"],
                        "description": "Attribute id (decimal) or name, e.g. 0 / 'on_off'",
                    },
                    "manufacturer": {
                        "type": ["integer", "null"],
                        "default": None,
                        "description": "Manufacturer code for MSP attributes (optional)",
                    },
                },
                "required": ["ieee", "endpoint_id", "cluster_id", "attribute"],
            },
            keywords=("zigbee", "cluster", "attribute", "read", "value", "zcl"),
        ),
        ToolSpec(
            name="zha_bindings_list",
            summary="List a device's Zigbee bindings (source cluster -> target device/group)",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {"ieee": _IEEE},
                "required": ["ieee"],
            },
            keywords=("zigbee", "bindings", "bind", "list", "groups", "cluster"),
        ),
        ToolSpec(
            name="zha_network_settings",
            summary="ZHA network settings: coordinator ieee, pan_id, channel, radio type, key info",
            tier=Tier.T0_READ,
            keywords=("zigbee", "network", "settings", "pan", "channel", "coordinator", "radio"),
        ),
        # --------------------------------------------------------- T1 reversible
        ToolSpec(
            name="zha_cluster_write",
            summary="Write a cluster attribute value (incl. manufacturer-specific); "
            "dry_run returns the intended write without touching the device",
            tier=Tier.T1_REVERSIBLE,
            schema={
                "type": "object",
                "properties": {
                    **_CLUSTER_TARGET,
                    "attribute": {
                        "type": ["integer", "string"],
                        "description": "Attribute id (decimal) or name",
                    },
                    "value": {"description": "New value to write"},
                    "manufacturer": {
                        "type": ["integer", "null"],
                        "default": None,
                        "description": "Manufacturer code for MSP attributes (optional)",
                    },
                    "dry_run": _DRY_RUN,
                },
                "required": ["ieee", "endpoint_id", "cluster_id", "attribute", "value"],
            },
            keywords=("zigbee", "cluster", "attribute", "write", "set", "manufacturer", "zcl"),
        ),
        ToolSpec(
            name="zha_reconfigure",
            summary="Re-run device configuration (re-bind + attribute reporting) for one device",
            tier=Tier.T1_REVERSIBLE,
            schema={
                "type": "object",
                "properties": {"ieee": _IEEE, "dry_run": _DRY_RUN},
                "required": ["ieee"],
            },
            keywords=("zigbee", "reconfigure", "repair", "reporting", "rebind", "fix"),
        ),
        ToolSpec(
            name="zha_bind",
            summary="Create a Zigbee binding between two devices (source -> target)",
            tier=Tier.T1_REVERSIBLE,
            schema={
                "type": "object",
                "properties": {
                    "source_ieee": _IEEE,
                    "target_ieee": _IEEE,
                    "dry_run": _DRY_RUN,
                },
                "required": ["source_ieee", "target_ieee"],
            },
            keywords=("zigbee", "bind", "binding", "link", "group", "create"),
        ),
        ToolSpec(
            name="zha_unbind",
            summary="Remove a Zigbee binding between two devices (source -> target)",
            tier=Tier.T1_REVERSIBLE,
            schema={
                "type": "object",
                "properties": {
                    "source_ieee": _IEEE,
                    "target_ieee": _IEEE,
                    "dry_run": _DRY_RUN,
                },
                "required": ["source_ieee", "target_ieee"],
            },
            keywords=("zigbee", "unbind", "binding", "remove", "unlink"),
        ),
        # --------------------------------------------------------------- T2 risky
        ToolSpec(
            name="zha_coordinator_backup",
            summary="Create a coordinator NVM/network backup (network keys, PAN, device table) "
            "for radio migration / disaster recovery; dry_run notes it will create a backup",
            tier=Tier.T2_RISKY,
            schema={"type": "object", "properties": {"dry_run": _DRY_RUN}},
            keywords=("zigbee", "coordinator", "backup", "nvm", "network", "migrate", "restore"),
        ),
    ),
)
