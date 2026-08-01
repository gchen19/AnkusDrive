"""
Release packages (issue #233): the one-call vendor/RFQ bundle — STEP + drawings +
BOM + manifest — gated by lifecycle and ECO.

Every piece a vendor needs already exists as its own tool (``export_shape``,
``export_drawing``, ``bom_extract``, ``set_title_block``, the items registry,
``lifecycle_transition``, ``eco_*``). What did not exist is the guarantee that ties
them together: **that the STEP, the PDF, the BOM and the title block all describe
the same revision of the same item.** Hand-assembled bundles get that wrong quietly
— a re-exported STEP next to last week's drawing, a print whose title block still
says Rev A. This module is the gate that makes it impossible.

FreeCAD-free on purpose, exactly like :mod:`driftpin.drawing_gate` and
:mod:`driftpin.inspection`: manifest composition, checksums, the releasable/
title-block/drawing gates, the determinism scrub and the RFQ shaping are all plain
data, so they unit-test on the host interpreter in milliseconds without a CAD
kernel. The worker shim (``release_package``) drives the real exporters and
delegates every *decision* here.

Four ideas carry the module:

* **Gates run BEFORE anything is written.** A package that refuses must leave no
  half-written directory a downstream script could mistake for a release, so the
  worker collects the page/title-block/lifecycle facts, runs :func:`release_gate`,
  and only then opens a file. A mismatch is a FAILURE with a naming diff, never a
  silent fix — quietly rewriting a title block to agree with the registry would
  destroy the only independent check that the print and the item describe the same
  thing.
* **Same item at the same revision ⇒ byte-identical package.** The recipes layer
  already promises same-inputs→same-bytes geometry; the exporters break that
  promise by stamping wall-clock time into their headers. :func:`canonical_bytes`
  scrubs exactly those fields (the STEP ``FILE_NAME`` timestamp is the classic) so
  a re-released package is diffable by checksum.
* **A draft package must be un-mistakable for a released one.** ``draft=True`` is
  the escape hatch for releasing an unreleased item, and it costs a
  ``PRELIMINARY`` mark on *every* artifact — burned into the SVG/PDF, and carried
  as a format-legal comment in the STEP, the DXF and the CSVs
  (:func:`stamp_watermark`), because a STEP that escapes the bundle still has to
  say what it is.
* **The manifest is the root of trust.** It carries the identity (item, part
  number, revision, lifecycle state, ECO) and a blake2b checksum per file. It is
  deliberately not self-checksummed — it is the thing you verify the others
  against.

Nothing here estimates anything, with one exception: the RFQ quantity-break table
is a :func:`driftpin.analysis.cost.cost_estimate` rollup, and it inherits that
tool's ``fidelity="correlation"`` / ``band_pct=100`` label verbatim. It is a
quote-comparison *baseline*, not a price.
"""
import hashlib
import json
import os
import re

SCHEMA = "driftpin.release/1"

# --- artifact kinds -----------------------------------------------------------

STEP = "step"                    # part/assembly geometry, the vendor's model
DRAWING_PDF = "drawing_pdf"      # the printable sheet
DRAWING_SVG = "drawing_svg"      # the same sheet as vector source
DRAWING_DXF = "drawing_dxf"      # profile geometry a laser/punch/CAM seat imports
BOM_CSV = "bom_csv"              # the recursive bill of materials
INSPECTION = "inspection"        # ballooned print + plan + blank AS9102 form
MANIFEST_JSON = "manifest_json"  # the index + checksums

KINDS = (STEP, DRAWING_PDF, DRAWING_SVG, DRAWING_DXF, BOM_CSV, INSPECTION,
         MANIFEST_JSON)

# What a vendor package contains when the caller doesn't say. DXF and PDF are both
# in: the PDF is what a human reads, the DXF is what a machine imports, and a
# fabricator asked for "the drawing" means both.
DEFAULT_KINDS = (STEP, DRAWING_PDF, DRAWING_DXF, BOM_CSV, MANIFEST_JSON)

# Dropped from an RFQ bundle. The inspection package states the ACCEPTANCE
# CRITERIA — which characteristics get measured, with what instrument, to what
# limits. That is the buyer's internal document at quote time; handing it to every
# bidder tells them exactly which features they will be judged on before anyone has
# won the work. It ships with the purchase order, not with the request for quote.
INTERNAL_ONLY_KINDS = frozenset({INSPECTION})

