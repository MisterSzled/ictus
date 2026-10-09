"""The value domain.

Every value crossing a node boundary ends up in YAML, so the representable
set is exactly what YAML represents. Naming the union is what lets the emitter
be type-checked end to end.
"""

from __future__ import annotations

__all__ = [
    "YamlDict",
    "YamlScalar",
    "YamlValue",
]

type YamlScalar = str | int | float | bool | None
type YamlValue = YamlScalar | list[YamlValue] | dict[str, YamlValue]
type YamlDict = dict[str, YamlValue]
