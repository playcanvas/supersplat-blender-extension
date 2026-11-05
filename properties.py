"""Per-scene import and publish settings shown in the SuperSplat sidebar panels."""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, EnumProperty, PointerProperty, StringProperty

from .runtime import runtime
from .supersplat.errors import ConfigurationError
from .supersplat.scene_import import SceneInfo, format_count, parse_scene_reference

# Blender keeps only pointers to dynamic enum strings; hold them here.
_level_items: list[tuple[str, str, str, int]] = []


def looked_up_scene(settings: bpy.types.PropertyGroup) -> SceneInfo | None:
    """The scene the last lookup or import found, if it is the one the link names."""
    state = runtime.importing.state()
    if state is None or state.scene is None:
        return None
    try:
        splat_id = parse_scene_reference(settings.import_link)
    except ConfigurationError:
        return None
    return state.scene if state.scene.id == splat_id else None


def _levels(self: bpy.types.PropertyGroup, _context: bpy.types.Context) -> list[tuple[str, str, str, int]]:
    scene = looked_up_scene(self)
    items = [("0", "Finest", "The most detailed level", 0)]
    if scene is not None and scene.levels > 1:
        last = scene.levels - 1
        items = [
            (
                str(level),
                f"{'Finest' if level == 0 else 'Coarsest' if level == last else f'Level {level}'}"
                f" ({format_count(count)} splats)",
                f"Level of detail {level}: {count:,} splats",
                level,
            )
            for level, count in enumerate(scene.lod_counts)
        ]
    _level_items[:] = items
    return _level_items


def _link_changed(self: bpy.types.PropertyGroup, _context: bpy.types.Context) -> None:
    # A link to another scene starts again from the finest level, without the last error.
    if looked_up_scene(self) is None:
        self.import_level = "0"
    if not runtime.importing.busy():
        runtime.importing.error = ""


class SuperSplatSettings(bpy.types.PropertyGroup):
    import_link: StringProperty(
        name="Link or Scene ID",
        description="A SuperSplat scene link (superspl.at/scene/...) or scene ID",
        update=_link_changed,
    )
    import_level: EnumProperty(
        name="Detail",
        description="Level of detail to import from a streamed (SSOG) scene",
        items=_levels,
    )
    import_environment: BoolProperty(
        name="Environment",
        description="Also import the scene's environment (its far background) as a separate object",
        default=True,
    )
    title: StringProperty(
        name="Title",
        description="Title shown on SuperSplat (up to 100 characters). Defaults to the object name",
    )
    description: StringProperty(
        name="Description",
        description="Optional description shown on SuperSplat",
    )
    sh_degree: EnumProperty(
        name="SH degree",
        description="Spherical harmonics degree to export, limited to what the splat contains",
        items=(
            ("3", "Full / Degree 3", "Export all available spherical harmonics"),
            ("2", "Degree 2", ""),
            ("1", "Degree 1", ""),
            ("0", "RGB only", "Export base colour only"),
        ),
        default="3",
    )
    convert_axes: BoolProperty(
        name="Convert Axes for SuperSplat",
        description=(
            "Convert Blender's Z-up world space to the 3DGS PLY convention so the scene is "
            "upright in SuperSplat. Turn off to export Blender coordinates unchanged"
        ),
        default=True,
    )


def register() -> None:
    bpy.utils.register_class(SuperSplatSettings)
    bpy.types.Scene.supersplat = PointerProperty(type=SuperSplatSettings)


def unregister() -> None:
    del bpy.types.Scene.supersplat
    bpy.utils.unregister_class(SuperSplatSettings)
