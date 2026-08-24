"""Release packages (issue #233) — pure-core unit tests.

No FreeCAD, no worker, no LLM: :mod:`ankusdrive.release` is the gate logic, the
naming, the determinism scrub and the manifest arithmetic, so these run on the host
interpreter in milliseconds. They prove the four things the layer promises:

  * the gates are TWO-SIDED and run before anything is written — a draft-state item
    refuses without ``draft=True``, an obsolete one refuses even with it, a title
    block whose revision disagrees with the registry refuses with a naming diff,
    and a clean item passes;
  * the same inputs produce the same BYTES — two exports of one solid that differ
    only in their wall-clock header scrub to identical bytes, and the manifest
    carries nothing that could vary independently of its inputs;
  * a draft artifact is un-mistakable — every format carries the PRELIMINARY mark
    in its own comment convention, and a format with no convention refuses rather
    than writing an unmarked file;
  * the manifest is the root of trust — its checksums catch a mutated, truncated or
    vanished artifact.

Run: python3 tests/test_release_package.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import release as rel  # noqa: E402

_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def _ok(label, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}  {detail}")


# --- the golden fixture -------------------------------------------------------
#
# One released item at Rev B, cut under ECO-0007, with a title block that agrees
# with it. Every gate test perturbs exactly one thing away from this.

def _record(**over):
    rec = {
        "part_number": "DP-001001",
        "rev": "B",
        "lifecycle": "released",
        "files": ["components/plate.FCStd"],
        "metadata": {"material": "AL6061-T6", "eco": "ECO-0007"},
    }
    rec.update(over)
    return rec


def _ident(**over):
    return rel.identity("plate", _record(**over))


def _title(**over):
    tb = {"part": "PLATE", "part_number": "DP-001001", "rev": "B",
          "material": "AL6061-T6"}
    tb.update(over)
    return tb


def _gate_ok():
    return {"ok": True, "violations": []}


def _page(**over):
    entry = {"page": "Page", "title_block": _title(), "gate": _gate_ok()}
    entry.update(over)
    return entry


def _codes(verdict):
    return sorted(p["code"] for p in verdict["problems"])


# --- identity + kind resolution ----------------------------------------------

def test_identity_reads_the_registry_not_the_caller():
    print("test_identity_reads_the_registry_not_the_caller")
    ident = _ident()
    _check("part number", ident["part_number"], "DP-001001")
    _check("revision", ident["rev"], "B")
    _check("lifecycle", ident["lifecycle"], "released")
    _check("material comes from item metadata", ident["material"], "AL6061-T6")
    _check("eco comes from item metadata", ident["eco"], "ECO-0007")
    # an explicit ECO overrides the recorded one — this release is cut under it
    _check("explicit eco wins",
           rel.identity("plate", _record(), eco="ECO-0099")["eco"], "ECO-0099")


def test_kinds_are_canonical_and_the_manifest_is_not_optional():
    print("test_kinds_are_canonical_and_the_manifest_is_not_optional")
    _check("default bundle", rel.resolve_kinds()["kinds"], list(rel.DEFAULT_KINDS))
    # the caller's order and duplicates must not reach the file set
    got = rel.resolve_kinds(["bom_csv", "step", "step"])["kinds"]
    _check("canonical order, deduplicated", got, ["step", "bom_csv", "manifest_json"])
    _ok("manifest is always present",
        rel.MANIFEST_JSON in rel.resolve_kinds(["step"])["kinds"])
    try:
        rel.resolve_kinds(["step", "drawing_png"])
    except ValueError as exc:
        _ok("an unknown kind fails loudly", "drawing_png" in str(exc), str(exc))
    else:
        _ok("an unknown kind fails loudly", False, "no ValueError raised")


def test_inspection_implies_a_drawing_to_balloon():
    print("test_inspection_implies_a_drawing_to_balloon")
    # a plan of balloon numbers with no print to find them on is not a deliverable
    res = rel.resolve_kinds(["step", "inspection"])
    _ok("a drawing kind is added", rel.DRAWING_SVG in res["kinds"], res)
    _check("and it is reported, not silent", res["implied"], [rel.DRAWING_SVG])
    already = rel.resolve_kinds(["inspection", "drawing_pdf"])
    _check("nothing implied when a drawing is already asked for",
           already["implied"], [])
    _ok("no extra drawing kind sneaks in",
        rel.DRAWING_SVG not in already["kinds"], already["kinds"])


def test_rfq_drops_the_internal_only_artifacts():
    print("test_rfq_drops_the_internal_only_artifacts")
    res = rel.resolve_kinds(["step", "drawing_pdf", "inspection"], rfq=True)
    _ok("the inspection package is dropped",
        rel.INSPECTION not in res["kinds"], res["kinds"])
    _check("and reported, not silently gone", res["dropped"], [rel.INSPECTION])
    _ok("the geometry and the print still ship",
        {"step", "drawing_pdf"} <= set(res["kinds"]), res["kinds"])
    _check("a release keeps it",
           rel.INSPECTION in rel.resolve_kinds(["inspection"])["kinds"], True)


# --- the lifecycle gate is two-sided -----------------------------------------

def test_a_released_item_packages_cleanly():
    print("test_a_released_item_packages_cleanly")
    v = rel.release_gate(_ident(), kinds=rel.DEFAULT_KINDS, pages=[_page()])
    _check("gate passes", v["ok"], True)
    _check("no problems", v["problems"], [])
    _check("nothing is watermarked", v["watermark"], None)
    _check("the page was actually checked", v["checked_pages"], ["Page"])


def test_a_draft_item_refuses_without_the_draft_flag():
    print("test_a_draft_item_refuses_without_the_draft_flag")
    ident = _ident(lifecycle="in_work", rev="-")
    v = rel.release_gate(ident, kinds=["step"], draft=False)
    _check("refused", v["ok"], False)
    _check("with the lifecycle code", _codes(v), ["not_released"])
    prob = v["problems"][0]
    _check("naming diff: expected released", prob["expected"], "released")
    _check("naming diff: actual in_work", prob["actual"], "in_work")
    _ok("the reason names the escape hatch", "draft=True" in prob["reason"],
        prob["reason"])

    d = rel.release_gate(ident, kinds=["step"], draft=True)
    _check("draft=True unlocks it", d["ok"], True)
    _check("and everything gets watermarked", d["watermark"], rel.WATERMARK_TEXT)


def test_in_review_is_draftable_too():
    print("test_in_review_is_draftable_too")
    ident = _ident(lifecycle="in_review", rev="-")
    _check("refused as a real release",
           rel.release_gate(ident, kinds=["step"])["ok"], False)
    _check("allowed as a draft",
           rel.release_gate(ident, kinds=["step"], draft=True)["ok"], True)


def test_an_obsolete_item_refuses_even_as_a_draft():
    print("test_an_obsolete_item_refuses_even_as_a_draft")
    # a retired part is not "preliminary" — a watermark saying so would be a wrong
    # description, so draft=True must NOT unlock it
    ident = _ident(lifecycle="obsolete")
    for label, draft in (("as a release", False), ("as a draft", True)):
        v = rel.release_gate(ident, kinds=["step"], draft=draft)
        _check(f"refused {label}", v["ok"], False)
        _check(f"code {label}", _codes(v), ["not_releasable"])


def test_a_released_item_without_a_revision_refuses():
    print("test_a_released_item_without_a_revision_refuses")
    # lifecycle.transition stamps rev "A" on first release, so this state is only
    # reachable through a hand-edited registry — which is exactly when you want the
    # package to refuse rather than emit files named "DP-001001_-.step"
    v = rel.release_gate(_ident(rev="-"), kinds=["step"])
    _check("refused", _codes(v), ["no_revision"])


# --- the title-block gate is two-sided ---------------------------------------

def test_a_rev_disagreement_refuses_with_a_naming_diff():
    print("test_a_rev_disagreement_refuses_with_a_naming_diff")
    # the classic: the model was revised, the print was not re-stamped
    v = rel.release_gate(_ident(), kinds=["drawing_pdf"],
                         pages=[_page(title_block=_title(rev="A"))])
    _check("refused", v["ok"], False)
    prob = v["problems"][0]
    _check("code", prob["code"], "title_block_mismatch")
    _check("field", prob["field"], "rev")
    _check("expected is the registry's revision", prob["expected"], "B")
    _check("actual is the print's", prob["actual"], "A")
    _check("and it names the page", prob["where"], "Page")
    _ok("the reason says what went wrong",
        "different revisions" in prob["reason"], prob["reason"])


def test_a_part_number_disagreement_refuses():
    print("test_a_part_number_disagreement_refuses")
    v = rel.release_gate(_ident(), kinds=["drawing_pdf"],
                         pages=[_page(title_block=_title(part_number="DP-009999"))])
    _check("refused", _codes(v), ["title_block_mismatch"])
    _check("field", v["problems"][0]["field"], "part_number")
    # a block with only a `part` field is judged on it — that is what a vendor reads
    ok = rel.release_gate(_ident(), kinds=["drawing_pdf"],
                          pages=[_page(title_block={"part": "DP-001001", "rev": "B",
                                                    "material": "AL6061-T6"})])
    _check("a bare part field standing in for the number passes", ok["ok"], True)


def test_a_material_disagreement_refuses_but_silence_does_not():
    print("test_a_material_disagreement_refuses_but_silence_does_not")
    bad = rel.release_gate(_ident(), kinds=["drawing_pdf"],
                           pages=[_page(title_block=_title(material="AL7075-T6"))])
    _check("a contradiction refuses", _codes(bad), ["title_block_mismatch"])
    _check("field", bad["problems"][0]["field"], "material")
    # a registry that declares no material has nothing to disagree with; a stated
    # material on the print is then information, not a conflict
    rec = _record()
    rec["metadata"] = {"eco": "ECO-0007"}
    quiet = rel.release_gate(rel.identity("plate", rec), kinds=["drawing_pdf"],
                             pages=[_page(title_block=_title(material="AL7075-T6"))])
    _check("silence in the registry is not a conflict", quiet["ok"], True)


def test_a_missing_field_is_a_mismatch_not_an_exemption():
    print("test_a_missing_field_is_a_mismatch_not_an_exemption")
    # an untitled print is exactly as ambiguous as a wrongly-titled one
    tb = _title()
    tb.pop("rev")
    v = rel.release_gate(_ident(), kinds=["drawing_pdf"],
                         pages=[_page(title_block=tb)])
    _check("the absent rev is caught", _codes(v), ["title_block_mismatch"])
    _check("with actual=None", v["problems"][0]["actual"], None)
    none = rel.release_gate(_ident(), kinds=["drawing_pdf"],
                            pages=[_page(title_block=None)])
    _check("no title block at all is its own code", _codes(none), ["no_title_block"])


def test_every_disagreement_is_reported_at_once():
    print("test_every_disagreement_is_reported_at_once")
    # an agent that has to re-release three times to find three problems stops using
    # the gate, so the gate reports them all
    v = rel.release_gate(_ident(), kinds=["drawing_pdf"], pages=[_page(
        title_block=_title(part_number="X", rev="A", material="Steel"))])
    _check("three mismatches, one call", len(v["problems"]), 3)
    _check("one per field", sorted(p["field"] for p in v["problems"]),
           ["material", "part_number", "rev"])


# --- the drawing gate + capabilities -----------------------------------------

def test_an_incomplete_drawing_refuses():
    print("test_an_incomplete_drawing_refuses")
    gate = {"ok": False, "violations": [{"code": "under", "reason": "..."},
                                        {"code": "no_datum", "reason": "..."}]}
    v = rel.release_gate(_ident(), kinds=["drawing_pdf"],
                         pages=[_page(gate=gate)])
    _check("refused", _codes(v), ["drawing_gate"])
    _check("carrying the gate's own codes", v["problems"][0]["actual"],
           ["no_datum", "under"])
    ungated = rel.release_gate(_ident(), kinds=["drawing_pdf"],
                               pages=[_page(gate=None)])
    _check("an ungated page is refused too", _codes(ungated), ["no_drawing_gate"])


def test_drawing_kinds_need_a_page():
    print("test_drawing_kinds_need_a_page")
    v = rel.release_gate(_ident(), kinds=["step", "drawing_dxf"], pages=[])
    _check("refused", _codes(v), ["no_pages"])
    _check("a geometry-only package needs none",
           rel.release_gate(_ident(), kinds=["step"], pages=[])["ok"], True)


def test_a_kind_the_host_cannot_render_refuses_before_writing():
    print("test_a_kind_the_host_cannot_render_refuses_before_writing")
    caps = {rel.DRAWING_PDF: {"ok": False, "reason": "needs svglib + reportlab",
                              "install": "pip install svglib reportlab"}}
    v = rel.release_gate(_ident(), kinds=["step", "drawing_pdf"], pages=[_page()],
                         capabilities=caps)
    _check("refused", _codes(v), ["kind_unavailable"])
    _ok("the reason carries the install hint",
        "pip install" in v["problems"][0]["reason"], v["problems"][0]["reason"])
    _check("a capable host passes",
           rel.release_gate(_ident(), kinds=["step", "drawing_pdf"],
                            pages=[_page()],
                            capabilities={rel.DRAWING_PDF: {"ok": True}})["ok"], True)


# --- naming -------------------------------------------------------------------

def test_names_lead_with_the_identity_and_carry_no_date():
    print("test_names_lead_with_the_identity_and_carry_no_date")
    _check("step", rel.artifact_name("DP-001001", "B", rel.STEP),
           "DP-001001_B.step")
    _check("pdf per page",
           rel.artifact_name("DP-001001", "B", rel.DRAWING_PDF, page="Page"),
           "DP-001001_B_Page.pdf")
    _check("dxf per page",
           rel.artifact_name("DP-001001", "B", rel.DRAWING_DXF, page="Page001"),
           "DP-001001_B_Page001.dxf")
    _check("bom", rel.artifact_name("DP-001001", "B", rel.BOM_CSV),
           "DP-001001_B_bom.csv")
    _check("manifest", rel.artifact_name("DP-001001", "B", rel.MANIFEST_JSON),
           "DP-001001_B_manifest.json")
    _check("fai form",
           rel.artifact_name("DP-001001", "B", rel.INSPECTION, page="Page",
                             suffix="fai"), "DP-001001_B_Page_fai.csv")
    _check("inspection plan",
           rel.artifact_name("DP-001001", "B", rel.INSPECTION, page="Page",
                             suffix="plan"), "DP-001001_B_Page_plan.json")


def test_names_are_filesystem_safe_and_stable():
    print("test_names_are_filesystem_safe_and_stable")
    got = rel.artifact_name("DP/001 001", "1.0", rel.STEP)
    _ok("unsafe characters collapse", got == "DP_001_001_1.0.step", got)
    _check("and it is a pure function of its inputs", got,
           rel.artifact_name("DP/001 001", "1.0", rel.STEP))


# --- determinism: the export headers ------------------------------------------

# A real FreeCAD STEP header, trimmed. Both volatile fields are present: the export
# instant in FILE_NAME, and OpenCASCADE's per-session export counter on the PRODUCT
# name (the "7.8 1" / "7.8 2" suffix).
_STEP_A = (b"ISO-10303-21;\nHEADER;\n"
           b"FILE_DESCRIPTION(('FreeCAD Model'),'2;1');\n"
           b"FILE_NAME('Open CASCADE Shape Model','2026-08-01T16:02:04',"
           b"('FreeCAD'),(\n    'FreeCAD'),'Open CASCADE STEP processor 7.8',"
           b"'FreeCAD','Unknown');\nENDSEC;\nDATA;\n"
           b"#7 = PRODUCT('Open CASCADE STEP translator 7.8 1',\n"
           b"  'Open CASCADE STEP translator 7.8 1','',(#8));\n"
           b"#11 = FOO();\nEND-ISO-10303-21;\n")
_STEP_B = (_STEP_A.replace(b"2026-08-01T16:02:04", b"2026-08-02T09:41:00")
                  .replace(b"translator 7.8 1", b"translator 7.8 2"))


def test_the_step_header_is_what_breaks_reproducibility():
    print("test_the_step_header_is_what_breaks_reproducibility")
    # the geometry is identical; only the export instant and OpenCASCADE's session
    # counter differ. Left alone, two releases of the SAME revision would not be
    # diffable by checksum — the promise the package is supposed to make.
    _ok("raw exports differ", _STEP_A != _STEP_B)
    a = rel.canonical_bytes("x.step", _STEP_A)
    b = rel.canonical_bytes("x.step", _STEP_B)
    _check("scrubbed exports are identical", a, b)
    _check("and so are their checksums", rel.digest(a), rel.digest(b))
    _ok("the fixed epoch is what landed", rel.EPOCH.encode() in a, a[:200])
    _ok("the session counter is gone", b"translator 7.8 1" not in a, a)
    _ok("the geometry section is untouched", b"#11 = FOO();" in a, a[-100:])
    _ok("the file is still a valid STEP",
        a.startswith(b"ISO-10303-21;") and a.rstrip().endswith(b"END-ISO-10303-21;"))


def test_the_step_product_name_carries_the_release_identity():
    print("test_the_step_product_name_carries_the_release_identity")
    # the counter has to be rewritten anyway, so spend the field on something the
    # recipient can use: the model self-identifies in their CAD system
    out = rel.canonical_bytes("x.step", _STEP_A, product="DP-001001 Rev B")
    _check("both PRODUCT string arguments are stamped",
           out.count(b"'DP-001001 Rev B'"), 2)
    _ok("the translator name is gone", b"Open CASCADE STEP translator" not in out, out)
    _ok("the entity is still well formed", b"#7 = PRODUCT('DP-001001 Rev B'," in out,
        out)
    # two different revisions of the same solid must NOT collide by checksum
    other = rel.canonical_bytes("x.step", _STEP_A, product="DP-001001 Rev C")
    _ok("a different revision is a different file", rel.digest(out) != rel.digest(other))


def test_scrubbing_is_idempotent_and_format_scoped():
    print("test_scrubbing_is_idempotent_and_format_scoped")
    once = rel.canonical_bytes("x.step", _STEP_A)
    _check("scrubbing twice changes nothing", rel.canonical_bytes("x.step", once),
           once)
    _check("a format with no clock is passed through untouched",
           rel.canonical_bytes("x.svg", _STEP_A), _STEP_A)
    dxf = b"  9\n$TDCREATE\n 40\n2460892.6688\n  9\n$EXTMIN\n 10\n0.0\n"
    scrubbed = rel.canonical_bytes("x.dxf", dxf)
    _ok("a dxf creation time is zeroed", b"2460892" not in scrubbed, scrubbed)
    _ok("but its geometry variables are not", b"$EXTMIN\n 10\n0.0" in scrubbed,
        scrubbed)


# --- the PRELIMINARY watermark ------------------------------------------------

def test_every_format_carries_the_mark_in_its_own_convention():
    print("test_every_format_carries_the_mark_in_its_own_convention")
    text = rel.WATERMARK_TEXT
    step = rel.stamp_watermark("x.step", _STEP_A, text)
    _ok("step keeps its magic first line", step.startswith(b"ISO-10303-21;\n"), step[:40])
    _ok("and carries a legal comment", b"/* " + text.encode() + b" */" in step)
    dxf = rel.stamp_watermark("x.dxf", b"  0\nSECTION\n", text)
    _ok("dxf uses a group-999 comment", dxf.startswith(b"999\n" + text.encode()), dxf)
    csv = rel.stamp_watermark("x.csv", b"part,count\nA,1\n", text)
    _ok("csv uses a # preamble", csv.startswith(b"# " + text.encode()), csv)
    _ok("and the data survives", b"part,count\nA,1\n" in csv, csv)
    _check("json is left to state it as a field",
           rel.stamp_watermark("x.json", b"{}", text), b"{}")


def test_the_svg_mark_is_a_real_diagonal_stamp():
    print("test_the_svg_mark_is_a_real_diagonal_stamp")
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="420mm" height="297mm" '
           b'viewBox="0 0 420 297"><g id="art"/></svg>\n')
    out = rel.stamp_watermark("x.svg", svg, rel.WATERMARK_TEXT)
    _ok("it is drawn", b'id="ankusdrive-watermark"' in out, out)
    _ok("rotated, so it reads as a stamp not a title", b"rotate(-30" in out)
    _ok("centred on the REAL sheet, not a hard-coded A4",
        b'x="210.00" y="148.50"' in out, out)
    _ok("under the closing tag", out.rstrip().endswith(b"</svg>"), out[-40:])
    _ok("and the artwork is still there", b'<g id="art"/>' in out)
    _check("page size read from the viewBox", rel.svg_page_size(svg), (420.0, 297.0))
    _check("with an A4 fallback when there is none",
           rel.svg_page_size(b"<svg></svg>"), (297.0, 210.0))


def test_an_unmarkable_format_refuses_rather_than_writing_unmarked():
    print("test_an_unmarkable_format_refuses_rather_than_writing_unmarked")
    try:
        rel.stamp_watermark("x.iges", b"data", rel.WATERMARK_TEXT)
    except ValueError as exc:
        _ok("it raises", "refusing" in str(exc), str(exc))
    else:
        _ok("it raises", False, "an unmarked artifact was returned")
    # the PDF is the deliberate exception: it is rendered FROM the marked SVG
    _check("pdf passes through by design",
           rel.stamp_watermark("x.pdf", b"%PDF-1.4", rel.WATERMARK_TEXT), b"%PDF-1.4")


# --- the BOM ------------------------------------------------------------------

def test_bom_csv_carries_its_identity_and_round_trips():
    print("test_bom_csv_carries_its_identity_and_round_trips")
    import csv
    import io
    rows = [{"part": "housing", "count": 2, "total_volume_mm3": 1234.5},
            {"part": "lid, upper", "count": 1, "total_volume_mm3": 99.0}]
    text = rel.bom_csv(rows, part="DP-001001", rev="B", eco="ECO-0007")
    _ok("part recorded", "# Part: DP-001001" in text, text)
    _ok("revision recorded", "# Revision: B" in text)
    _ok("eco recorded", "# ECO: ECO-0007" in text)
    body = [ln for ln in text.splitlines() if not ln.startswith("#")]
    parsed = list(csv.DictReader(io.StringIO("\n".join(body))))
    _check("every line survives", len(parsed), 2)
    _check("the comma in a part name did not split the row",
           parsed[1]["part"], "lid, upper")
    _check("emitted twice, byte-identical", text,
           rel.bom_csv(rows, part="DP-001001", rev="B", eco="ECO-0007"))


def test_bom_columns_leave_the_seam_for_orderable_parts():
    print("test_bom_columns_leave_the_seam_for_orderable_parts")
    # #234 will resolve purchased lines against a curated off-the-shelf catalog and
    # put a canonical designation, a catalog id and a stocked-ness verdict on the
    # rows. They must reach the vendor without a change here, and a column nobody
    # anticipated must be appended rather than dropped.
    plain = [{"part": "housing", "count": 1, "total_volume_mm3": 10.0}]
    _check("today's columns", rel.bom_columns(plain),
           ["part", "count", "total_volume_mm3"])
    stocked = [{"part": "M4x12 SHCS", "count": 8, "designation": "ISO 4762 M4x12",
                "catalog_id": "iso4762-m4-12", "stocked": True}]
    _check("catalog columns land in their reserved order",
           rel.bom_columns(stocked),
           ["part", "count", "designation", "catalog_id", "stocked"])
    # a line the catalog cannot fill carries its nearest alternatives, and the
    # not-stocked verdict has to survive to the vendor — that IS the deliverable
    not_stocked = [{"part": "M4x13 SHCS", "count": 8,
                    "designation": "ISO 4762 M4x13", "catalog_id": None,
                    "stocked": False, "alternatives": "M4x12; M4x16",
                    "corpus_revision": "2026-08"}]
    _check("an unstocked line keeps its alternatives in order",
           rel.bom_columns(not_stocked),
           ["part", "count", "designation", "catalog_id", "stocked",
            "alternatives", "corpus_revision"])
    text = rel.bom_csv(not_stocked)
    _ok("an unanticipated column is appended, never dropped",
        "corpus_revision" in text.splitlines()[1], text)
    _ok("and the not_stocked verdict reaches the vendor", "False" in text, text)

    # quote-time facts are deliberately NOT reserved: a price baked into a released
    # artifact is stale by the time anyone reads it. They still pass through (sorted,
    # at the end) rather than being dropped, but nothing here plans for them.
    for field in ("supplier", "sku", "unit_price_usd", "lead_time_days"):
        _ok(f"{field} is not a reserved release column",
            field not in rel.BOM_COLUMNS, rel.BOM_COLUMNS)


# --- the RFQ flavour ----------------------------------------------------------

def test_quantity_breaks_are_a_comparison_baseline_not_a_price():
    print("test_quantity_breaks_are_a_comparison_baseline_not_a_price")
    tab = rel.quantity_break_table(18800.0, "AL6061-T6", [100, 1, 10])
    _check("ok", tab["ok"], True)
    _check("sorted and deduplicated", [r["quantity"] for r in tab["rows"]],
           [1, 10, 100])
    unit = [r["unit_cost_usd"] for r in tab["rows"]]
    _ok("unit cost falls with quantity (setup amortises)",
        unit[0] > unit[1] > unit[2], unit)
    _ok("extended cost still rises",
        tab["rows"][2]["extended_cost_usd"] > tab["rows"][0]["extended_cost_usd"])
    _check("fidelity is inherited from cost_estimate, not invented",
           tab["fidelity"], "correlation")
    _check("with its band", tab["band_pct"], 100.0)
    _ok("and the basis says what it is not", "not a price" in tab["basis"],
        tab["basis"])


def test_an_unpriceable_material_degrades_rather_than_crashing():
    print("test_an_unpriceable_material_degrades_rather_than_crashing")
    tab = rel.quantity_break_table(1000.0, "Unobtainium", [1, 10])
    _check("not ok", tab["ok"], False)
    _check("no rows invented", tab["rows"], [])
    _ok("with a reason", "Unobtainium" in tab["reason"], tab["reason"])


# --- the manifest is the root of trust ----------------------------------------

def _blobs():
    return [("DP-001001_B.step", rel.STEP, b"step bytes"),
            ("DP-001001_B_bom.csv", rel.BOM_CSV, b"part,count\nhousing,1\n")]


def _manifest(**kw):
    files = [rel.describe_file(n, k, d) for n, k, d in _blobs()]
    kw.setdefault("kinds", ["step", "bom_csv", "manifest_json"])
    kw.setdefault("pages", ["Page"])
    return rel.build_manifest(_ident(), files, **kw)


def test_manifest_pins_the_identity_and_every_checksum():
    print("test_manifest_pins_the_identity_and_every_checksum")
    man = _manifest()
    _check("schema", man["schema"], rel.SCHEMA)
    for field, want in (("item", "plate"), ("part_number", "DP-001001"),
                        ("rev", "B"), ("lifecycle", "released"),
                        ("eco", "ECO-0007"), ("material", "AL6061-T6")):
        _check(f"identity carries {field}", man[field], want)
    _check("checksum algorithm is declared", man["checksum"], "blake2b-256")
    _check("one entry per file", len(man["files"]), 2)
    _check("files sorted by name", [f["name"] for f in man["files"]],
           sorted(f["name"] for f in man["files"]))
    entry = next(f for f in man["files"] if f["kind"] == rel.STEP)
    _check("size recorded", entry["bytes"], len(b"step bytes"))
    _check("checksum is the blake2b of the bytes", entry["blake2b"],
           rel.digest(b"step bytes"))
    _check("a real release is not a draft", man["draft"], False)
    _check("and carries no watermark", man["watermark"], None)


def test_the_manifest_holds_nothing_that_could_vary_on_its_own():
    print("test_the_manifest_holds_nothing_that_could_vary_on_its_own")
    # this is the whole determinism promise in one assertion: build it twice and
    # the serialised bytes must be identical, so a re-release is diffable
    import re
    a = rel.serialize_manifest(_manifest())
    b = rel.serialize_manifest(_manifest())
    _check("byte-identical", a, b)
    # a wall-clock stamp is the one thing that would make two releases of the same
    # revision differ, so no date or clock time may appear anywhere in the bytes
    stamps = re.findall(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}", a)
    _ok("no date or clock time leaked in", not stamps, stamps)
    _check("canonical JSON (sorted keys)", json.loads(a), json.loads(b))
    _ok("keys really are sorted", a.index('"draft"') < a.index('"item"') < a.index('"rev"'),
        a[:400])


def test_a_draft_manifest_says_so_in_a_field_a_parser_sees():
    print("test_a_draft_manifest_says_so_in_a_field_a_parser_sees")
    man = _manifest(draft=True)
    _check("draft flagged", man["draft"], True)
    _check("with the exact watermark text", man["watermark"], rel.WATERMARK_TEXT)


def test_an_rfq_manifest_declares_its_flavour_and_its_fidelity():
    print("test_an_rfq_manifest_declares_its_flavour_and_its_fidelity")
    rfq = rel.quantity_break_table(18800.0, "AL6061-T6", [1, 10])
    man = _manifest(rfq=rfq, dropped=[rel.INSPECTION])
    _check("flavor", man["flavor"], "rfq")
    _check("dropped kinds are on the record", man["dropped_kinds"], [rel.INSPECTION])
    _check("the rollup is labelled", man["rfq"]["fidelity"], "correlation")
    _check("a release has no rfq block", "rfq" in _manifest(), False)
    _check("and calls itself a release", _manifest()["flavor"], "release")


def test_manifest_checksums_catch_a_tampered_package():
    print("test_manifest_checksums_catch_a_tampered_package")
    with tempfile.TemporaryDirectory() as td:
        man = _manifest()
        for name, _kind, data in _blobs():
            Path(td, name).write_bytes(data)
        good = rel.verify_package(man, td)
        _check("an intact package verifies", good["ok"], True)
        _check("and every file was actually read", good["checked"], 2)

        # one byte changed, same length — the size check alone would miss it
        Path(td, "DP-001001_B.step").write_bytes(b"step bytez")
        bad = rel.verify_package(man, td)
        _check("a mutated artifact is caught", bad["ok"], False)
        _check("by checksum, not by size",
               [m["field"] for m in bad["mismatched"]], ["blake2b"])
        _check("and it is named", bad["mismatched"][0]["name"], "DP-001001_B.step")

        os.unlink(os.path.join(td, "DP-001001_B.step"))
        gone = rel.verify_package(man, td)
        _check("a vanished artifact is caught", gone["missing"],
               ["DP-001001_B.step"])
        _check("not silently ok", gone["ok"], False)


def test_the_manifest_is_not_in_its_own_file_list():
    print("test_the_manifest_is_not_in_its_own_file_list")
    # it is the document you verify the OTHERS against; a self-checksum is either
    # impossible or a lie
    man = _manifest()
    _check("no manifest_json entry",
           [f for f in man["files"] if f["kind"] == rel.MANIFEST_JSON], [])


def main():
    for fn in (
        test_identity_reads_the_registry_not_the_caller,
        test_kinds_are_canonical_and_the_manifest_is_not_optional,
        test_inspection_implies_a_drawing_to_balloon,
        test_rfq_drops_the_internal_only_artifacts,
        test_a_released_item_packages_cleanly,
        test_a_draft_item_refuses_without_the_draft_flag,
        test_in_review_is_draftable_too,
        test_an_obsolete_item_refuses_even_as_a_draft,
        test_a_released_item_without_a_revision_refuses,
        test_a_rev_disagreement_refuses_with_a_naming_diff,
        test_a_part_number_disagreement_refuses,
        test_a_material_disagreement_refuses_but_silence_does_not,
        test_a_missing_field_is_a_mismatch_not_an_exemption,
        test_every_disagreement_is_reported_at_once,
        test_an_incomplete_drawing_refuses,
        test_drawing_kinds_need_a_page,
        test_a_kind_the_host_cannot_render_refuses_before_writing,
        test_names_lead_with_the_identity_and_carry_no_date,
        test_names_are_filesystem_safe_and_stable,
        test_the_step_header_is_what_breaks_reproducibility,
        test_the_step_product_name_carries_the_release_identity,
        test_scrubbing_is_idempotent_and_format_scoped,
        test_every_format_carries_the_mark_in_its_own_convention,
        test_the_svg_mark_is_a_real_diagonal_stamp,
        test_an_unmarkable_format_refuses_rather_than_writing_unmarked,
        test_bom_csv_carries_its_identity_and_round_trips,
        test_bom_columns_leave_the_seam_for_orderable_parts,
        test_quantity_breaks_are_a_comparison_baseline_not_a_price,
        test_an_unpriceable_material_degrades_rather_than_crashing,
        test_manifest_pins_the_identity_and_every_checksum,
        test_the_manifest_holds_nothing_that_could_vary_on_its_own,
        test_a_draft_manifest_says_so_in_a_field_a_parser_sees,
        test_an_rfq_manifest_declares_its_flavour_and_its_fidelity,
        test_manifest_checksums_catch_a_tampered_package,
        test_the_manifest_is_not_in_its_own_file_list,
    ):
        fn()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
