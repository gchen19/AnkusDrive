"""Materials database — lookup, Ashby-style selection, and FEM-card mapping.

Pure-Python, FreeCAD-free. The corpus is a JSON library of material cards whose
property values are **SI quantity strings** (``"68900 MPa"``, ``"2700 kg/m^3"``)
matching the FreeCAD FCMat / ``fem_set_material`` convention — so a card returned
by :func:`get` drops straight into a FEM material card via :func:`to_fem_material`.

Three public calls back the ``material_get`` / ``material_select`` /
``material_list`` tools:

    get(name)                       -> one card (dict)
    select(criteria, rank_by)       -> ranked list of cards that pass the filter
    list_materials(category=None)   -> brief names/categories

Optionally enrich the corpus with FreeCAD's bundled ``.FCMat`` cards via
:func:`driftpin.analysis.materials.fcmat.load_fcmat_cards` — see that module.

Design notes live in ``docs/SIMULATION_EXAMPLES.md`` (§2 + materials-corpus
section) and ``docs/SIMULATION_TOOLS.md`` (family 2).
"""
from __future__ import annotations

import json
from pathlib import Path

_SEED_PATH = Path(__file__).with_name("seed.json")
_OPTICAL_PATH = Path(__file__).with_name("optical.json")

# FEM material card keys consumed directly by fem_set_material.
_FEM_KEYS = ("YoungsModulus", "PoissonRatio", "Density")

# Canonical numeric accessors: name -> (card_field, scale, target_unit).
# Each converts a card's quantity string into a single float in the target unit,
# giving select()/rank_by a unit-stable basis. Scale multiplies the parsed value.
_NUMERIC = {
    "youngs_gpa":            ("YoungsModulus", 1e-3, "GPa"),
    "youngs_mpa":            ("YoungsModulus", 1.0, "MPa"),
    "poisson":               ("PoissonRatio", 1.0, ""),
    "density_g_cc":          ("Density", 1e-3, "g/cm^3"),
    "density_kg_m3":         ("Density", 1.0, "kg/m^3"),
    "yield_mpa":             ("yield_strength", 1.0, "MPa"),
    "uts_mpa":               ("ultimate_strength", 1.0, "MPa"),
    "fatigue_mpa":           ("fatigue_endurance", 1.0, "MPa"),
    "fracture_mpa_sqrt_m":   ("fracture_toughness", 1.0, "MPa*m^0.5"),
    "thermal_conductivity_w_mk": ("thermal_conductivity", 1.0, "W/m/K"),
    "specific_heat_j_kgk":   ("specific_heat", 1.0, "J/kg/K"),
    "cte_per_k":             ("cte", 1.0, "1/K"),
    "service_temp_c":        ("max_service_temp", 1.0, "C"),
    "cost_usd_kg":           ("rough_cost", 1.0, "USD/kg"),
    "refractive_index":      ("refractive_index", 1.0, ""),
    "abbe":                  ("abbe_number", 1.0, ""),
    # --- process / rheology layer (injection molding, issue #106) ---
    "melt_temp_c":           ("melt_temp_c", 1.0, "C"),
    "mold_temp_c":           ("mold_temp_c", 1.0, "C"),
    "eject_temp_c":          ("eject_temp_c", 1.0, "C"),
}

# Range-valued accessors: name -> (card_field, end, scale, target_unit) where the
# card value is a two-token "lo hi" string (e.g. recommended_wall_mm "0.8 3.5").
# end ∈ {"min","max"} selects which token. numeric() resolves these too.
_NUMERIC_RANGE = {
    "recommended_wall_min_mm": ("recommended_wall_mm", "min", 1.0, "mm"),
    "recommended_wall_max_mm": ("recommended_wall_mm", "max", 1.0, "mm"),
    "mold_shrinkage_min_pct":  ("mold_shrinkage_pct", "min", 1.0, "%"),
    "mold_shrinkage_max_pct":  ("mold_shrinkage_pct", "max", 1.0, "%"),
}

# rank_by -> (numeric-key expression, descending?). "specific_*" are computed.
_RANKERS = {
    "strength":          ("yield_mpa", True),
    "stiffness":         ("youngs_gpa", True),
    "cost":              ("cost_usd_kg", False),
    "density":           ("density_g_cc", False),
    "specific_strength": ("__specific_strength", True),
    "specific_stiffness": ("__specific_stiffness", True),
}


class MaterialNotFound(KeyError):
    """Raised by get() when no card matches the requested name."""


# --- corpus loading -----------------------------------------------------------

_CACHE: dict | None = None


def _merge_card(base: dict, overlay: dict) -> dict:
    """Field-wise merge: overlay fills/updates base, preserving base fields the
    overlay doesn't mention (so a vendor optical card augments a hand card's
    mechanical props rather than dropping them)."""
    out = dict(base)
    out.update(overlay)
    return out


