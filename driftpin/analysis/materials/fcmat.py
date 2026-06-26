"""Mapping + loader for FreeCAD's bundled ``.FCMat`` material cards.

FreeCAD ships ~100+ engineering material cards (the Material workbench /
``Mod/Material`` library: steels, aluminium alloys, thermoplastics, glasses,
cast irons …). This module is **the FCMat → DriftPin-schema mapping source of
truth**: ``tools/build_material_corpus.py`` calls :func:`load_fcmat_cards` to
vendor those cards into the shipped ``fcmat.json``, and the runtime corpus
loader can also enrich opportunistically via
``materials.reload_corpus(extra_cards=load_fcmat_cards())``.

Two FCMat on-disk formats are supported:

* **FreeCAD >= 1.0** — YAML cards (``General:``/``Models:`` blocks) with property
  values nested under ``Models.LinearElastic`` / ``Models.Thermal`` / … . These
  need PyYAML, imported lazily so importing this module never hard-fails.
* **Legacy (<= 0.21)** — INI-style ``.FCMat`` (flat ``[Mechanical]`` sections),
  parsed with the stdlib ``configparser``.

The property values already use the same SI quantity strings as our corpus
(``"68900 MPa"``, ``"2700 kg/m^3"``), so they merge cleanly. The module
**degrades gracefully**: a missing directory, a missing PyYAML, or an
unparseable file yields fewer/zero cards rather than raising — so the seed
corpus is always the guaranteed baseline.

FreeCAD ``.FCMat`` cards are LGPL/CC-BY and ship with FreeCAD; the vendored JSON
attributes them in its header and each card records its FCMat filename + the
card's own ``SourceURL``/``License`` in ``source``.
"""
from __future__ import annotations

import configparser
import os
import re
from pathlib import Path

# Some FreeCAD cards use a European decimal comma ("80,00 MPa"); normalise a
# comma sitting between two digits to a dot so parse_quantity accepts it.
_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d)")


def _normq(value: str) -> str:
    return _DECIMAL_COMMA.sub(".", value)

# FreeCAD FCMat property key -> our card key. Source of truth for the mapping;
# applies to both the YAML (nested) and legacy INI (flat) layouts.
_FCMAT_MAP = {
    "Name": "name",
    "YoungsModulus": "YoungsModulus",
    "PoissonRatio": "PoissonRatio",
    "ShearModulus": "shear_modulus",
    "Density": "Density",
    "UltimateTensileStrength": "ultimate_strength",
    "YieldStrength": "yield_strength",
    "ThermalConductivity": "thermal_conductivity",
    "SpecificHeat": "specific_heat",
    "ThermalExpansionCoefficient": "cte",
}

