#!/usr/bin/env python3
"""Extract a lean optical-material corpus from the refractiveindex.info-database.

The full database is vendored as a git submodule (pinned to a release tag); this
build step reads only the curated TARGETS below and emits a small JSON the
runtime materials loader merges — so the worker never needs PyYAML or the full
submodule tree at runtime, only this committed JSON.

Dev dependency: PyYAML (``pip install pyyaml``). Run after updating the submodule:

    python3 tools/extract_optical_corpus.py \
        --source vendor/refractiveindex.info-database/database \
        --out driftpin/analysis/materials/optical.json

Provenance (the submodule tag) is recorded in each card's ``optical_source`` and
in the file header. Data is CC0 (public domain).
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import yaml

# d-line (helium) wavelength in micrometres — the reference for n_d / Abbe.
LAMBDA_D_UM = 0.58756

# Curated extraction targets: rel path under <source>/data -> (name, category).
# Glass "specs" files carry full property bags (n_d, Abbe, density, CTE); the
# organic/main files carry only a dispersion formula, so n_d is computed.
TARGETS = {
    "specs/schott/optical/N-BK7.yml":  ("N-BK7", "glass"),
    "specs/schott/optical/N-SF11.yml": ("N-SF11", "glass"),
    "specs/schott/optical/F2.yml":     ("F2", "glass"),
    "main/SiO2/nk/Malitson.yml":       ("Fused-Silica", "glass"),
    "organic/(C5H8O2)n - poly(methyl methacrylate)/nk/Sultanova.yml": ("PMMA", "polymer"),
    "organic/(C8H8)n - polystyrene/nk/Sultanova.yml":                 ("Polystyrene", "polymer"),
    "organic/(C16H14O3)n - polycarbonate/nk/Sultanova.yml":           ("Polycarbonate", "polymer"),
}


def n_from_formula(formula_type: str, coeffs: list[float], lam_um: float) -> float:
    """Refractive index at wavelength lam_um from an rii dispersion formula.
    Supports 'formula 1' (Sellmeier, C terms squared) and 'formula 2'
    (Sellmeier-2, C terms used as-is). Returns n (float)."""
    l2 = lam_um * lam_um
    c0 = coeffs[0]
    n2_minus_1 = c0
    pairs = coeffs[1:]
    for i in range(0, len(pairs) - 1, 2):
        b, c = pairs[i], pairs[i + 1]
        denom = l2 - (c * c if formula_type == "formula 1" else c)
        n2_minus_1 += b * l2 / denom
    return (1.0 + n2_minus_1) ** 0.5


def _first_formula(data_block: list) -> tuple[str, list[float], str | None]:
    for entry in data_block:
        t = entry.get("type", "")
        if t.startswith("formula"):
            coeffs = [float(x) for x in str(entry["coefficients"]).split()]
            return t, coeffs, entry.get("wavelength_range")
    raise ValueError("no dispersion formula in DATA block")


def _q(value, unit: str) -> str:
    return f"{value} {unit}".strip()


def extract_one(path: Path, name: str, category: str, tag: str, rel: str) -> dict:
    doc = yaml.safe_load(path.read_text())
    ftype, coeffs, wl_range = _first_formula(doc["DATA"])
    nd = round(n_from_formula(ftype, coeffs, LAMBDA_D_UM), 5)

    card: dict = {
        "name": name, "category": category,
        "refractive_index": f"{nd}",
        "dispersion_formula": ftype,
        "dispersion_coefficients": coeffs,
    }
    if wl_range:
        card["wavelength_range_um"] = str(wl_range)

    props = doc.get("PROPERTIES") or {}
    if "Vd" in props:
        card["abbe_number"] = f"{props['Vd']}"
    if "nd" in props:  # prefer the catalog's published n_d when present
        card["refractive_index"] = f"{props['nd']}"
    dens = props.get("density")
    if isinstance(dens, list) and dens and "value" in dens[0]:
        card["Density"] = _q(dens[0]["value"], "kg/m^3")
    cte = props.get("thermal_expansion")
    if isinstance(cte, list) and cte and "value" in cte[0]:
        card["cte"] = _q(cte[0]["value"], "1/K")

    card["basis"] = "vendor"
    card["optical_source"] = (
        f"refractiveindex.info-database {tag}: data/{rel} (CC0)"
    )
    # Mirror provenance into the common `source` field so every corpus card
    # carries source+basis (the materials gate in tests/test_materials.py).
    card["source"] = card["optical_source"]
    return card


def _submodule_tag(source: Path) -> str:
    """Best-effort: the tag/commit the submodule is checked out at."""
    repo = source
    while repo != repo.parent and not (repo / ".git").exists():
        repo = repo.parent
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown-revision"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="vendor/refractiveindex.info-database/database",
                    help="path to the database/ dir of the submodule")
    ap.add_argument("--out", default="driftpin/analysis/materials/optical.json")
    args = ap.parse_args()

    source = Path(args.source)
    data_root = source / "data"
    if not data_root.is_dir():
        raise SystemExit(f"source data dir not found: {data_root} "
                         f"(did you init the submodule?)")
    tag = _submodule_tag(source)

    cards, missing = [], []
    for rel, (name, category) in TARGETS.items():
        path = data_root / rel
        if not path.is_file():
            missing.append(rel)
            continue
        cards.append(extract_one(path, name, category, tag, rel))

    cards.sort(key=lambda c: (c["category"], c["name"]))
    payload = {
        "schema_version": 1,
        "generated_from": f"refractiveindex.info-database {tag}",
        "note": ("Optical cards extracted by tools/extract_optical_corpus.py from "
                 "the refractiveindex.info-database submodule (CC0). Merged "
                 "field-wise onto seed.json by the materials loader. Regenerate "
                 "after bumping the submodule tag."),
        "materials": cards,
    }
    out = Path(args.out)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {len(cards)} optical cards -> {out}  (from {tag})")
    if missing:
        print("MISSING (skipped):")
        for m in missing:
            print(f"  {m}")


if __name__ == "__main__":
    main()
