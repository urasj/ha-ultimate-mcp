"""dashboards/ surface manifest — Lovelace dashboards (W5).

Pure data; loaded at startup. Reads use the lovelace/* WS commands. The single
write tool (dashboard_config_save) saves storage-mode dashboards via the
lovelace/config/save WS command (journaled, with a pre-image undo artifact) and
YAML-mode dashboards through an atomic file write.
No gates: Lovelace is always present.
"""

from ultimate_mcp.spec import SurfaceSpec, Tier, ToolSpec

_DRY_RUN = {"type": "boolean", "default": True, "description": "Preview only (default true)"}
_URL_PATH = {
    "type": ["string", "null"],
    "default": None,
    "description": "Dashboard url_path (None = the default/overview dashboard)",
}

SURFACE = SurfaceSpec(
    name="dashboards",
    summary="Lovelace dashboards: list dashboards, fetch a dashboard's config, list resources, "
    "lint card structure, and save a dashboard config via the lovelace/config/save WS command",
    impl_module="ultimate_mcp.tools.dashboards.impl",
    requires=(),  # Lovelace is always present
    tools=(
        # ----------------------------------------------------------- T0 reads
        ToolSpec(
            name="dashboard_list",
            summary="List all Lovelace dashboards (url_path, title, mode, icon, require_admin)",
            tier=Tier.T0_READ,
            keywords=("dashboards", "lovelace", "list", "panels", "views"),
        ),
        ToolSpec(
            name="dashboard_get_config",
            summary="Fetch the full config for one dashboard (views + cards)",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {"url_path": _URL_PATH},
            },
            keywords=("dashboard", "config", "get", "views", "cards", "lovelace"),
        ),
        ToolSpec(
            name="dashboard_resources",
            summary="List registered Lovelace resources (custom cards: JS/CSS module URLs)",
            tier=Tier.T0_READ,
            keywords=("resources", "custom", "cards", "modules", "js", "frontend"),
        ),
        # ------------------------------------------------------------- lint
        ToolSpec(
            name="dashboard_card_lint",
            summary="Fetch a dashboard config and heuristically validate its card structure "
            "(missing type, empty entities, unknown keys) -> warnings",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {"url_path": _URL_PATH},
            },
            keywords=("lint", "validate", "cards", "warnings", "check", "dashboard"),
        ),
        # --------------------------------------------------------- T2 save
        ToolSpec(
            name="dashboard_config_save",
            summary="Save a full dashboard config. Storage-mode via the lovelace/config/save "
            "WS command (journaled, pre-image undo artifact, no core restart); YAML-mode via "
            "atomic file write. dry_run shows a diff against the live config.",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {
                    "config": {"type": "object", "description": "Full Lovelace config ({views: [...]})"},
                    "url_path": _URL_PATH,
                    "mode": {
                        "type": "string",
                        "enum": ["storage", "yaml"],
                        "default": "storage",
                        "description": "storage -> lovelace/config/save WS command; "
                        "yaml -> atomic write of yaml_path",
                    },
                    "yaml_path": {
                        "type": ["string", "null"],
                        "default": None,
                        "description": "Config-relative YAML file to write when mode=yaml",
                    },
                    "dry_run": _DRY_RUN,
                },
                "required": ["config"],
            },
            keywords=("dashboard", "save", "config", "write", "lovelace", "update"),
        ),
    ),
)
