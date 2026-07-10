"""filesystem/ surface manifest — guarded config-tree access (W5).

Pure data; loaded at startup. Reads and writes go through ctx.fs (rooted at the
HA config mount, path-escape guarded). Writes are atomic (tmp + os.replace) and
keep an undo copy. Backup tars are read-only (Python tarfile). No gates: the
config filesystem always exists.
"""

from ultimate_mcp.spec import SurfaceSpec, Tier, ToolSpec

_REL = {"type": "string", "description": "Path relative to the HA config root"}
_DRY_RUN = {"type": "boolean", "default": True, "description": "Preview only (default true)"}

SURFACE = SurfaceSpec(
    name="filesystem",
    summary="Config filesystem: read/tree/grep, YAML lint, secrets audit, custom-component "
    "inventory, backup-tar inspection, and guarded www/theme/YAML writes + component scaffolding",
    impl_module="ultimate_mcp.tools.filesystem.impl",
    requires=(),  # the config filesystem always exists
    tools=(
        # ----------------------------------------------------------- T0 reads
        ToolSpec(
            name="fs_read",
            summary="Read a config file (secrets.yaml values are masked)",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {"path": _REL},
                "required": ["path"],
            },
            keywords=("read", "cat", "file", "view", "config", "yaml"),
        ),
        ToolSpec(
            name="fs_tree",
            summary="Directory listing under the config root (files, sizes, dirs)",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "", "description": "Subdir (default: root)"},
                    "max_depth": {"type": "integer", "default": 2, "minimum": 1, "maximum": 8},
                },
            },
            keywords=("tree", "ls", "dir", "listing", "files", "structure"),
        ),
        ToolSpec(
            name="fs_grep",
            summary="Search a regex/substring across config YAML files; returns file+line matches",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "regex": {"type": "boolean", "default": False},
                    "max_matches": {"type": "integer", "default": 200, "maximum": 2000},
                },
                "required": ["pattern"],
            },
            keywords=("grep", "search", "find", "pattern", "ripgrep"),
        ),
        ToolSpec(
            name="yaml_lint",
            summary="Parse a YAML file with pyyaml and report syntax errors (line/column)",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {"path": _REL},
                "required": ["path"],
            },
            keywords=("yaml", "lint", "validate", "parse", "syntax", "check"),
        ),
        ToolSpec(
            name="secrets_audit",
            summary="Cross-reference !secret references against secrets.yaml: unused keys + missing refs",
            tier=Tier.T0_READ,
            keywords=("secrets", "audit", "unused", "missing", "!secret", "hygiene"),
        ),
        ToolSpec(
            name="custom_component_inventory",
            summary="List custom_components with each manifest.json (domain, version, requirements, codeowners)",
            tier=Tier.T0_READ,
            keywords=("custom", "components", "hacs", "manifest", "integrations", "inventory"),
        ),
        ToolSpec(
            name="backup_tar_list",
            summary="List the files inside a /backup/*.tar (read-only, via Python tarfile)",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "tar_path": {"type": "string", "description": "Path to a .tar under /backup (rel to config root)"},
                    "max_entries": {"type": "integer", "default": 500, "maximum": 5000},
                },
                "required": ["tar_path"],
            },
            keywords=("backup", "tar", "list", "archive", "contents"),
        ),
        ToolSpec(
            name="backup_tar_diff",
            summary="Diff a config file inside a backup tar against the current on-disk copy",
            tier=Tier.T0_READ,
            schema={
                "type": "object",
                "properties": {
                    "tar_path": {"type": "string"},
                    "member": {"type": "string", "description": "Path of the file inside the tar"},
                    "current_path": {
                        "type": ["string", "null"],
                        "default": None,
                        "description": "Config-relative path to compare against (defaults to member basename)",
                    },
                },
                "required": ["tar_path", "member"],
            },
            keywords=("backup", "diff", "compare", "restore", "changed", "history"),
        ),
        # ----------------------------------------------------- T1 reversible
        ToolSpec(
            name="fs_write_www",
            summary="Write a file under config/www (served at /local/...) — atomic, keeps an undo copy",
            tier=Tier.T1_REVERSIBLE,
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path under www/, e.g. dashboard/app.js"},
                    "content": {"type": "string"},
                    "dry_run": _DRY_RUN,
                },
                "required": ["path", "content"],
            },
            keywords=("www", "local", "deploy", "write", "static", "serve"),
        ),
        ToolSpec(
            name="theme_write",
            summary="Write a theme YAML file under config/themes/ — atomic, keeps an undo copy",
            tier=Tier.T1_REVERSIBLE,
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Theme file name (without .yaml)"},
                    "content": {"type": "string", "description": "Theme YAML body"},
                    "dry_run": _DRY_RUN,
                },
                "required": ["name", "content"],
            },
            keywords=("theme", "themes", "frontend", "css", "colors", "write"),
        ),
        # --------------------------------------------------------- T2 risky
        ToolSpec(
            name="yaml_edit_any",
            summary="Edit ANY config YAML (incl. recorder:/http:/logger:) — validates as YAML, "
            "atomic write with an undo copy; dry_run shows a unified diff",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {
                    "path": _REL,
                    "content": {"type": "string", "description": "Full new file content"},
                    "dry_run": _DRY_RUN,
                },
                "required": ["path", "content"],
            },
            keywords=("yaml", "edit", "configuration", "recorder", "http", "logger", "write", "any"),
        ),
        ToolSpec(
            name="custom_component_scaffold",
            summary="Create a minimal custom_components/<domain>/ (manifest.json + __init__.py); "
            "dry_run lists the files it would create",
            tier=Tier.T2_RISKY,
            schema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "Integration domain, e.g. my_thing"},
                    "name": {"type": ["string", "null"], "default": None, "description": "Friendly name"},
                    "version": {"type": "string", "default": "0.1.0"},
                    "dry_run": _DRY_RUN,
                },
                "required": ["domain"],
            },
            keywords=("scaffold", "custom", "component", "integration", "create", "boilerplate"),
        ),
    ),
)
