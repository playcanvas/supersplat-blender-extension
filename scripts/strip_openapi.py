#!/usr/bin/env python3
"""Write tests/fixtures/openapi.json from the SuperSplat API's OpenAPI document, without its prose.

    python3 scripts/strip_openapi.py <openapi.json> [tests/fixtures/openapi.json]

`description`, `example` and `examples` are removed everywhere except as
property names; everything else is kept as it is.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROSE = {"description", "example", "examples"}


def strip(node: Any, in_properties: bool = False) -> Any:
    if isinstance(node, dict):
        return {
            key: strip(value, key == "properties" and not in_properties)
            for key, value in node.items()
            if in_properties or key not in PROSE
        }
    if isinstance(node, list):
        return [strip(value) for value in node]
    return node


def main(argv: list[str]) -> int:
    if not 1 <= len(argv) <= 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    source = Path(argv[0])
    target = Path(argv[1]) if len(argv) > 1 else ROOT / "tests" / "fixtures" / "openapi.json"
    spec = json.loads(source.read_text(encoding="utf-8"))
    target.write_text(json.dumps(strip(spec), indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
