"""Optional enrichment: load FreeCAD's bundled ``.FCMat`` material cards.

FreeCAD ships ~100+ material cards (the Material workbench / ``Mod/Material``
library) as INI-style ``.FCMat`` files. They already use the same SI quantity
strings as our corpus, so they merge cleanly via
``materials.reload_corpus(extra_cards=load_fcmat_cards())``.

This module **degrades gracefully**: if no card directory can be found it returns
an empty list rather than raising, so the seed corpus is always the guaranteed
baseline and nothing here is a hard dependency on FreeCAD being installed.
"""
from __future__ import annotations

import configparser
import os
from pathlib import Path

# FreeCAD FCMat [Mechanical]/[Thermal]/... key -> our card key.
_FCMAT_MAP = {
    "Name": "name",
    "YoungsModulus": "YoungsModulus",
    "PoissonRatio": "PoissonRatio",
    "Density": "Density",
    "UltimateTensileStrength": "ultimate_strength",
    "YieldStrength": "yield_strength",
    "ThermalConductivity": "thermal_conductivity",
    "SpecificHeat": "specific_heat",
    "ThermalExpansionCoefficient": "cte",
}


def _candidate_dirs(extra: str | None = None) -> list[Path]:
    dirs: list[Path] = []
    if extra:
        dirs.append(Path(extra))
    env = os.environ.get("DRIFTPIN_FCMAT_DIR")
    if env:
        dirs.append(Path(env))
    # Best-effort: ask FreeCAD where its resources live, if importable.
    try:
        import FreeCAD  # type: ignore

        res = Path(FreeCAD.getResourceDir())
        dirs += [res / "Mod" / "Material" / "StandardMaterial",
                 res / "Mod" / "Material" / "Standard",
                 res / "Mod" / "Material"]
    except Exception:
        pass
    return [d for d in dirs if d.is_dir()]


def _parse_one(path: Path) -> dict | None:
    cp = configparser.ConfigParser()
    cp.optionxform = str  # preserve key case
    try:
        cp.read(path, encoding="utf-8")
    except Exception:
        return None
    flat: dict[str, str] = {}
    for section in cp.sections():
        for k, v in cp.items(section):
            if v:
                flat[k] = v
    card: dict[str, str] = {}
    for fc_key, our_key in _FCMAT_MAP.items():
        if fc_key in flat:
            card[our_key] = flat[fc_key]
    if "name" not in card:
        card["name"] = path.stem
    if "YoungsModulus" not in card and "Density" not in card:
        return None  # not a usable mechanical card
    card.setdefault("category", "fcmat")
    card["basis"] = "typical"
    card["source"] = f"FreeCAD FCMat: {path.name}"
    return card


def load_fcmat_cards(directory: str | None = None) -> list[dict]:
    """Discover and parse FreeCAD ``.FCMat`` cards into our card schema.

    Looks in `directory`, then $DRIFTPIN_FCMAT_DIR, then FreeCAD's resource dir
    (if importable). Returns a (possibly empty) list of card dicts — never raises
    on a missing directory or an unparseable file."""
    cards: list[dict] = []
    seen: set[str] = set()
    for d in _candidate_dirs(directory):
        for path in sorted(d.rglob("*.FCMat")):
            card = _parse_one(path)
            if card and card["name"] not in seen:
                seen.add(card["name"])
                cards.append(card)
    return cards
