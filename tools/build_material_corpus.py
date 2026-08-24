#!/usr/bin/env python3
"""Vendor FreeCAD's bundled ``.FCMat`` material library into a shipped JSON.

FreeCAD ships ~100+ engineering material cards (steels, aluminium alloys,
thermoplastics, glasses, cast irons). They are LGPL/CC-BY and ship with
FreeCAD, so we vendor them into ``ankusdrive/analysis/materials/fcmat.json`` —
making the corpus always-present rather than gated on a runtime FreeCAD path.

The FCMat -> AnkusDrive-schema mapping lives in
``ankusdrive/analysis/materials/fcmat.py`` (the source of truth); this builder
just discovers + serialises. Run it after a FreeCAD upgrade:

    # explicit source (any FreeCAD install's Materials dir):
    python3 tools/build_material_corpus.py \
        --source "$(freecadcmd -c 'import FreeCAD;print(FreeCAD.getResourceDir())' \
                    2>/dev/null | tail -1)/Mod/Material/Resources/Materials/Standard"

    # or, with FreeCAD importable, auto-discover:
    freecadcmd tools/build_material_corpus.py

Parsing the YAML FCMat cards needs PyYAML (``pip install pyyaml``) — a dev
dependency, not a runtime one. Cards already present in ``seed.json`` win on a
name clash (the loader overlays seed onto the vendored base), so this file may
freely contain near-duplicates under FreeCAD's own names.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ankusdrive.analysis.materials import fcmat  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--source", default=None,
        help="FreeCAD Materials dir to scan (default: $ANKUSDRIVE_FCMAT_DIR or "
             "FreeCAD's resource dir if importable)")
    ap.add_argument(
        "--out",
        default=str(_REPO_ROOT / "ankusdrive" / "analysis" / "materials" / "fcmat.json"))
    args = ap.parse_args()

    cards = fcmat.load_fcmat_cards(args.source)
    if not cards:
        raise SystemExit(
            "no FCMat cards found — pass --source <FreeCAD>/Mod/Material/"
            "Resources/Materials/Standard, set $ANKUSDRIVE_FCMAT_DIR, or run "
            "under freecadcmd so FreeCAD is importable.")

    cards.sort(key=lambda c: (c.get("category", ""), c["name"]))
    payload = {
        "schema_version": 1,
        "note": (
            "FreeCAD bundled .FCMat material cards, vendored by "
            "tools/build_material_corpus.py via ankusdrive.analysis.materials."
            "fcmat (the FCMat->schema mapping source of truth). FreeCAD's "
            "material library is LGPL/CC-BY and ships with FreeCAD; each card "
            "records its FCMat filename + the card's own License/SourceURL in "
            "'source'. Merged field-wise UNDER seed.json by the loader (seed "
            "wins on a name clash). Regenerate after a FreeCAD upgrade."),
        "attribution": (
            "Material data from FreeCAD (https://www.freecad.org), "
            "Mod/Material library, licensed LGPL-2.1+/CC-BY. "
            "(c) The FreeCAD project and card authors."),
        "materials": cards,
    }
    out = Path(args.out)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {len(cards)} FCMat cards -> {out}")


if __name__ == "__main__":
    main()