# 2nd-level (under Standard/) or Father-model name -> our category label.
_CATEGORY_MAP = {
    "aluminum": "aluminum", "aluminium": "aluminum",
    "steel": "steel", "iron": "cast_iron", "copper": "copper",
    "titanium": "titanium", "alloys": "metal", "metal": "metal",
    "thermoplast": "polymer", "thermoset": "polymer",
    "glass": "glass", "wood": "wood", "carbon": "carbon",
    "aggregate": "aggregate",
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
        dirs += [
            # FreeCAD >= 1.0 layout
            res / "Mod" / "Material" / "Resources" / "Materials" / "Standard",
            res / "Mod" / "Material" / "Resources" / "Materials",
            # legacy layout
            res / "Mod" / "Material" / "StandardMaterial",
            res / "Mod" / "Material" / "Standard",
            res / "Mod" / "Material",
        ]
    except Exception:
        pass
    return [d for d in dirs if d.is_dir()]


def _category_for(path: Path) -> str:
    """Best-effort category from the card's directory tree (…/Standard/Metal/
    Aluminum/foo.FCMat -> 'aluminum')."""
    parts = [p.lower().replace(".fcmat", "") for p in path.parts]
    # Walk the path; the last known token wins so a metal sub-dir is more
    # specific than its parent (…/Metal/Aluminum/foo -> 'aluminum').
    cat = None
    for seg in parts:
        if seg in _CATEGORY_MAP:
            cat = _CATEGORY_MAP[seg]
    return cat or "fcmat"


def _flatten_yaml(doc: dict) -> tuple[dict, dict]:
    """Return (flat_property_map, general_block) from a parsed FreeCAD>=1.0 YAML
    card. Property values nested under Models.<Model>.<Key> are flattened to
    {Key: value}; later models win on a key clash (rare)."""
    flat: dict[str, str] = {}
    models = doc.get("Models") or {}
    if isinstance(models, dict):
        for model in models.values():
            if not isinstance(model, dict):
                continue
            for k, v in model.items():
                if k in ("UUID", "Father"):
                    continue
                if isinstance(v, (str, int, float)) and str(v).strip():
                    flat[k] = str(v)
    general = doc.get("General") or {}
    if isinstance(general, dict) and general.get("Name"):
        flat.setdefault("Name", str(general["Name"]))
    return flat, (general if isinstance(general, dict) else {})


def _flatten_ini(path: Path) -> tuple[dict, dict]:
    cp = configparser.ConfigParser()
    cp.optionxform = str  # preserve key case
    cp.read(path, encoding="utf-8")
    flat: dict[str, str] = {}
    for section in cp.sections():
        for k, v in cp.items(section):
            if v:
                flat[k] = v
    return flat, {}


def _parse_one(path: Path) -> dict | None:
    text = ""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None

    flat: dict = {}
    general: dict = {}
    if text.lstrip().startswith("---") or "Models:" in text or "General:" in text:
        try:
            import yaml  # lazy: not a hard import-time dependency
        except Exception:
            return None
        try:
            doc = yaml.safe_load(text)
        except Exception:
            return None
        if not isinstance(doc, dict):
            return None
        flat, general = _flatten_yaml(doc)
    else:
        try:
            flat, general = _flatten_ini(path)
        except Exception:
            return None

    card: dict = {}
    for fc_key, our_key in _FCMAT_MAP.items():
        if fc_key in flat:
            card[our_key] = _normq(flat[fc_key]) if our_key != "name" else flat[fc_key]
    if "name" not in card:
        card["name"] = path.stem
    # Drop FreeCAD's empty placeholder card (rho=1 kg/m^3, no real props).
    if card.get("name") == "Default":
        return None
    # Need at least stiffness or density to be a usable mechanical card.
    if "YoungsModulus" not in card and "Density" not in card:
        return None

    card["category"] = _category_for(path)
    card["basis"] = "typical"
    src_bits = [f"FreeCAD FCMat {path.name}"]
    if general.get("License"):
        src_bits.append(str(general["License"]))
    if general.get("SourceURL"):
        src_bits.append(str(general["SourceURL"]))
    card["source"] = "; ".join(src_bits)
    return card


def load_fcmat_cards(directory: str | None = None) -> list[dict]:
    """Discover and parse FreeCAD ``.FCMat`` cards into our card schema.

    Looks in `directory`, then $DRIFTPIN_FCMAT_DIR, then FreeCAD's resource dir
    (if importable), supporting both the YAML (>=1.0) and legacy INI layouts.
    Skips non-mechanical cards (appearance/fluid/etc. without stiffness or
    density). Returns a (possibly empty) list of card dicts, de-duplicated by
    name (first directory wins) — never raises on a missing directory or an
    unparseable file."""
    cards: list[dict] = []
    seen: set[str] = set()
    for d in _candidate_dirs(directory):
        for path in sorted(d.rglob("*.FCMat")):
            card = _parse_one(path)
            if card and card["name"] not in seen:
                seen.add(card["name"])
                cards.append(card)
        if cards:
            break  # a usable directory was found; don't double-scan fallbacks
    return cards
