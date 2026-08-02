"""Release packages (issue #233) — end-to-end through the FreeCAD worker.

The pure core is unit-tested in test_release_package.py; this drives the real
integration shim against a live document, a real items.json registry and the actual
exporters. It builds the same 60×40×8 plate the drawing-gate and inspection suites
use (Ø12 H7 through hole), registers it as an item, walks it through the lifecycle,
and cuts packages from it.

What it proves that a pure-core test cannot:

  * the golden bundle contains EXACTLY the declared kinds, every manifest checksum
    verifies against the bytes on disk, and a SECOND release of the same revision is
    BYTE-IDENTICAL file for file — the promise that makes a re-release diffable;
  * the gates are two-sided against real state: an in_work item refuses (and writes
    nothing at all), a title block whose rev disagrees with the registry refuses
    with a naming diff, and both pass once the disagreement is fixed;
  * `draft=True` unlocks an unreleased item and marks EVERY artifact — the drawing,
    the STEP header and the CSVs;
  * ECO integration: releasing after lifecycle_apply_change stamps the ECO id into
    the manifest AND into the title block that renders on the sheet;
  * the RFQ flavour adds quantity breaks and drops the internal-only inspection
    package.

svglib/reportlab are optional, so `drawing_pdf` is requested only where the host can
render it; the tests assert on the STEP/SVG/DXF/CSV paths that always work and probe
the PDF path separately.

Run: python3 tests/test_release_package_worker.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402
from driftpin import release as rel  # noqa: E402

_PASS = _FAIL = 0

# The kinds every test below asks for: everything except the PDF, whose renderer is
# optional (test_pdf_kind_is_gated_on_its_renderer covers that path on its own).
KINDS = ["step", "drawing_svg", "drawing_dxf", "bom_csv", "inspection"]


def _check(label, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}  {detail}")


def _eq(label, got, want):
    _check(label, got == want, f"got {got!r}, want {want!r}")


# --- the fixture --------------------------------------------------------------

def _plate(w, name, *, balloon=True):
    """60×40×8 plate, Ø12 H7 through hole at (30, 20), fully dimensioned and (by
    default) ballooned — a print that passes drawing_gate with require_ballooned,
    which is what a release with an inspection package demands."""
    w.call("new_document", name=name)
    plate = w.call("add_primitive", kind="box", w=60, d=40, h=8)
    drill = w.call("add_primitive", kind="cylinder", r=6, h=8, placement=[30, 20, 0])
    part = w.call("boolean_op", op="cut", base=plate["handle"], tool=drill["handle"])
    edges = w.call("list_edges", handle=part["handle"])
    hole = next(e["tag"] for e in edges
                if e.get("radius") and abs(e["radius"] - 6.0) < 1e-6)
    page = w.call("make_drawing_page", name="Page")["handle"]
    w.call("add_projection_group", page=page, body=part["handle"],
           views=["Front", "Top"])
    w.call("add_dimension", page=page, view="Front", kind="horizontal",
           from_point=[0, 0, 0], to_point=[60, 0, 0], tolerance={"sym": 0.2})
    w.call("add_dimension", page=page, view="Front", kind="vertical",
           from_point=[0, 0, 0], to_point=[0, 0, 8], tolerance={"sym": 0.1})
    w.call("add_dimension", page=page, view="Top", kind="vertical",
           from_point=[0, 0, 0], to_point=[0, 40, 0], tolerance={"sym": 0.2})
    w.call("add_dimension", page=page, view="Top", kind="diameter", edge=hole,
           tolerance={"fit": "H7"})
    w.call("add_dimension", page=page, view="Top", kind="horizontal",
           from_point=[0, 20, 0], to_point=[30, 20, 0], tolerance={"sym": 0.1})
    w.call("add_dimension", page=page, view="Top", kind="vertical",
           from_point=[30, 0, 0], to_point=[30, 20, 0], tolerance={"sym": 0.1})
    w.call("fit_page", page=page)
    if balloon:
        w.call("balloon_drawing", page=page)
    return page, part["handle"]


def _register_item(w, registry, *, release=True):
    """Register the plate as an item and (by default) walk it to `released`, which
    stamps its first revision A."""
    res = w.call("items_new", registry=registry, item="plate",
                 files=["plate.FCStd"], metadata={"material": "AL6061-T6"})
    if release:
        w.call("lifecycle_transition", registry=registry, item="plate",
               to="in_review")
        w.call("lifecycle_transition", registry=registry, item="plate",
               to="released")
    return res["part_number"]


def _fixture(w, name, td, *, release=True, rev="A", balloon=True, **title):
    """A released plate with an agreeing title block.

    Returns (registry path, part number, page handle)."""
    page, _part = _plate(w, name, balloon=balloon)
    registry = os.path.join(td, "items.json")
    pn = _register_item(w, registry, release=release)
    fields = {"part": "PLATE", "part_number": pn, "rev": rev,
              "material": "AL6061-T6"}
    fields.update(title)
    w.call("set_title_block", page=page, **fields)
    return registry, pn, page


def _read(td, *parts):
    return Path(td, *parts).read_bytes()


# --- the golden bundle --------------------------------------------------------

def test_golden_package_contains_exactly_the_declared_kinds():
    print("test_golden_package_contains_exactly_the_declared_kinds")
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            registry, pn, page = _fixture(w, "rel_golden", td)
            out = os.path.join(td, "pkg")
            res = w.call("release_package", registry=registry, item="plate",
                         out_dir=out, kinds=KINDS, process="prismatic")
            _check("the package was cut", res["ok"], res["problems"])
            _eq("at the registry's revision", res["rev"], "A")
            _eq("under the registry's part number", res["part_number"], pn)
            _eq("as a real release, not a draft", res["draft"], False)
            _eq("nothing is watermarked", res["watermark"], None)

            # exactly the declared kinds — manifest_json added, nothing else
            _eq("the kind list is the declared one plus its index",
                res["kinds"], KINDS + ["manifest_json"])
            _eq("every kind produced at least one file",
                sorted({f["kind"] for f in res["files"]}), sorted(KINDS))
            on_disk = sorted(os.listdir(out))
            _eq("the directory holds the manifest and nothing else unexpected",
                on_disk,
                sorted([f["name"] for f in res["files"]]
                       + [os.path.basename(res["manifest_path"])]))
            _check("every file name leads with the identity",
                   all(f["name"].startswith(f"{pn}_A") for f in res["files"]),
                   on_disk)


def test_manifest_checksums_verify_against_the_bytes_on_disk():
    print("test_manifest_checksums_verify_against_the_bytes_on_disk")
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            registry, _pn, page = _fixture(w, "rel_verify", td)
            out = os.path.join(td, "pkg")
            res = w.call("release_package", registry=registry, item="plate",
                         out_dir=out, kinds=KINDS, process="prismatic")
            _check("the handler self-verified", res["verify"]["ok"], res["verify"])
            _eq("checking every file", res["verify"]["checked"], len(res["files"]))

            # and independently, from the manifest as it was written to disk
            manifest = json.loads(Path(res["manifest_path"]).read_text(
                encoding="utf-8"))
            again = rel.verify_package(manifest, out)
            _check("an independent verify agrees", again["ok"], again)
            _eq("the manifest is not in its own file list",
                [f for f in manifest["files"] if f["kind"] == "manifest_json"], [])

            # one byte flipped, same length: only the checksum can catch it
            step = next(f for f in manifest["files"] if f["kind"] == "step")
            path = os.path.join(out, step["name"])
            data = bytearray(Path(path).read_bytes())
            data[-3] = data[-3] ^ 0x01
            Path(path).write_bytes(bytes(data))
            bad = rel.verify_package(manifest, out)
            _check("a tampered artifact is caught", not bad["ok"], bad)
            _eq("by checksum", [m["field"] for m in bad["mismatched"]], ["blake2b"])


def test_a_second_release_of_the_same_revision_is_byte_identical():
    print("test_a_second_release_of_the_same_revision_is_byte_identical")
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            registry, _pn, page = _fixture(w, "rel_repro", td)
            a = os.path.join(td, "pkg_a")
            b = os.path.join(td, "pkg_b")
            first = w.call("release_package", registry=registry, item="plate",
                           out_dir=a, kinds=KINDS, process="prismatic")
            second = w.call("release_package", registry=registry, item="plate",
                            out_dir=b, kinds=KINDS, process="prismatic")
            _check("both releases succeeded",
                   first["ok"] and second["ok"], (first["problems"],
                                                  second["problems"]))
            _eq("the same file set", sorted(os.listdir(a)), sorted(os.listdir(b)))
            for name in sorted(os.listdir(a)):
                _eq(f"byte-identical: {name}",
                    Path(a, name).read_bytes(), Path(b, name).read_bytes())
            # the checksums are the point: a re-release is diffable by manifest
            _eq("every checksum matches",
                {f["name"]: f["blake2b"] for f in first["files"]},
                {f["name"]: f["blake2b"] for f in second["files"]})
            # the STEP is where the clock leaks would be, so name it explicitly
            step = next(f["name"] for f in first["files"] if f["kind"] == "step")
            raw = _read(a, step)
            _check("no export instant survived in the STEP header",
                   rel.EPOCH.encode() in raw, raw[:400])
            _check("nor OpenCASCADE's per-session export counter",
                   b"Open CASCADE STEP translator" not in raw, raw[:800])
            _check("the STEP self-identifies instead",
                   b"Rev A" in raw, raw[:800])


# --- the gates, two-sided, against real state ---------------------------------

def test_an_unreleased_item_refuses_and_writes_nothing():
    print("test_an_unreleased_item_refuses_and_writes_nothing")
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            registry, _pn, page = _fixture(w, "rel_draft", td, release=False, rev="-")
            out = os.path.join(td, "pkg")
            res = w.call("release_package", registry=registry, item="plate",
                         out_dir=out, kinds=KINDS, process="prismatic")
            _check("refused", not res["ok"], res)
            _eq("for the lifecycle reason",
                sorted(p["code"] for p in res["problems"]), ["not_released"])
            _eq("naming diff: expected released",
                res["problems"][0]["expected"], "released")
            _eq("naming diff: actual in_work", res["problems"][0]["actual"],
                "in_work")
            # the whole point of gating BEFORE writing: no half-package is left for
            # a build script to pick up
            _check("nothing was written", not os.path.exists(out), os.path.exists(out))
            _eq("and no files are claimed", res["files"], [])


def test_a_title_block_rev_disagreement_refuses_with_a_naming_diff():
    print("test_a_title_block_rev_disagreement_refuses_with_a_naming_diff")
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            # the classic failure: the item is at Rev A, the print still says "-"
            registry, pn, page = _fixture(w, "rel_revdiff", td, rev="-")
            out = os.path.join(td, "pkg")
            res = w.call("release_package", registry=registry, item="plate",
                         out_dir=out, kinds=KINDS, process="prismatic")
            _check("refused", not res["ok"], res)
            prob = next(p for p in res["problems"] if p.get("field") == "rev")
            _eq("code", prob["code"], "title_block_mismatch")
            _eq("expected is the registry's revision", prob["expected"], "A")
            _eq("actual is the print's", prob["actual"], "-")
            _eq("naming the page", prob["where"], "Page")
            _check("nothing was written", not os.path.exists(out))

            # fix ONLY the title block and the same call now passes
            w.call("set_title_block", page=page, part="PLATE",
                   part_number=pn, rev="A", material="AL6061-T6")
            fixed = w.call("release_package", registry=registry, item="plate",
                           out_dir=out, kinds=KINDS, process="prismatic")
            _check("re-stamping the print is all it took", fixed["ok"],
                   fixed["problems"])


def test_a_material_disagreement_refuses():
    print("test_a_material_disagreement_refuses")
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            # the registry says 6061; the print calls for 7075. Someone has to
            # decide which — the release will not pick one silently.
            registry, _pn, page = _fixture(w, "rel_matdiff", td, material="AL7075-T6")
            res = w.call("release_package", registry=registry, item="plate",
                         out_dir=os.path.join(td, "pkg"), kinds=KINDS,
                         process="prismatic")
            _check("refused", not res["ok"], res)
            prob = next(p for p in res["problems"] if p.get("field") == "material")
            _eq("expected is the registry's material", prob["expected"],
                "AL6061-T6")
            _eq("actual is the print's", prob["actual"], "AL7075-T6")


def test_an_unballooned_print_refuses_an_inspection_package():
    print("test_an_unballooned_print_refuses_an_inspection_package")
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            # the same fixture with the balloons left off: an inspection package
            # keyed to balloon numbers the print does not carry is useless to a
            # quality engineer, so the release must refuse rather than ship it
            registry, _pn, page = _fixture(w, "rel_noballoon", td, balloon=False)
            out = os.path.join(td, "pkg")
            res = w.call("release_package", registry=registry, item="plate",
                         out_dir=out, kinds=["step", "inspection"],
                         process="prismatic")
            _check("refused", not res["ok"], res)
            _eq("for the drawing-gate reason",
                [p["code"] for p in res["problems"]], ["drawing_gate"])
            _eq("naming the unballooned violation",
                res["problems"][0]["actual"], ["not_ballooned"])
            _check("nothing was written", not os.path.exists(out))

            # a package that does NOT ask for inspection is unaffected
            plain = w.call("release_package", registry=registry, item="plate",
                           out_dir=os.path.join(td, "plain"),
                           kinds=["step", "drawing_svg"], process="prismatic")
            _check("an unballooned print still releases without it", plain["ok"],
                   plain["problems"])

            # ballooning the print is all it takes for the inspection kind to pass
            w.call("balloon_drawing", page=page)
            fixed = w.call("release_package", registry=registry, item="plate",
                           out_dir=out, kinds=["step", "inspection"],
                           process="prismatic")
            _check("ballooning the print unblocks it", fixed["ok"],
                   fixed["problems"])
            # the ballooned print, the plan and the blank AS9102 form
            _eq("and the inspection package is really there",
                len([f for f in fixed["files"] if f["kind"] == "inspection"]), 2)
            _check("with the ballooned drawing it references",
                   any(f["kind"] == "drawing_svg" for f in fixed["files"]),
                   fixed["files"])


def test_an_obsolete_item_refuses_even_as_a_draft():
    print("test_an_obsolete_item_refuses_even_as_a_draft")
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            registry, _pn, page = _fixture(w, "rel_obsolete", td)
            w.call("lifecycle_transition", registry=registry, item="plate",
                   to="obsolete")
            for label, draft in (("as a release", False), ("as a draft", True)):
                res = w.call("release_package", registry=registry, item="plate",
                             out_dir=os.path.join(td, f"pkg_{draft}"),
                             kinds=["step"], draft=draft)
                _check(f"refused {label}", not res["ok"], res)
                _eq(f"code {label}", [p["code"] for p in res["problems"]],
                    ["not_releasable"])


# --- the PRELIMINARY watermark ------------------------------------------------

def test_draft_marks_every_artifact_not_just_the_drawing():
    print("test_draft_marks_every_artifact_not_just_the_drawing")
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            registry, _pn, page = _fixture(w, "rel_prelim", td, release=False, rev="-")
            out = os.path.join(td, "pkg")
            res = w.call("release_package", registry=registry, item="plate",
                         out_dir=out, kinds=KINDS, draft=True, process="prismatic")
            _check("draft=True unlocks an in_work item", res["ok"], res["problems"])
            _eq("the revision is the un-released sentinel", res["rev"], "-")
            _eq("the package says it is a draft", res["draft"], True)
            _eq("with the watermark text", res["watermark"], rel.WATERMARK_TEXT)

            mark = rel.WATERMARK_TEXT.encode("utf-8")
            by_kind = {}
            for f in res["files"]:
                by_kind.setdefault(f["kind"], []).append(f["name"])
            svg = _read(out, by_kind["drawing_svg"][0])
            _check("the drawing carries a real diagonal stamp",
                   b'id="driftpin-watermark"' in svg and mark in svg, svg[:200])
            step = _read(out, by_kind["step"][0])
            _check("the STEP carries it as a legal comment",
                   b"/* " + mark in step, step[:200])
            _check("and is still a valid STEP",
                   step.startswith(b"ISO-10303-21;\n/* "), step[:60])
            dxf = _read(out, by_kind["drawing_dxf"][0])
            _check("the DXF carries a group-999 comment",
                   dxf.startswith(b"999\n" + mark), dxf[:100])
            bom = _read(out, by_kind["bom_csv"][0])
            _check("the BOM carries a # preamble", bom.startswith(b"# " + mark),
                   bom[:100])
            fai = _read(out, sorted(by_kind["inspection"])[0])
            _check("so does the inspection package", mark in fai, fai[:200])

            manifest = json.loads(Path(res["manifest_path"]).read_text(
                encoding="utf-8"))
            _eq("and the manifest states it as a field a parser sees",
                manifest["draft"], True)
            _eq("with the same text", manifest["watermark"], rel.WATERMARK_TEXT)


def test_a_real_release_carries_no_mark_anywhere():
    print("test_a_real_release_carries_no_mark_anywhere")
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            registry, _pn, page = _fixture(w, "rel_clean", td)
            out = os.path.join(td, "pkg")
            res = w.call("release_package", registry=registry, item="plate",
                         out_dir=out, kinds=KINDS, process="prismatic")
            _check("released", res["ok"], res["problems"])
            mark = b"PRELIMINARY"
            offenders = [f["name"] for f in res["files"]
                         if mark in _read(out, f["name"])]
            _eq("no artifact says PRELIMINARY", offenders, [])


# --- ECO integration ----------------------------------------------------------

def test_releasing_after_apply_change_stamps_the_eco():
    print("test_releasing_after_apply_change_stamps_the_eco")
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            registry, pn, page = _fixture(w, "rel_eco", td)
            # an F3-preserving change on the released item: same part number,
            # rev A -> B, back to in_work
            changed = w.call("lifecycle_apply_change", registry=registry,
                             item="plate",
                             after={"material": "AL6061-T6", "finish": "anodized"},
                             note="loosen the internal pocket")
            _eq("classified as a revision, not a renumber",
                changed["disposition"], "revise")
            _eq("same part number", changed["part_number"], pn)
            _eq("bumped revision", changed["rev"], "B")
            w.call("lifecycle_transition", registry=registry, item="plate",
                   to="in_review")
            w.call("lifecycle_transition", registry=registry, item="plate",
                   to="released")
            # re-stamp the print at the new revision, as an engineer would
            w.call("set_title_block", page=page, part="PLATE", part_number=pn,
                   rev="B", material="AL6061-T6")

            out = os.path.join(td, "pkg")
            res = w.call("release_package", registry=registry, item="plate",
                         out_dir=out, kinds=KINDS, eco="ECO-0007",
                         process="prismatic")
            _check("released at rev B", res["ok"], res["problems"])
            _eq("at the bumped revision", res["rev"], "B")
            _eq("the ECO is on the result", res["eco"], "ECO-0007")

            manifest = json.loads(Path(res["manifest_path"]).read_text(
                encoding="utf-8"))
            _eq("and stamped into the manifest", manifest["eco"], "ECO-0007")
            _eq("next to the revision it belongs to", manifest["rev"], "B")

            svg = _read(out, next(f["name"] for f in res["files"]
                                  if f["kind"] == "drawing_svg")).decode("utf-8")
            _check("and rendered into the title block on the sheet",
                   "ECO-0007" in svg, svg[-1500:])
            _check("in the REV cell", "REV / ECO" in svg, svg[-1500:])
            bom = _read(out, next(f["name"] for f in res["files"]
                                  if f["kind"] == "bom_csv")).decode("utf-8")
            _check("the BOM names the same change order", "# ECO: ECO-0007" in bom,
                   bom)
            _check("every file is now named for rev B",
                   all("_B" in f["name"] for f in res["files"]),
                   [f["name"] for f in res["files"]])


def test_the_eco_defaults_to_the_items_metadata():
    print("test_the_eco_defaults_to_the_items_metadata")
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            page, _part = _plate(w, "rel_ecometa")
            registry = os.path.join(td, "items.json")
            res = w.call("items_new", registry=registry, item="plate",
                         files=["plate.FCStd"],
                         metadata={"material": "AL6061-T6", "eco": "ECO-0042"})
            pn = res["part_number"]
            w.call("lifecycle_transition", registry=registry, item="plate",
                   to="in_review")
            w.call("lifecycle_transition", registry=registry, item="plate",
                   to="released")
            w.call("set_title_block", page=page, part="PLATE", part_number=pn,
                   rev="A", material="AL6061-T6")
            out = w.call("release_package", registry=registry, item="plate",
                         out_dir=os.path.join(td, "pkg"), kinds=["step", "bom_csv"],
                         process="prismatic")
            _check("released", out["ok"], out["problems"])
            _eq("the item's recorded ECO was picked up", out["eco"], "ECO-0042")
            _eq("an explicit one still wins",
                w.call("release_package", registry=registry, item="plate",
                       out_dir=os.path.join(td, "pkg2"), kinds=["step"],
                       eco="ECO-0099", process="prismatic")["eco"], "ECO-0099")


# --- the RFQ flavour ----------------------------------------------------------

def test_rfq_adds_quantity_breaks_and_drops_internal_artifacts():
    print("test_rfq_adds_quantity_breaks_and_drops_internal_artifacts")
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            registry, _pn, page = _fixture(w, "rel_rfq", td)
            out = os.path.join(td, "pkg")
            res = w.call("release_package", registry=registry, item="plate",
                         out_dir=out, kinds=KINDS, rfq=True,
                         quantity_breaks=[1, 25, 250], process="prismatic")
            _check("the RFQ was cut", res["ok"], res["problems"])
            _eq("it declares its flavour", res["flavor"], "rfq")
            _eq("the inspection package is dropped", res["dropped_kinds"],
                ["inspection"])
            _eq("and no inspection file was written",
                [f for f in res["files"] if f["kind"] == "inspection"], [])
            _check("but the geometry and the print still ship",
                   {"step", "drawing_svg", "drawing_dxf", "bom_csv"}
                   <= {f["kind"] for f in res["files"]},
                   {f["kind"] for f in res["files"]})

            rfq = res["manifest"]["rfq"]
            _check("the cost rollup landed", rfq["ok"], rfq)
            _eq("one row per requested break",
                [r["quantity"] for r in rfq["rows"]], [1, 25, 250])
            unit = [r["unit_cost_usd"] for r in rfq["rows"]]
            _check("unit cost falls with quantity", unit[0] > unit[1] > unit[2], unit)
            _eq("priced against the item's declared material", rfq["material"],
                "AL6061-T6")
            # fidelity discipline: this is a comparison baseline, not a quote
            _eq("labelled correlation", rfq["fidelity"], "correlation")
            _eq("with its band", rfq["band_pct"], 100.0)
            _check("and says what it is not", "not a price" in rfq["basis"],
                   rfq["basis"])


def test_an_unpriceable_rfq_still_ships_its_geometry():
    print("test_an_unpriceable_rfq_still_ships_its_geometry")
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            page, _part = _plate(w, "rel_nomat")
            registry = os.path.join(td, "items.json")
            # no material in the metadata: nothing to price against, and nothing
            # for the title block to contradict either
            res = w.call("items_new", registry=registry, item="plate",
                         files=["plate.FCStd"], metadata={})
            pn = res["part_number"]
            w.call("lifecycle_transition", registry=registry, item="plate",
                   to="in_review")
            w.call("lifecycle_transition", registry=registry, item="plate",
                   to="released")
            w.call("set_title_block", page=page, part="PLATE", part_number=pn,
                   rev="A")
            out = w.call("release_package", registry=registry, item="plate",
                         out_dir=os.path.join(td, "pkg"),
                         kinds=["step", "bom_csv"], rfq=True, process="prismatic")
            _check("the package is still cut", out["ok"], out["problems"])
            rfq = out["manifest"]["rfq"]
            _eq("but the rollup degrades rather than crashing", rfq["ok"], False)
            _check("with a reason", "no material" in rfq["reason"], rfq["reason"])
            _check("and the geometry is there anyway",
                   any(f["kind"] == "step" for f in out["files"]), out["files"])


# --- the optional PDF renderer ------------------------------------------------

def test_pdf_kind_is_gated_on_its_renderer():
    print("test_pdf_kind_is_gated_on_its_renderer")
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            registry, _pn, page = _fixture(w, "rel_pdf", td)
            out = os.path.join(td, "pkg")
            res = w.call("release_package", registry=registry, item="plate",
                         out_dir=out, kinds=["step", "drawing_pdf"],
                         process="prismatic")
            if res["ok"]:
                pdf = next(f for f in res["files"] if f["kind"] == "drawing_pdf")
                _check("a real pdf was written",
                       _read(out, pdf["name"]).startswith(b"%PDF"), pdf)
            else:
                # svglib/reportlab absent: the gate must refuse BEFORE writing, not
                # ship a bundle quietly missing the sheet a human was to read
                _eq("refused for the capability reason",
                    [p["code"] for p in res["problems"]], ["kind_unavailable"])
                _check("with an install hint",
                       "pip install" in res["problems"][0]["reason"],
                       res["problems"][0]["reason"])
                _check("and nothing was written", not os.path.exists(out))


# --- assembly BOM -------------------------------------------------------------

def test_an_assembly_releases_a_recursive_bom():
    print("test_an_assembly_releases_a_recursive_bom")
    import csv
    import io
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            w.call("new_document", name="rel_asm")
            box = w.call("add_primitive", kind="box", w=20, d=20, h=5)
            pin = w.call("add_primitive", kind="cylinder", r=2, h=10)
            asm = w.call("make_assembly", name="Asm")["handle"]
            w.call("add_part", assembly=asm, source={"handle": box["handle"]},
                   name="Base")
            for i in range(3):
                w.call("add_part", assembly=asm, source={"handle": pin["handle"]},
                       placement=[i * 5.0, 0, 5], name=f"Pin{i}")
            registry = os.path.join(td, "items.json")
            pn = _register_item(w, registry)
            out = os.path.join(td, "pkg")
            res = w.call("release_package", registry=registry, item="plate",
                         out_dir=out, kinds=["step", "bom_csv"], handle=asm)
            _check("the assembly package was cut", res["ok"], res["problems"])
            text = _read(out, next(f["name"] for f in res["files"]
                                   if f["kind"] == "bom_csv")).decode("utf-8")
            _check("the BOM names the part number", f"# Part: {pn}" in text, text)
            body = [ln for ln in text.splitlines() if not ln.startswith("#")]
            rows = list(csv.DictReader(io.StringIO("\n".join(body))))
            counts = {r["part"]: int(r["count"]) for r in rows}
            _check("the three identical pins are one line of qty 3",
                   3 in counts.values(), counts)
            _check("and the base is its own line", len(rows) >= 2, counts)


def main():
    for fn in (
        test_golden_package_contains_exactly_the_declared_kinds,
        test_manifest_checksums_verify_against_the_bytes_on_disk,
        test_a_second_release_of_the_same_revision_is_byte_identical,
        test_an_unreleased_item_refuses_and_writes_nothing,
        test_a_title_block_rev_disagreement_refuses_with_a_naming_diff,
        test_a_material_disagreement_refuses,
        test_an_unballooned_print_refuses_an_inspection_package,
        test_an_obsolete_item_refuses_even_as_a_draft,
        test_draft_marks_every_artifact_not_just_the_drawing,
        test_a_real_release_carries_no_mark_anywhere,
        test_releasing_after_apply_change_stamps_the_eco,
        test_the_eco_defaults_to_the_items_metadata,
        test_rfq_adds_quantity_breaks_and_drops_internal_artifacts,
        test_an_unpriceable_rfq_still_ships_its_geometry,
        test_pdf_kind_is_gated_on_its_renderer,
        test_an_assembly_releases_a_recursive_bom,
    ):
        fn()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