# --- lifecycle gating ---------------------------------------------------------

# The states a package may be cut from without an explicit draft flag. Only
# `released` — that is what "released" MEANS, and it is the state lifecycle.py
# freezes the item at a revision in.
RELEASABLE_STATES = ("released",)

# States `draft=True` unlocks, at the cost of a PRELIMINARY mark on every artifact.
DRAFT_STATES = ("in_work", "in_review")

# `obsolete` is unlocked by nothing. A retired part is not something a vendor
# should ever be quoting or cutting, and a watermark saying "preliminary" would be
# an actively wrong description of it.

WATERMARK_TEXT = "PRELIMINARY — NOT FOR PRODUCTION"

# --- determinism --------------------------------------------------------------

# The fixed instant every scrubbed export header is rewritten to. Value is
# irrelevant as long as it never changes; the Unix epoch is the conventional
# "deliberately not now".
EPOCH = "1970-01-01T00:00:00"

# blake2b-256. Wider than the 128-bit fingerprint change.py pins baselines with,
# because a release manifest is the artifact that leaves the building: a vendor
# re-verifies against it months later with no access to the registry that produced
# it, so it gets the full-strength digest.
DIGEST_SIZE = 32
DIGEST_NAME = "blake2b-256"


# === identity =================================================================

def identity(item_id, record, *, eco=None):
    """The identity block every artifact in the package must agree with.

    Reads it off the item registry record (:mod:`driftpin.items`), which is the
    single source of truth: the part number was allocated there, the revision was
    stamped there by ``lifecycle_transition``, and the material lives in the item's
    queryable metadata. ``eco`` overrides the item's recorded ECO reference (the
    change order this release is cut under).

    Returns ``{item, part_number, rev, lifecycle, material, eco}``; ``material``
    and ``eco`` are None when the registry doesn't declare them."""
    rec = record or {}
    meta = rec.get("metadata") or {}
    return {
        "item": item_id,
        "part_number": rec.get("part_number"),
        "rev": rec.get("rev", "-"),
        "lifecycle": rec.get("lifecycle", "in_work"),
        "material": meta.get("material"),
        "eco": eco if eco is not None else meta.get("eco"),
    }


DRAWING_KINDS = (DRAWING_PDF, DRAWING_SVG, DRAWING_DXF)


def resolve_kinds(kinds=None, *, rfq=False):
    """Normalise the requested artifact kinds.

    Returns ``{kinds, dropped, implied}``. The result is in canonical :data:`KINDS`
    order (not the caller's) and deduplicated, so the kind list — and therefore the
    file set — never depends on how the request was typed. Two additions the caller
    doesn't have to remember:

    * ``manifest_json`` is always present. A bundle without its index cannot be
      verified, so it is not an optional kind.
    * ``inspection`` implies a drawing kind. The inspection deliverable is a
      *ballooned print* plus its plan; asking for the plan without the print it
      references would ship an inspector a list of balloon numbers and nothing to
      find them on. ``drawing_svg`` is the implied one (it is the format DriftPin
      composes natively and needs no external renderer).

    ``rfq=True`` removes :data:`INTERNAL_ONLY_KINDS` and reports them in
    ``dropped`` rather than dropping them silently.

    An unknown kind raises ValueError: a typo'd kind that produced a package
    quietly missing an artifact is precisely the hand-assembly failure this tool
    exists to end."""
    want = list(DEFAULT_KINDS if kinds is None else kinds)
    unknown = sorted({k for k in want if k not in KINDS})
    if unknown:
        raise ValueError(
            f"unknown release kind(s) {unknown}; choose from {list(KINDS)}")
    want = set(want) | {MANIFEST_JSON}
    dropped = []
    if rfq:
        dropped = sorted(want & INTERNAL_ONLY_KINDS)
        want -= INTERNAL_ONLY_KINDS
    implied = []
    if INSPECTION in want and not (want & set(DRAWING_KINDS)):
        want.add(DRAWING_SVG)
        implied.append(DRAWING_SVG)
    return {"kinds": [k for k in KINDS if k in want], "dropped": dropped,
            "implied": implied}


def needs_pages(kinds):
    """True iff any requested kind is produced from a drawing page."""
    return bool(set(kinds) & {DRAWING_PDF, DRAWING_SVG, DRAWING_DXF, INSPECTION})


