"""Move Blender 5.3 Gaussian splat point clouds to and from flat columns (SplatData)."""

from __future__ import annotations

import bpy
import numpy as np

from .supersplat.axes import ExportTransform, export_transform
from .supersplat.errors import DecodeError, ExportError
from .supersplat.ply import SplatData, degree_for_coefficients

# Attribute name -> (expected data type, components, foreach_get key)
_REQUIRED = {
    "position": ("FLOAT_VECTOR", 3, "vector"),
    "radiance:base": ("FLOAT4", 4, "vector"),
    "scale": ("FLOAT_VECTOR", 3, "vector"),
    "rotation": ("QUATERNION", 4, "value"),
}
_SH_PREFIX = "radiance:sh_"
_MAX_SH_COEFFICIENTS = 15

NO_SELECTION_MESSAGE = (
    "Select a Gaussian splat object (a point cloud whose type is Gaussian Splat)."
)


def is_splat_object(obj: bpy.types.Object | None) -> bool:
    return (
        obj is not None
        and obj.type == "POINTCLOUD"
        and getattr(obj.data, "type", None) == "GAUSSIAN_SPLAT"
    )


def find_target(context: bpy.types.Context) -> bpy.types.Object | None:
    """The active object if it is a splat, else the only selected splat."""
    active = context.view_layer.objects.active if context.view_layer else None
    if is_splat_object(active) and active.select_get():
        return active
    selected = [obj for obj in context.selected_objects if is_splat_object(obj)]
    if len(selected) == 1:
        return selected[0]
    if is_splat_object(active) and not selected:
        return active
    return None


def sh_coefficient_count(pointcloud: bpy.types.PointCloud) -> int:
    names = pointcloud.attributes.keys()
    count = 0
    while count < _MAX_SH_COEFFICIENTS and f"{_SH_PREFIX}{count}" in names:
        count += 1
    return count


def available_sh_degree(pointcloud: bpy.types.PointCloud) -> int:
    return degree_for_coefficients(sh_coefficient_count(pointcloud))


def transform_for(obj: bpy.types.Object, convert_axes: bool) -> ExportTransform:
    """World matrix (plus the SuperSplat axis conversion) applied on export.

    Raises ExportError for transforms a splat cannot represent (non-uniform
    scale, shear, mirroring).
    """
    return export_transform([list(row) for row in obj.matrix_world], convert_axes=convert_axes)


def read_splat(obj: bpy.types.Object, depsgraph: bpy.types.Depsgraph) -> SplatData:
    """Snapshot the evaluated splat attributes. Must run on the main thread."""
    evaluated = obj.evaluated_get(depsgraph)
    pointcloud = evaluated.data
    if getattr(pointcloud, "type", None) != "GAUSSIAN_SPLAT":
        raise ExportError(f"'{obj.name}' does not evaluate to a Gaussian splat point cloud.")
    count = len(pointcloud.points)
    if count == 0:
        raise ExportError(f"'{obj.name}' contains no splats.")

    attributes = pointcloud.attributes
    missing = [name for name in _REQUIRED if name not in attributes]
    if missing:
        raise ExportError(
            f"'{obj.name}' is missing Gaussian splat attributes: {', '.join(missing)}."
        )

    def column(name: str, data_type: str, width: int, key: str) -> np.ndarray:
        attribute = attributes[name]
        if attribute.domain != "POINT" or attribute.data_type != data_type:
            raise ExportError(
                f"Attribute '{name}' must be a {data_type} point attribute "
                f"(found {attribute.data_type} on {attribute.domain})."
            )
        values = np.empty(count * width, dtype=np.float32)
        attribute.data.foreach_get(key, values)
        return values

    columns = {name: column(name, *spec) for name, spec in _REQUIRED.items()}
    sh = [
        column(f"{_SH_PREFIX}{index}", "FLOAT_VECTOR", 3, "vector")
        for index in range(sh_coefficient_count(pointcloud))
    ]
    return SplatData(
        count=count,
        position=columns["position"],
        base=columns["radiance:base"],
        scale=columns["scale"],
        rotation=columns["rotation"],
        sh=sh,
    )


def new_splat_object(
    name: str, data: SplatData, collection: bpy.types.Collection | None = None
) -> bpy.types.Object:
    """Create a Gaussian splat object from columns in Blender's attribute layout.

    The result matches what Blender's PLY importer builds for the same splats.
    Must run on the main thread.
    """
    if data.count <= 0:
        raise DecodeError("The scene contains no splats.")
    data.validate()
    pointcloud = bpy.data.pointclouds.new(name)
    pointcloud.type = "GAUSSIAN_SPLAT"
    pointcloud.resize(data.count)
    columns = {
        "position": data.position,
        "radiance:base": data.base,
        "scale": data.scale,
        "rotation": data.rotation,
    }
    columns.update({f"{_SH_PREFIX}{index}": column for index, column in enumerate(data.sh)})
    attributes = pointcloud.attributes
    for attribute_name, values in columns.items():
        data_type, _width, key = _REQUIRED.get(attribute_name, ("FLOAT_VECTOR", 3, "vector"))
        attribute = attributes.get(attribute_name) or attributes.new(attribute_name, data_type, "POINT")
        attribute.data.foreach_set(key, np.ascontiguousarray(values, dtype=np.float32).reshape(-1))
    pointcloud.update_tag()
    obj = bpy.data.objects.new(name, pointcloud)
    (collection or bpy.context.collection).objects.link(obj)
    return obj
