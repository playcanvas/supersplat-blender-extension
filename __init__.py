"""SuperSplat: import SuperSplat scenes into Blender and publish Gaussian splats to SuperSplat."""

from __future__ import annotations

from . import importer, operators, panels, preferences, properties
from .runtime import runtime

_modules = (properties, preferences, operators, importer, panels)


def register() -> None:
    for module in _modules:
        module.register()


def unregister() -> None:
    runtime.shutdown()
    for module in reversed(_modules):
        module.unregister()
