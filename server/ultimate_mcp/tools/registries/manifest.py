"""registries/ surface manifest — entity/device/area/floor/label/category registries (W5).

Pure data; loaded at startup. Reads go through the core WS config/*_registry/*
commands; writes are reversible registry updates (T1) or removals (T2).
The registries always exist on an HA install, so the surface has no gates.
"""

from ultimate_mcp.spec import SurfaceSpec, Tier, ToolSpec

_ENTITY_ID = {"type": "string", "description": "e.g. light.kitchen"}
_DRY_RUN = {"type": "boolean", "default": True, "description": "Preview only (default true)"}

SURFACE = SurfaceSpec(
    name="registries",
    summary="Entity, device, area, floor, label and category registries: list/inspect, "
    "rename and re-home entities, manage areas/labels, control Assist exposure, remove stale rows",
    impl_module="ultimate_mcp.tools.registries.impl",
    requires=(),  # the registries always exist on an HA install
    tools=(
        # ----------------------------------------------------------- T0 reads
        ToolSpec(
            name="entity_list",
            summary="List every row in the entity registry (entity_id, name, area, labels, hidden/disabled)",
            tier=Tier.T0_READ,
            keywords=("entities", "registry", "list", "inventory", "all"),
        ),
        ToolSpec(
            name="entity_get",
            summary="Full entity-registry record for one entity_id",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {"entity_id": _ENTITY_ID},
                "required": ["entity_id"],
            },
            keywords=("entity", "get", "detail", "record", "inspect"),
        ),
        ToolSpec(
            name="device_list",
            summary="List every device in the device registry (id, name, manufacturer, area, config entries)",
            tier=Tier.T0_READ,
            keywords=("devices", "registry", "list", "hardware", "inventory"),
        ),
        ToolSpec(
            name="area_list",
            summary="List all areas (area_id, name, floor, labels, aliases)",
            tier=Tier.T0_READ,
            keywords=("areas", "rooms", "list", "zones"),
        ),
        ToolSpec(
            name="floor_list",
            summary="List all floors (floor_id, name, level, aliases)",
            tier=Tier.T0_READ,
            keywords=("floors", "levels", "list", "storeys"),
        ),
        ToolSpec(
            name="label_list",
            summary="List all labels (label_id, name, color, icon)",
            tier=Tier.T0_READ,
            keywords=("labels", "tags", "list"),
        ),
        ToolSpec(
            name="category_list",
            summary="List categories for a scope (e.g. automation, scene) from the category registry",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "default": "automation",
                        "description": "Category scope, e.g. automation, scene, script",
                    }
                },
            },
            keywords=("categories", "list", "scope", "organize"),
        ),
        # ----------------------------------------------------- T1 reversible
        ToolSpec(
            name="entity_update",
            summary="Update an entity-registry row (name, area_id, labels, hidden, disabled, icon, "
            "new_entity_id) — reversible; dry_run shows the update payload",
            tier=Tier.T1_REVERSIBLE,
            schema={
                "type": "object",
                "properties": {
                    "entity_id": _ENTITY_ID,
                    "updates": {
                        "type": "object",
                        "description": "Fields to change, e.g. {name, area_id, labels, "
                        "hidden, disabled_by, icon, new_entity_id}",
                    },
                    "dry_run": _DRY_RUN,
                },
                "required": ["entity_id", "updates"],
            },
            keywords=("entity", "update", "rename", "area", "label", "hide", "assign"),
        ),
        ToolSpec(
            name="device_update",
            summary="Update a device-registry row (name_by_user, area_id, labels, disabled_by) — reversible",
            tier=Tier.T1_REVERSIBLE,
            schema={
                "type": "object",
                "properties": {
                    "device_id": {"type": "string"},
                    "updates": {"type": "object", "description": "e.g. {name_by_user, area_id, labels}"},
                    "dry_run": _DRY_RUN,
                },
                "required": ["device_id", "updates"],
            },
            keywords=("device", "update", "rename", "area", "assign"),
        ),
        ToolSpec(
            name="area_create",
            summary="Create a new area (name, optional floor_id, labels, icon)",
            tier=Tier.T1_REVERSIBLE,
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "floor_id": {"type": ["string", "null"], "default": None},
                    "labels": {"type": "array", "items": {"type": "string"}, "default": []},
                    "icon": {"type": ["string", "null"], "default": None},
                    "dry_run": _DRY_RUN,
                },
                "required": ["name"],
            },
            keywords=("area", "create", "room", "add", "new"),
        ),
        ToolSpec(
            name="area_update",
            summary="Update an existing area (name, floor_id, labels, icon, aliases)",
            tier=Tier.T1_REVERSIBLE,
            schema={
                "type": "object",
                "properties": {
                    "area_id": {"type": "string"},
                    "updates": {"type": "object"},
                    "dry_run": _DRY_RUN,
                },
                "required": ["area_id", "updates"],
            },
            keywords=("area", "update", "rename", "room", "floor"),
        ),
        ToolSpec(
            name="label_create",
            summary="Create a new label (name, optional color, icon, description)",
            tier=Tier.T1_REVERSIBLE,
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "color": {"type": ["string", "null"], "default": None},
                    "icon": {"type": ["string", "null"], "default": None},
                    "description": {"type": ["string", "null"], "default": None},
                    "dry_run": _DRY_RUN,
                },
                "required": ["name"],
            },
            keywords=("label", "create", "tag", "add", "new"),
        ),
        ToolSpec(
            name="entity_expose_assist",
            summary="Set Assist (voice) exposure for entities across one or more assistants",
            tier=Tier.T1_REVERSIBLE,
            schema={
                "type": "object",
                "properties": {
                    "entity_ids": {"type": "array", "items": {"type": "string"}},
                    "should_expose": {"type": "boolean"},
                    "assistants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": ["conversation"],
                        "description": "Assistant ids, e.g. conversation, cloud.alexa, cloud.google_assistant",
                    },
                    "dry_run": _DRY_RUN,
                },
                "required": ["entity_ids", "should_expose"],
            },
            keywords=("assist", "expose", "voice", "alexa", "google", "conversation", "exposure"),
        ),
        # ------------------------------------------------------- T2 removals
        ToolSpec(
            name="entity_remove",
            summary="Remove an entity from the registry (only works for entities the integration "
            "no longer provides) — destructive; dry_run previews",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {"entity_id": _ENTITY_ID, "dry_run": _DRY_RUN},
                "required": ["entity_id"],
            },
            keywords=("entity", "remove", "delete", "purge", "stale"),
        ),
        ToolSpec(
            name="device_remove",
            summary="Remove a device from the registry / detach it from a config entry — destructive",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {
                    "device_id": {"type": "string"},
                    "config_entry_id": {
                        "type": ["string", "null"],
                        "default": None,
                        "description": "If set, detach the device from this config entry instead of a hard remove",
                    },
                    "dry_run": _DRY_RUN,
                },
                "required": ["device_id"],
            },
            keywords=("device", "remove", "delete", "detach", "purge"),
        ),
    ),
)