# === the gates ================================================================
#
# Every gate returns a list of PROBLEM dicts rather than raising, because a release
# attempt should report all of them at once — an agent that has to fix a title
# block, re-ballon a print and re-release three times to discover three problems is
# an agent that will stop using the gate. Shape:
#
#     {code, where, field?, expected?, actual?, reason}
#
# `code` is machine-branchable, `reason` is what a human reads, and expected/actual
# are the naming diff the issue asks for.

def _problem(code, reason, *, where="", field=None, expected=None, actual=None):
    out = {"code": code, "where": where, "reason": reason}
    if field is not None:
        out["field"] = field
    if expected is not None or actual is not None:
        out["expected"] = expected
        out["actual"] = actual
    return out


def lifecycle_problems(ident, *, draft=False):
    """Is this item releasable at all?

    ``released`` always is. ``in_work`` / ``in_review`` are only with ``draft=True``
    — and then every artifact is watermarked. ``obsolete`` never is: a retired part
    must not be quoted or cut, and "preliminary" would be a wrong description of it
    rather than a warning."""
    state = ident.get("lifecycle")
    problems = []
    if state in RELEASABLE_STATES:
        if ident.get("rev") in ("-", "", None):
            problems.append(_problem(
                "no_revision", "item is released but carries no revision — a "
                "released milestone must be releasable AT a revision; the package "
                "would have nothing to name its files after",
                field="rev", expected="a revision", actual=ident.get("rev")))
        return problems
    if state in DRAFT_STATES:
        if not draft:
            problems.append(_problem(
                "not_released",
                f"item is {state!r}, not {list(RELEASABLE_STATES)} — release it "
                "first, or pass draft=True to cut a PRELIMINARY package that says "
                "so on every artifact",
                field="lifecycle", expected="released", actual=state))
        return problems
    problems.append(_problem(
        "not_releasable",
        f"item is {state!r}; a package cannot be cut from it in any mode "
        "(draft=True does not unlock an obsolete part — it is retired, not "
        "preliminary)",
        field="lifecycle", expected="released", actual=state))
    return problems


def title_block_problems(ident, fields, *, where=""):
    """Does the print's title block describe the same thing the registry does?

    This is the check nothing else in the toolchain performs, and the reason the
    whole module exists. Compared:

    * **part number** — the title block's ``part_number`` field, or its
      ``part`` field when that is all there is. A vendor keys off this string; if
      it doesn't equal the allocated part number the drawing is documenting some
      other part.
    * **revision** — the title block's ``rev`` against the item's. This is the one
      that goes wrong in real life: the model gets revised, the print doesn't get
      re-stamped, and the package ships a Rev B STEP with a Rev A drawing.
    * **material** — only when the registry declares one. A registry that is silent
      about material has nothing to disagree with, so a stated material is not a
      contradiction. A registry that DOES declare one and a print that says
      something else is a real conflict about what to cut the part from.

    A missing field is a mismatch, not an exemption: an untitled print is exactly
    as ambiguous as a wrongly-titled one."""
    if not fields:
        return [_problem(
            "no_title_block",
            "page has no title block — nothing on the sheet states which part "
            "number and revision it documents; call set_title_block before release",
            where=where)]
    problems = []
    pn = fields.get("part_number") or fields.get("part")
    if pn != ident.get("part_number"):
        problems.append(_problem(
            "title_block_mismatch",
            f"title block names part {pn!r}; the registry allocated "
            f"{ident.get('part_number')!r} for item {ident.get('item')!r}",
            where=where, field="part_number",
            expected=ident.get("part_number"), actual=pn))
    if fields.get("rev") != ident.get("rev"):
        problems.append(_problem(
            "title_block_mismatch",
            f"title block says revision {fields.get('rev')!r}; the item is at "
            f"revision {ident.get('rev')!r} — the print and the model describe "
            "different revisions",
            where=where, field="rev",
            expected=ident.get("rev"), actual=fields.get("rev")))
    want_mat = ident.get("material")
    if want_mat is not None and fields.get("material") != want_mat:
        problems.append(_problem(
            "title_block_mismatch",
            f"title block calls for {fields.get('material')!r}; the item's "
            f"metadata declares {want_mat!r}",
            where=where, field="material",
            expected=want_mat, actual=fields.get("material")))
    return problems


