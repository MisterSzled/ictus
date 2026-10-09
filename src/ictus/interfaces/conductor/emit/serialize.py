"""Rendering a compiled document to YAML text.

Two settings are correctness, not formatting: wrapping is disabled, because
ruamel wraps a long double-quoted scalar without a continuation marker and
reloading turns the break into a space; and multi-line strings are emitted as
block scalars, which carry their line breaks literally.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString

if TYPE_CHECKING:
    from ictus.graph.values import YamlDict, YamlValue

__all__ = ["dump_yaml"]


def _yaml() -> YAML:
    yaml = YAML()
    yaml.default_flow_style = False
    # Never wrap a scalar: a wrapped double-quoted string loses its
    # continuation marker, and reloading turns the break into a space.
    yaml.width = 1 << 30
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def blockify(value: YamlValue) -> YamlValue:
    """Render multi-line strings as block scalars, which carry their breaks literally."""
    if isinstance(value, dict):
        return {key: blockify(item) for key, item in value.items()}
    if isinstance(value, list):
        return [blockify(item) for item in value]
    if isinstance(value, str) and "\n" in value and block_safe(value):
        return LiteralScalarString(value)
    return value


def block_safe(text: str) -> bool:
    """Whether block style round-trips this string exactly.

    Trailing whitespace, tabs and carriage returns do not, so those stay quoted.
    """
    if "\r" in text or "\t" in text:
        return False
    return not any(line != line.rstrip() for line in text.split("\n"))


def dump_yaml(document: YamlDict) -> str:
    buf = io.StringIO()
    _yaml().dump(blockify(document), buf)
    return buf.getvalue()
