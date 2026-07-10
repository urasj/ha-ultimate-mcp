"""hacs/ surface manifest — HACS inventory from .storage (W1).

Pure data. Gated on the HACS custom integration being present, since the whole
surface reads HACS's own .storage file.
"""

from ultimate_mcp.spec import SurfaceSpec, Tier, ToolSpec

SURFACE = SurfaceSpec(
    name="hacs",
    summary="HACS (custom repositories) inventory: installed components, categories, "
    "and which have pending updates — read straight from .storage",
    impl_module="ultimate_mcp.tools.hacs.impl",
    requires=("integration:hacs",),  # HACS is a custom_component/integration, not an add-on
    tools=(
        ToolSpec(
            name="hacs_inventory",
            summary="Installed HACS repositories with category and installed/available versions",
            tier=Tier.T0_READ,
            keywords=("hacs", "inventory", "installed", "repositories", "custom", "components"),
        ),
        ToolSpec(
            name="hacs_pending_updates",
            summary="HACS repos where the installed version differs from the available version",
            tier=Tier.T0_READ,
            keywords=("hacs", "updates", "pending", "outdated", "upgrade", "available"),
        ),
    ),
)