def drawing_problems(gate, *, where=""):
    """Turn a ``drawing_gate`` report into release problems.

    An incomplete drawing is not releasable: a fabricator who cannot reconstruct
    the part from the dimension set will either ask (delay) or guess (scrap). The
    gate's own violation codes are carried through verbatim so the fix is the same
    one ``drawing_gate`` would have told them about."""
    if gate is None:
        return [_problem("no_drawing_gate",
                         "page was not gated; a release requires drawing_gate to "
                         "pass for every included page", where=where)]
    if gate.get("ok"):
        return []
    codes = sorted({str(v.get("code")) for v in gate.get("violations") or []})
    return [_problem(
        "drawing_gate",
        f"drawing_gate fails on this page ({len(gate.get('violations') or [])} "
        f"violation(s): {', '.join(codes)}) — the print does not fully specify "
        "the part",
        where=where, field="violations", expected=[], actual=codes)]


def capability_problems(kinds, capabilities):
    """Refuse up front for a kind this host cannot actually produce.

    ``capabilities`` maps a kind to ``{ok, reason, install?}`` — the worker probes
    them (``drawing_pdf`` needs svglib + reportlab, which are optional). Checked
    with the other gates rather than discovered mid-write, so a host without a PDF
    renderer refuses the package instead of shipping a bundle that is quietly
    missing the sheet a human was supposed to read."""
    problems = []
    for kind in kinds:
        cap = (capabilities or {}).get(kind)
        if cap is None or cap.get("ok"):
            continue
        reason = cap.get("reason") or f"{kind} is unavailable on this host"
        if cap.get("install"):
            reason += f" — install: {cap['install']}"
        problems.append(_problem("kind_unavailable", reason, field="kind",
                                 expected=kind, actual=None))
    return problems


def release_gate(ident, *, kinds, pages=(), draft=False, capabilities=None):
    """The whole pre-flight, in one call: may this package be written?

    ``pages`` is one entry per drawing page bound to the item, each
    ``{page, title_block, gate}`` — the page's name, its title-block fields (or
    None), and its ``drawing_gate`` report (or None). The worker reads those off
    the live document; every judgement about them is made here. ``capabilities``
    reports which kinds this host can render (see :func:`capability_problems`).

    Returns ``{ok, problems, watermark, draft, checked_pages}``. ``watermark`` is
    the text every artifact must carry (None for a real release). ``ok`` is True
    only when ``problems`` is empty — and the worker writes nothing at all until
    then, so a refused release leaves no partial directory behind."""
    problems = list(lifecycle_problems(ident, draft=draft))
    problems += capability_problems(kinds, capabilities)
    page_list = list(pages)
    if needs_pages(kinds) and not page_list:
        problems.append(_problem(
            "no_pages",
            f"kinds {sorted(set(kinds) & {DRAWING_PDF, DRAWING_SVG, DRAWING_DXF, INSPECTION})} "
            "need a drawing page, but no page is bound to this item"))
    for entry in page_list:
        where = str(entry.get("page", ""))
        problems += title_block_problems(ident, entry.get("title_block"),
                                         where=where)
        problems += drawing_problems(entry.get("gate"), where=where)
    return {
        "ok": not problems,
        "problems": problems,
        "draft": bool(draft),
        "watermark": WATERMARK_TEXT if draft else None,
        "checked_pages": [str(e.get("page", "")) for e in page_list],
    }


# === artifact naming ==========================================================

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(text):
    """A filesystem-safe, deterministic token. Collapses every run of unsafe
    characters to a single underscore so two names differing only in punctuation
    can't collide silently on one and diverge on another platform."""
    return _UNSAFE.sub("_", str(text)).strip("_") or "x"


# kind -> (suffix, extension). The page name is interpolated for the per-page
# kinds; `inspection` produces two files per page and is handled by name below.
_NAME_RULES = {
    STEP: ("", ".step"),
    DRAWING_PDF: ("", ".pdf"),
    DRAWING_SVG: ("", ".svg"),
    DRAWING_DXF: ("", ".dxf"),
    BOM_CSV: ("bom", ".csv"),
    MANIFEST_JSON: ("manifest", ".json"),
}