def _load_corpus() -> dict:
    global _CACHE
    if _CACHE is None:
        cards = {m["name"]: m for m in json.loads(_SEED_PATH.read_text())["materials"]}
        # Optional vendor-extracted optical layer (tools/extract_optical_corpus.py),
        # merged field-wise so it augments rather than replaces seed cards.
        if _OPTICAL_PATH.is_file():
            for m in json.loads(_OPTICAL_PATH.read_text())["materials"]:
                cards[m["name"]] = _merge_card(cards.get(m["name"], {}), m)
        _CACHE = cards
    return _CACHE


def reload_corpus(extra_cards: list[dict] | None = None) -> int:
    """Reset the in-memory corpus from seed.json + optical.json, optionally
    merging extra_cards field-wise (e.g. from fcmat.load_fcmat_cards()). Returns
    the total card count. Later sources augment earlier ones by name."""
    global _CACHE
    _CACHE = None
    cards = dict(_load_corpus())
    for c in extra_cards or []:
        cards[c["name"]] = _merge_card(cards.get(c["name"], {}), c)
    _CACHE = cards
    return len(_CACHE)


# --- quantity-string parsing --------------------------------------------------

def parse_quantity(s) -> tuple[float, str]:
    """Split an SI quantity string into (value, unit). ``"68900 MPa"`` ->
    (68900.0, "MPa"); ``"0.33"`` -> (0.33, ""). Accepts an already-numeric input.
    Returns a (float, str) tuple. Raises ValueError on an unparseable value."""
    if isinstance(s, (int, float)):
        return float(s), ""
    if s is None:
        raise ValueError("cannot parse quantity from None")
    parts = str(s).strip().split(None, 1)
    try:
        value = float(parts[0])
    except (ValueError, IndexError) as e:
        raise ValueError(f"unparseable quantity: {s!r}") from e
    unit = parts[1].strip() if len(parts) > 1 else ""
    return value, unit


def parse_range(s) -> tuple[float, float]:
    """Split a two-token range string ``"0.8 3.5"`` -> (0.8, 3.5). A single token
    is treated as a degenerate range (lo == hi). Returns (lo, hi) floats with
    lo <= hi. Raises ValueError if unparseable or if lo > hi (a likely typo)."""
    if s is None:
        raise ValueError("cannot parse range from None")
    toks = str(s).strip().split()
    try:
        nums = [float(t) for t in toks[:2]]
    except (ValueError, IndexError) as e:
        raise ValueError(f"unparseable range: {s!r}") from e
    if not nums:
        raise ValueError(f"empty range: {s!r}")
    lo, hi = (nums[0], nums[1]) if len(nums) > 1 else (nums[0], nums[0])
    if lo > hi:
        raise ValueError(f"range lo>hi: {s!r}")
    return lo, hi


def numeric(card: dict, key: str):
    """Return a card property as a single float in canonical units (see _NUMERIC /
    _NUMERIC_RANGE), or None if the card lacks that property. Returns a float or
    None. Raises KeyError if key is not a known canonical accessor."""
    if key in _NUMERIC_RANGE:
        field, end, scale, _unit = _NUMERIC_RANGE[key]
        if field not in card:
            return None
        lo, hi = parse_range(card[field])
        return (lo if end == "min" else hi) * scale
    if key not in _NUMERIC:
        raise KeyError(f"unknown numeric accessor: {key!r}")
    field, scale, _unit = _NUMERIC[key]
    if field not in card:
        return None
    value, _u = parse_quantity(card[field])
    return value * scale


# --- public API ---------------------------------------------------------------

def get(name: str) -> dict:
    """Return the full material card for `name` (case-insensitive, hyphen/space
    insensitive). Returns the card dict. Raises MaterialNotFound (with a
    'did_you_mean' suggestion list) when nothing matches."""
    corpus = _load_corpus()
    if name in corpus:
        return dict(corpus[name])
    norm = _norm(name)
    for k, v in corpus.items():
        if _norm(k) == norm:
            return dict(v)
    suggestions = [k for k in corpus if norm in _norm(k) or _norm(k) in norm]
    raise MaterialNotFound(
        f"{name!r} not found; did_you_mean={suggestions or sorted(corpus)[:5]}"
    )


def list_materials(category: str | None = None) -> dict:
    """List corpus materials, optionally filtered to one category. Returns
    {count, category, materials:[{name, category}, ...]} sorted by name."""
    corpus = _load_corpus()
    cards = [
        {"name": c["name"], "category": c.get("category", "unknown")}
        for c in corpus.values()
        if category is None or c.get("category") == category
    ]
    cards.sort(key=lambda c: c["name"])
    return {"count": len(cards), "category": category, "materials": cards}


