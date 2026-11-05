"""Minimal JSON Schema checker for the keywords used in tests/fixtures/openapi.json.

The fixture is a trimmed copy of the public SuperSplat OpenAPI document at
https://playcanvas.com/api/supersplat/openapi.json, with descriptions and examples
stripped. Formats are ignored.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

SPEC_PATH = Path(__file__).resolve().parent / "fixtures" / "openapi.json"


@lru_cache(maxsize=1)
def spec() -> dict[str, Any]:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def schema(name: str) -> dict[str, Any]:
    return spec()["components"]["schemas"][name]


def operation(method: str, path: str) -> dict[str, Any]:
    return spec()["paths"][path][method.lower()]


def response_schema(method: str, path: str, status: int) -> dict[str, Any]:
    response = operation(method, path)["responses"][str(status)]
    return response["content"]["application/json"]["schema"]


def request_schema(method: str, path: str) -> dict[str, Any]:
    return operation(method, path)["requestBody"]["content"]["application/json"]["schema"]


def errors(instance: Any, node: dict[str, Any], where: str = "$") -> list[str]:
    if "$ref" in node:
        node = schema(node["$ref"].rsplit("/", 1)[-1])
    if "anyOf" in node:
        options = [errors(instance, option, where) for option in node["anyOf"]]
        return [] if any(not found for found in options) else [f"{where}: matches no anyOf option"]
    found: list[str] = []
    expected = node.get("type")
    if expected is not None and not _is_type(instance, expected):
        return [f"{where}: expected {expected}, got {type(instance).__name__}"]
    if "const" in node and instance != node["const"]:
        found.append(f"{where}: must equal {node['const']!r}")
    if "enum" in node and instance not in node["enum"]:
        found.append(f"{where}: {instance!r} not in {node['enum']}")
    if isinstance(instance, str):
        if len(instance) < node.get("minLength", 0):
            found.append(f"{where}: shorter than {node['minLength']}")
        if "maxLength" in node and len(instance) > node["maxLength"]:
            found.append(f"{where}: longer than {node['maxLength']}")
        if "pattern" in node and not re.search(node["pattern"], instance):
            found.append(f"{where}: does not match {node['pattern']}")
    if isinstance(instance, int) and not isinstance(instance, bool):
        if "minimum" in node and instance < node["minimum"]:
            found.append(f"{where}: below {node['minimum']}")
        if "maximum" in node and instance > node["maximum"]:
            found.append(f"{where}: above {node['maximum']}")
    if isinstance(instance, list):
        if len(instance) < node.get("minItems", 0):
            found.append(f"{where}: fewer than {node['minItems']} items")
        if "maxItems" in node and len(instance) > node["maxItems"]:
            found.append(f"{where}: more than {node['maxItems']} items")
        if node.get("uniqueItems") and len({json.dumps(i, sort_keys=True) for i in instance}) != len(instance):
            found.append(f"{where}: items are not unique")
        if "items" in node:
            for index, item in enumerate(instance):
                found += errors(item, node["items"], f"{where}[{index}]")
    if isinstance(instance, dict):
        properties = node.get("properties", {})
        for key in node.get("required", []):
            if key not in instance:
                found.append(f"{where}: missing required {key!r}")
        for key, value in instance.items():
            if key in properties:
                found += errors(value, properties[key], f"{where}.{key}")
            elif node.get("additionalProperties") is False:
                found.append(f"{where}: unexpected property {key!r}")
    return found


def _is_type(instance: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    types = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}
    return isinstance(instance, types[expected])