def artifact_name(part_number, rev, kind, *, page=None, suffix=None):
    """The deterministic file name for one artifact.

    ``<part number>_<rev>[_<page>][_<suffix>].<ext>`` — the identity leads, so a
    directory of packages sorts by part and revision, and a file that escapes its
    bundle still says which revision it belongs to. Names never contain a date: two
    releases of the same revision must produce the same names or the checksum diff
    is meaningless."""
    if kind == INSPECTION:
        ext = ".csv" if (suffix or "fai") == "fai" else ".json"
        parts = [_slug(part_number), _slug(rev), _slug(page), _slug(suffix or "fai")]
        return "_".join(parts) + ext
    if kind not in _NAME_RULES:
        raise ValueError(f"unknown release kind {kind!r}")
    fixed, ext = _NAME_RULES[kind]
    parts = [_slug(part_number), _slug(rev)]
    if page is not None:
        parts.append(_slug(page))
    if suffix or fixed:
        parts.append(_slug(suffix or fixed))
    return "_".join(parts) + ext


# === checksums ================================================================

def digest(data):
    """blake2b-256 of some bytes — the checksum a manifest entry pins."""
    return hashlib.blake2b(data, digest_size=DIGEST_SIZE).hexdigest()


def digest_file(path):
    """blake2b-256 of a file's bytes. Raises if it is missing: a manifest cannot
    pin bytes that aren't there."""
    with open(path, "rb") as f:
        return digest(f.read())


# === determinism: scrub the volatile fields out of an export ==================
#
# The recipes layer guarantees same-inputs -> same-bytes GEOMETRY. The exporters
# then break the guarantee at the file level by stamping the wall clock into their
# headers, which is the classic way a "reproducible" package stops being diffable.
# These scrubs rewrite exactly those fields and nothing else.

# ISO-10303-21 header: FILE_NAME('<name>','<timestamp>',...). The timestamp is the
# second argument and it is the export instant — verified live against FreeCAD's
# STEP writer, which emits e.g. '2026-08-01T16:02:04'.
_STEP_TIMESTAMP = re.compile(rb"(FILE_NAME\s*\(\s*'[^']*'\s*,\s*')([^']*)(')")

# The subtler one, and the reason the reproducibility gate has to diff real bytes
# rather than trust a timestamp scrub: OpenCASCADE names the PRODUCT entity after
# its translator plus a per-SESSION export counter — 'Open CASCADE STEP translator
# 7.8 1' on the first export of a worker, '... 2' on the second. Two releases of an
# unchanged solid therefore differ in a field that describes nothing about the part.
# Rewriting it costs nothing (STEP is text; no offsets depend on the length) and the
# release spends the field on something useful instead: the part number and
# revision, so the model self-identifies inside the recipient's CAD system.
_STEP_PRODUCT = re.compile(
    rb"(PRODUCT\(\s*')Open CASCADE STEP translator[^']*('\s*,\s*\n?\s*')[^']*(')")

# DXF header variables carrying creation/update times, as Julian-day reals under
# group code 40. TechDraw's writeDXFPage does not currently emit them; the scrub is
# defensive so a future writer (or a differently-configured FreeCAD) can't
# reintroduce the drift silently.
_DXF_TIMESTAMP = re.compile(
    rb"((?:\A|\r?\n)[ \t]*9\r?\n\$TD[A-Z]*(?:CREATE|UPDATE|TIMER)\r?\n[ \t]*40\r?\n)"
    rb"[^\r\n]*")


def canonical_bytes(name, data, *, product=None):
    """Normalise an exported file's volatile header fields.

    Dispatches on ``name``'s extension:

    * ``.step`` / ``.stp`` — the ``FILE_NAME`` timestamp becomes :data:`EPOCH`, and
      OpenCASCADE's session-counted translator ``PRODUCT`` name becomes ``product``
      (the release passes the part number + revision) or, absent one, the bare
      translator name with the counter dropped.
    * ``.dxf`` — ``$TDCREATE`` / ``$TDUPDATE`` style header times become 0.
    * everything else — returned unchanged. The drawing SVG is composed by DriftPin
      from a template and carries no clock; the CSVs and the manifest are written
      here. The PDF is the one format that cannot be scrubbed after the fact
      (rewriting a date changes byte offsets the xref table points at), so its
      determinism is bought upstream instead — see :func:`pdf_invariant_note`.

    Returns the canonical bytes. Idempotent: scrubbing an already-scrubbed file is
    a no-op, so a package can be re-canonicalized without drifting."""
    ext = os.path.splitext(str(name))[1].lower()
    if ext in (".step", ".stp"):
        out = _STEP_TIMESTAMP.sub(
            lambda m: m.group(1) + EPOCH.encode("ascii") + m.group(3), data, count=1)
        label = (str(product).encode("utf-8") if product
                 else b"Open CASCADE STEP translator")
        return _STEP_PRODUCT.sub(
            lambda m: m.group(1) + label + m.group(2) + label + m.group(3), out)
    if ext == ".dxf":
        return _DXF_TIMESTAMP.sub(lambda m: m.group(1) + b"0.0", data)
    return data