def select(criteria: dict | None = None, rank_by: str = "specific_strength") -> dict:
    """Filter the corpus by `criteria`, then Ashby-rank survivors by `rank_by`.

    criteria keys are ``min_<accessor>`` / ``max_<accessor>`` where <accessor> is
    one of the canonical numeric names (e.g. ``min_yield_mpa``, ``max_density_g_cc``,
    ``min_service_temp_c``). A card missing the queried property fails a min and
    passes a max (conservative). rank_by: strength | stiffness | cost | density |
    specific_strength | specific_stiffness.

    Returns {rank_by, count, criteria, candidates:[{name, score, ...numerics}, ...]}
    best-first. An empty filter yields candidates:[] (not the closest miss).
    Raises KeyError on an unknown criterion or rank_by."""
    criteria = criteria or {}
    corpus = _load_corpus()

    if rank_by not in _RANKERS:
        raise KeyError(f"unknown rank_by: {rank_by!r}; choose from {sorted(_RANKERS)}")

    parsed = []
    for raw_key, bound in criteria.items():
        if not (raw_key.startswith("min_") or raw_key.startswith("max_")):
            raise KeyError(f"criterion must start with min_/max_: {raw_key!r}")
        kind, acc = raw_key.split("_", 1)
        if acc not in _NUMERIC and acc not in _NUMERIC_RANGE:
            raise KeyError(f"unknown criterion accessor: {acc!r}")
        parsed.append((kind, acc, float(bound)))

    survivors = [c for c in corpus.values() if _passes(c, parsed)]
    scored = []
    for c in survivors:
        score = _score(c, rank_by)
        if score is None:
            continue  # can't rank a card missing the ranking property
        scored.append((score, c))

    descending = _RANKERS[rank_by][1]
    scored.sort(key=lambda t: t[0], reverse=descending)

    candidates = []
    for score, c in scored:
        candidates.append({
            "name": c["name"], "category": c.get("category"),
            "score": round(score, 4),
            "yield_mpa": numeric(c, "yield_mpa"),
            "density_g_cc": numeric(c, "density_g_cc"),
            "youngs_gpa": numeric(c, "youngs_gpa"),
            "cost_usd_kg": numeric(c, "cost_usd_kg"),
        })
    return {
        "rank_by": rank_by, "count": len(candidates),
        "criteria": criteria, "candidates": candidates,
    }


def refractive_index_at(card: dict, wavelength_nm: float = 587.56) -> float:
    """Refractive index of an optical card at `wavelength_nm` (default the d-line).

    Uses the card's vendor dispersion formula ('formula 1'/'formula 2' Sellmeier,
    coefficients in micrometres) when present; otherwise falls back to the static
    'refractive_index' (n_d). Returns n (float). Raises KeyError if the card has
    neither a dispersion formula nor a refractive_index, ValueError if the
    wavelength is outside the fitted range."""
    if "dispersion_coefficients" not in card:
        if "refractive_index" not in card:
            raise KeyError(f"card {card.get('name')!r} has no optical data")
        return parse_quantity(card["refractive_index"])[0]

    lam = wavelength_nm / 1000.0  # nm -> um
    rng = card.get("wavelength_range_um")
    if rng:
        lo, hi = (float(x) for x in str(rng).split())
        if not (lo <= lam <= hi):
            raise ValueError(
                f"{wavelength_nm} nm outside fitted range {rng} um for {card.get('name')!r}"
            )
    ftype = card.get("dispersion_formula", "formula 2")
    coeffs = [float(x) for x in card["dispersion_coefficients"]]
    l2 = lam * lam
    n2m1 = coeffs[0]
    pairs = coeffs[1:]
    for i in range(0, len(pairs) - 1, 2):
        b, c = pairs[i], pairs[i + 1]
        denom = l2 - (c * c if ftype == "formula 1" else c)
        n2m1 += b * l2 / denom
    return (1.0 + n2m1) ** 0.5


def to_fem_material(card: dict, name: str | None = None) -> dict:
    """Project a material card down to the keys fem_set_material consumes.
    Returns {YoungsModulus, PoissonRatio, Density, Name} as quantity strings.
    Raises KeyError if the card is missing a required structural property."""
    out = {}
    for k in _FEM_KEYS:
        if k not in card:
            raise KeyError(f"card {card.get('name')!r} missing FEM key {k!r}")
        out[k] = card[k]
    out["Name"] = name or card.get("name", "Material")
    return out


# --- helpers ------------------------------------------------------------------

def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _passes(card: dict, parsed) -> bool:
    for kind, acc, bound in parsed:
        val = numeric(card, acc)
        if kind == "min":
            if val is None or val < bound:
                return False
        else:  # max
            if val is not None and val > bound:
                return False
    return True


def _score(card: dict, rank_by: str):
    expr = _RANKERS[rank_by][0]
    if expr == "__specific_strength":
        y, d = numeric(card, "yield_mpa"), numeric(card, "density_g_cc")
        return None if (y is None or not d) else y / d
    if expr == "__specific_stiffness":
        e, d = numeric(card, "youngs_gpa"), numeric(card, "density_g_cc")
        return None if (e is None or not d) else e / d
    return numeric(card, expr)
