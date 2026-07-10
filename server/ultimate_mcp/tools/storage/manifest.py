"""storage/ surface manifest — .storage registry surgery (W2, the killer feature).

Pure data; all mutating tools route through safety/storage_editor.py
(stop -> backup -> atomic-edit -> validate -> start).
"""

from ultimate_mcp.spec import SurfaceSpec, Tier, ToolSpec

_ENTITY_ID = {"type": "string", "pattern": r"^[a-z0-9_]+\.[a-z0-9_]+$"}
_DRY_RUN = {"type": "boolean", "default": True, "description": "Preview only (default true)"}

SURFACE = SurfaceSpec(
    name="storage",
    summary=".storage registry surgery: read/scan/patch HA's internal JSON stores, "
    "deep entity renames, orphan cleanup — all writes via the stop-backup-edit-verify protocol",
    impl_module="ultimate_mcp.tools.storage.impl",
    requires=(),  # .storage always exists on an HA install
    tools=(
        ToolSpec(
            name="storage_read",
            summary="Read any .storage file by key with secrets (tokens/passwords) masked",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "e.g. core.entity_registry"},
                },
                "required": ["key"],
            },
            keywords=("registry", "json", "internal", "hidden", "dotstorage", "read"),
        ),
        ToolSpec(
            name="storage_list",
            summary="Inventory of .storage files: size plus parsed 'key' and 'version'",
            tier=Tier.T0_READ,
            keywords=("inventory", "files", "list", "registries", "stores"),
        ),
        ToolSpec(
            name="storage_orphan_scan",
            summary="Find registry orphans: entities pointing at missing devices, "
            "devices whose config entries are all gone (report only)",
            tier=Tier.T0_READ,
            keywords=("orphan", "stale", "ghost", "dangling", "leftover", "cleanup", "scan"),
        ),
        ToolSpec(
            name="dependency_graph",
            summary="Where is this entity referenced: dashboards, automations, scripts, "
            "scenes, groups, templates — across YAML and .storage",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {"entity_id": _ENTITY_ID},
                "required": ["entity_id"],
            },
            keywords=("references", "used", "where", "impact", "consumers", "depends", "graph"),
        ),
        ToolSpec(
            name="entity_rename_deep",
            summary="Rename an entity_id AND rewrite every reference the dependency "
            "graph found (registry, dashboards, YAML) in one guarded operation",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {
                    "old_entity_id": _ENTITY_ID,
                    "new_entity_id": _ENTITY_ID,
                    "dry_run": _DRY_RUN,
                },
                "required": ["old_entity_id", "new_entity_id"],
            },
            keywords=("rename", "entity_id", "migrate", "bulk", "rewrite", "refactor"),
        ),
        ToolSpec(
            name="storage_orphan_clean",
            summary="Remove the orphans storage_orphan_scan found, via the guarded editor",
            tier=Tier.T2_RISKY,
            schema={"type": "object", "properties": {"dry_run": _DRY_RUN}},
            keywords=("orphan", "clean", "remove", "prune", "ghost", "stale"),
        ),
        ToolSpec(
            name="storage_patch",
            summary="Guarded RFC-6902-style JSON patch (add/replace/remove) on any .storage file",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "e.g. core.config_entries"},
                    "json_patch": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {"type": "string", "enum": ["add", "replace", "remove"]},
                                "path": {"type": "string", "description": "JSON pointer, e.g. /data/entities/0/name"},
                                "value": {},
                            },
                            "required": ["op", "path"],
                        },
                    },
                    "dry_run": _DRY_RUN,
                },
                "required": ["key", "json_patch"],
            },
            keywords=("patch", "edit", "surgery", "json", "pointer", "fix", "modify"),
        ),
    ),
)