def pdf_invariant_note():
    """Why the PDF is handled differently, in one string the manifest carries.

    reportlab stamps ``/CreationDate``, ``/ModDate`` and a random document ``/ID``
    into every file it writes. They cannot be scrubbed afterwards without
    invalidating the cross-reference table, so the worker sets reportlab's
    ``rl_config.invariant`` before rendering instead — the library's own switch for
    exactly this. Labelled rather than asserted: this environment has no
    svglib/reportlab, so the claim is carried, not proven."""
    return ("PDF determinism relies on reportlab's rl_config.invariant (fixed "
            "date, fixed document ID) rather than a post-hoc scrub, because "
            "rewriting a PDF date shifts the byte offsets its xref table points "
            "at. UNVERIFIED where svglib/reportlab is not installed.")


# === the PRELIMINARY watermark ================================================

def _xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_SVG_VIEWBOX = re.compile(
    rb'viewBox\s*=\s*["\']\s*[-\d.eE]+\s+[-\d.eE]+\s+([-\d.eE]+)\s+([-\d.eE]+)')


def svg_page_size(data, default=(297.0, 210.0)):
    """(width, height) in user units from an SVG's viewBox, or ``default``.

    The watermark has to be sized and centred against the real sheet; a hard-coded
    A4 would sit off-centre on an A3 print, which is exactly the sort of detail
    that makes a watermark look like a mistake instead of a statement."""
    m = _SVG_VIEWBOX.search(data)
    if not m:
        return default
    try:
        return (float(m.group(1)), float(m.group(2)))
    except ValueError:
        return default


def watermark_svg(data, text):
    """Burn a diagonal ``text`` across an SVG sheet.

    Drawn last (so nothing hides it) at low opacity (so it doesn't obscure the
    geometry it is marking), and rotated, because a horizontal band reads as a
    title and a diagonal one reads as a stamp."""
    w, h = svg_page_size(data)
    cx, cy = w / 2.0, h / 2.0
    size = max(6.0, min(w, h) * 0.11)
    mark = (
        f'<g id="driftpin-watermark">'
        f'<text x="{cx:.2f}" y="{cy:.2f}" font-size="{size:.2f}" '
        f'font-family="sans-serif" text-anchor="middle" '
        f'fill="#cc0000" fill-opacity="0.16" stroke="none" '
        f'transform="rotate(-30 {cx:.2f} {cy:.2f})">{_xml_escape(text)}</text>'
        f'</g>\n').encode("utf-8")
    idx = data.rfind(b"</svg>")
    if idx == -1:
        return data + mark
    return data[:idx] + mark + data[idx:]


def stamp_watermark(name, data, text):
    """Mark one artifact PRELIMINARY, using its format's own comment convention.

    A watermark that only lives on the drawing is not a watermark: the STEP is what
    a shop actually loads, and it has to say so too. Per format:

    * ``.svg`` — a real diagonal stamp across the sheet (:func:`watermark_svg`).
    * ``.step`` / ``.stp`` — an ISO-10303-21 comment directly after the magic line.
    * ``.dxf`` — a group-999 comment, the DXF spec's own comment record, at the top.
    * ``.csv`` — a ``#`` preamble line, matching how ``fai_csv`` carries its
      disclaimer.
    * ``.pdf`` — returned unchanged, and deliberately so: the worker renders the
      PDF *from the already-watermarked SVG*, so the mark is in the page content
      rather than bolted onto the container.
    * ``.json`` — returned unchanged: the manifest states ``draft`` and
      ``watermark`` as fields, which is stronger than a comment a parser drops.

    An unrecognised extension raises. Silently returning an unmarked artifact would
    be the one failure mode that matters here."""
    ext = os.path.splitext(str(name))[1].lower()
    if ext == ".svg":
        return watermark_svg(data, text)
    if ext in (".step", ".stp"):
        line = f"/* {text} */\n".encode("utf-8")
        head = b"ISO-10303-21;"
        if data.startswith(head):
            return head + b"\n" + line + data[len(head):].lstrip(b"\r\n")
        return line + data
    if ext == ".dxf":
        return f"999\n{text}\n".encode("utf-8") + data
    if ext == ".csv":
        return f"# {text}\n".encode("utf-8") + data
    if ext in (".pdf", ".json"):
        return data
    raise ValueError(
        f"no watermark convention for {ext!r} — refusing to write an unmarked "
        f"artifact into a draft package")


