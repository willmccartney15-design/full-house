"""Normalise free-text room names into comparable categories."""

from __future__ import annotations

import re

# Order matters: first match wins.
_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("dorm", ("dorm", "bunk", "bed in", "shared room", "mixed", "female only", "male only")),
    ("family", ("family", "quad", "quadruple", "4 people", "triple", "apartment", "two bedroom", "2 bedroom")),
    ("twin", ("twin", "two single", "2 single")),
    ("single", ("single",)),
    ("suite", ("suite", "spa")),
    ("double", ("double", "queen", "king", "standard", "deluxe", "superior", "studio", "private")),
]


def normalise_room(name: str) -> str:
    text = re.sub(r"\s+", " ", name.lower())
    for category, keywords in _RULES:
        if any(k in text for k in keywords):
            return category
    return "other"