# === the bill of materials ====================================================

# Declared column order. Anything `bom_extract` emits that isn't listed is appended
# in sorted order rather than dropped — that is the SEAM for the orderable-parts
# work (#234): when it starts resolving purchased lines against the curated
# off-the-shelf catalog, the canonical `designation`, the `catalog_id`, whether the
# line is `stocked`, and the nearest `alternatives` for one that isn't land in these
# reserved positions with no change here. A column nobody anticipated still reaches
# the vendor, in sorted order, rather than being silently dropped.
#
# Deliberately absent: supplier, SKU, price, lead time. Those are QUOTE-TIME facts —
# they belong to the response an RFQ comes back with, not to a released BOM, which
# has to stay true for as long as the revision does. A price baked into a released
# artifact is stale by the time anyone reads it, and a released artifact is exactly
# the thing nobody re-checks. The RFQ flavour gets its cost baseline from
# `cost_estimate` instead (see `quantity_break_table`), where it is labelled
# `correlation` / band 100% and so cannot be mistaken for a real quote.
BOM_COLUMNS = ("part", "count", "designation", "catalog_id", "stocked",
               "alternatives", "total_volume_mm3", "total_mass_kg")


def bom_columns(rows):
    """The CSV column list for a set of BOM rows: the declared order filtered to
    what is actually present, then any unanticipated key, sorted."""
    present = set()
    for r in rows:
        present |= set(r)
    known = [c for c in BOM_COLUMNS if c in present]
    return known + sorted(present - set(BOM_COLUMNS))


def _csv_cell(value):
    # Local rather than imported from inspection: a two-line quoting rule is not
    # worth a cross-module dependency between two otherwise independent gates.
    s = "" if value is None else str(value)
    if any(ch in s for ch in ',"\n'):
        return '"' + s.replace('"', '""') + '"'
    return s


def bom_csv(rows, *, part=None, rev=None, eco=None):
    """Serialise a ``bom_extract`` result to CSV text.

    A ``#`` preamble carries the identity, so a BOM that escapes its bundle still
    says which part and revision it belongs to — the same discipline
    :func:`driftpin.inspection.fai_csv` applies. Rows are emitted in the order
    given (``bom_extract`` already sorts them by descending count), so two runs
    over the same assembly produce the same bytes."""
    cols = bom_columns(rows)
    lines = []
    if part:
        lines.append(f"# Part: {part}")
    if rev:
        lines.append(f"# Revision: {rev}")
    if eco:
        lines.append(f"# ECO: {eco}")
    lines.append(f"# Lines: {len(rows)}")
    lines.append(",".join(_csv_cell(c) for c in cols))
    for r in rows:
        lines.append(",".join(_csv_cell(r.get(c)) for c in cols))
    return "\n".join(lines) + "\n"


# === the RFQ flavour ==========================================================

def quantity_break_table(volume_mm3, material, breaks, *, process="cnc", **kw):
    """Per-unit cost at each quantity break — the quote-comparison baseline.

    An RFQ without quantity breaks gets one number back per vendor and no way to
    tell a low setup charge from a low piece price. This runs
    :func:`driftpin.analysis.cost.cost_estimate` once per quantity and reports the
    curve, which is what makes two quotes comparable.

    Returns ``{ok, rows, currency, material, process, volume_mm3, fidelity,
    band_pct, basis}``, or ``{ok: False, reason}`` when the material has no
    density/price in the corpus and none was supplied — a degradation, not a
    crash, so an RFQ for an exotic material still produces its geometry and
    drawings.

    The rollup inherits ``cost_estimate``'s label verbatim: ``correlation``,
    ``band_pct=100``. Trust the RATIO between quantities, not the dollars."""
    from driftpin.analysis import cost as _cost
    qs = sorted({int(q) for q in (breaks or [1]) if int(q) >= 1})
    if not qs:
        qs = [1]
    rows = []
    try:
        for q in qs:
            r = _cost.cost_estimate(volume_mm3=float(volume_mm3), material=material,
                                    process=process, quantity=q, **kw)
            rows.append({
                "quantity": q,
                "unit_cost_usd": round(float(r["unit_cost"]), 4),
                "extended_cost_usd": round(float(r["unit_cost"]) * q, 4),
                "material_cost_usd": round(float(r["material_cost"]), 4),
                "process_cost_usd": round(float(r["process_cost"]), 4),
                "tooling_amortized_usd": round(float(r["tooling_amortized"]), 4),
            })
    except (ValueError, KeyError) as exc:
        return {"ok": False, "rows": [], "quantities": qs,
                "reason": f"cost rollup unavailable: {exc}"}
    return {
        "ok": True,
        "rows": rows,
        "quantities": qs,
        "currency": "USD",
        "material": material,
        "process": process,
        "volume_mm3": round(float(volume_mm3), 6),
        "fidelity": "correlation",
        "band_pct": 100.0,
        "basis": "driftpin cost_estimate rollup (exact material cost over an "
                 "order-of-magnitude machine-time table) — a quote-COMPARISON "
                 "baseline, not a price",
    }


# === the manifest =============================================================

def describe_file(name, kind, data):
    """One manifest file entry: ``{name, kind, bytes, blake2b}``."""
    return {"name": name, "kind": kind, "bytes": len(data), "blake2b": digest(data)}


def build_manifest(ident, files, *, kinds, draft=False, rfq=None, pages=(),
                   dropped=(), implied=(), notes=None):
    """Compose the package index — the root of trust.

    Carries the identity (item, part number, revision, lifecycle state, ECO
    reference), the kind list actually produced, the pages the drawings came from,
    and a blake2b-256 checksum per file. It is deliberately NOT self-checksummed:
    it is the document you verify the others against.

    Nothing time-varying is recorded — no generation timestamp, no tool version, no
    hostname. Two releases of the same revision from the same inputs must produce
    the same bytes, and every field that could vary independently of the inputs is
    a field that would break that.

    Returns the manifest dict; serialise it with :func:`serialize_manifest`."""
    man = {
        "schema": SCHEMA,
        "item": ident.get("item"),
        "part_number": ident.get("part_number"),
        "rev": ident.get("rev"),
        "lifecycle": ident.get("lifecycle"),
        "eco": ident.get("eco"),
        "material": ident.get("material"),
        "draft": bool(draft),
        "watermark": WATERMARK_TEXT if draft else None,
        "flavor": "rfq" if rfq is not None else "release",
        "kinds": list(kinds),
        "pages": [str(p) for p in pages],
        "checksum": DIGEST_NAME,
        "files": sorted(files, key=lambda f: f["name"]),
        "determinism": pdf_invariant_note(),
    }
    if dropped:
        man["dropped_kinds"] = list(dropped)
    if implied:
        man["implied_kinds"] = list(implied)
    if rfq is not None:
        man["rfq"] = rfq
    if notes:
        man["notes"] = list(notes)
    return man


def serialize_manifest(manifest):
    """Canonical, git-diffable JSON for a manifest (sorted keys, 2-space indent,
    trailing newline) — the same deterministic-bytes discipline
    ``change.serialize_eco`` / ``serialize_baseline`` use."""
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def verify_package(manifest, out_dir):
    """Re-read every file the manifest pins and check it still has the recorded
    size and checksum.

    This is what makes a released package auditable months later with nothing but
    the directory: a corrupted, truncated or quietly re-exported artifact is caught
    against the manifest that shipped with it. The worker also runs it on its own
    output before returning, so a package never claims a checksum its bytes don't
    have.

    Returns ``{ok, checked, missing, mismatched}`` where each ``mismatched`` entry
    is ``{name, field, expected, actual}``."""
    missing, mismatched = [], []
    checked = 0
    for entry in manifest.get("files", []):
        path = os.path.join(out_dir, entry["name"])
        if not os.path.isfile(path):
            missing.append(entry["name"])
            continue
        with open(path, "rb") as f:
            data = f.read()
        checked += 1
        if len(data) != entry.get("bytes"):
            mismatched.append({"name": entry["name"], "field": "bytes",
                               "expected": entry.get("bytes"), "actual": len(data)})
        got = digest(data)
        if got != entry.get("blake2b"):
            mismatched.append({"name": entry["name"], "field": "blake2b",
                               "expected": entry.get("blake2b"), "actual": got})
    return {"ok": not missing and not mismatched, "checked": checked,
            "missing": sorted(missing), "mismatched": mismatched}
