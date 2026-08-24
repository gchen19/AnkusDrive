"""Orderable standard parts (issue #234) — pure-core unit tests.

No FreeCAD, no worker, no network: :mod:`ankusdrive.orderable` is string and table
arithmetic over plain dicts, so these run on the host interpreter in milliseconds.
They prove the four things the layer promises:

  * a designation is DERIVED, never guessed — every family round-trips through the
    parser back to the same canonical string, two spellings of one part collapse to
    one BOM line, and a fact nobody supplied (which grade of steel?) is reported as
    incomplete rather than defaulted to a plausible lie;
  * purchased-ness is two-sided — a hand-modelled "Bracket" is never flagged and
    never acquires a designation, while a hand-modelled "M6Screw" IS flagged;
  * the stocked-length ladder is DISCRETE and nothing interpolates — an M4×12
    resolves, an M4×13 does not, and the answer to the M4×13 is "12 and 16 exist",
    never a silently rounded size;
  * a part nobody stocks is LOUD — it drags a BOM's ok to False, names the
    alternatives, and stays in the parts list rather than being quietly dropped.

The catalog corpus itself is gated too: every declared size has a non-empty ladder,
every rung comes from the declared nominal-length series, and no ladder contains a
length shorter than the size it belongs to.

Run: python3 tests/test_orderable.py
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import orderable as ord_  # noqa: E402
from ankusdrive.analysis import standards as std  # noqa: E402

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


def _raises(label, exc, fn, *args, **kw):
    try:
        fn(*args, **kw)
    except exc:
        _ok(label, True)
    else:
        _ok(label, False, f"expected {exc.__name__}, got a value")


# --- designations are derived, never guessed ---------------------------------

def test_the_three_designations_the_issue_names():
    print("test_the_three_designations_the_issue_names")
    _check("socket head cap screw",
           ord_.designate_fastener("socket_head_cap_screw", "M4", 12,
                                   "A2")["designation"],
           "ISO 4762 M4×12 A2")
    _check("sealed bearing",
           ord_.designate_bearing("608", "2RS")["designation"], "608-2RS")
    _check("o-ring",
           ord_.designate_oring(24.99, 3.53, "NBR70")["designation"],
           "AS568-214 NBR70")


def test_every_family_round_trips_through_the_parser():
    print("test_every_family_round_trips_through_the_parser")
    # A designation is only an IDENTITY if it survives being written to a property
    # and read back. Anything that doesn't round-trip is a label, not a key.
    for label, card in (
        ("SHCS", ord_.designate_fastener("socket_head_cap_screw", "M4", 12, "A2")),
        ("hex bolt", ord_.designate_fastener("hex_bolt", "M8", 40, "8.8")),
        ("hex nut", ord_.designate_fastener("hex_nut", "M6", None, "8")),
        ("washer", ord_.designate_fastener("washer", "M6", None, "A2")),
        ("bearing", ord_.designate_bearing("6205", "2Z")),
        ("open bearing", ord_.designate_bearing("608")),
        ("o-ring", ord_.designate_oring(24.99, 3.53, "FKM75")),
        ("threaded rod", ord_.designate_threaded_rod(8, 1.25, 1000, "A2")),
        ("fine-pitch rod", ord_.designate_threaded_rod(8, 1.0, 1000, "A2")),
    ):
        back = ord_.parse_designation(card["designation"])
        _check(f"{label} round-trips", back.get("designation"), card["designation"])
        _check(f"{label} keeps its family", back.get("family"), card["family"])


def test_two_spellings_of_one_part_are_one_bom_line():
    print("test_two_spellings_of_one_part_are_one_bom_line")
    canon = "ISO 4762 M4×12 A2"
    for spelling in ("iso4762 m4x12 a2", "ISO 4762 M4 x 12 A2",
                     "  ISO4762   M4*12   A2  ", "DIN 912 M4×12 A2"):
        _check(f"{spelling!r}", ord_.normalize(spelling), canon)
    _check("and they share an identity key",
           len({ord_.designation_key(s) for s in
                ("iso4762 m4x12 a2", "ISO 4762 M4×12 A2")}), 1)


def test_the_standard_number_is_part_of_the_identity():
    print("test_the_standard_number_is_part_of_the_identity")
    # "M4×12 cap screw" is ambiguous; the standard number is what disambiguates it.
    # A plain-shank hex bolt is ISO 4014 (partially threaded), NOT ISO 4017.
    _check("socket head -> ISO 4762",
           ord_.designate_fastener("socket_head_cap_screw", "M4", 12)["standard"],
           "ISO 4762")
    _check("hex bolt -> ISO 4014 (partially threaded)",
           ord_.designate_fastener("hex_bolt", "M4", 12)["standard"], "ISO 4014")
    _check("nut -> ISO 4032",
           ord_.designate_fastener("hex_nut", "M4")["standard"], "ISO 4032")
    _check("washer -> ISO 7089",
           ord_.designate_fastener("washer", "M4")["standard"], "ISO 7089")


def test_a_washer_is_designated_by_bare_nominal_size():
    print("test_a_washer_is_designated_by_bare_nominal_size")
    # ISO 7089 designates by the nominal size of the bolt it fits ("ISO 7089 - 8"),
    # not by a thread designation — and it still has to round-trip.
    card = ord_.designate_fastener("washer", "M8", None, "A2")
    _check("no M prefix", card["designation"], "ISO 7089 8 A2")
    _check("round-trips", ord_.normalize("ISO 7089 8 A2"), "ISO 7089 8 A2")
    _check("an M-prefixed spelling is accepted too",
           ord_.normalize("ISO 7089 M8 A2"), "ISO 7089 8 A2")


def test_a_missing_grade_is_reported_not_invented():
    print("test_a_missing_grade_is_reported_not_invented")
    card = ord_.designate_fastener("socket_head_cap_screw", "M4", 12)
    _check("the designation still exists", card["designation"], "ISO 4762 M4×12")
    _check("but it is not orderable", card["complete"], False)
    _ok("and the reason says why", "grade" in card["reason"], card["reason"])
    _ok("nothing invented a grade", card["grade"] is None)
    graded = ord_.designate_fastener("socket_head_cap_screw", "M4", 12, "A2")
    _check("supplying it completes the designation", graded["complete"], True)


def test_a_typod_grade_is_loud():
    print("test_a_typod_grade_is_loud")
    # A silently-accepted bad grade becomes a part you order and cannot use.
    E = ord_.DesignationError
    _raises("'8.9' is not a property class", E,
            ord_.designate_fastener, "socket_head_cap_screw", "M4", 12, "8.9")
    _raises("'A3' is not a stainless grade", E,
            ord_.designate_fastener, "socket_head_cap_screw", "M4", 12, "A3")
    _raises("unknown kind", E, ord_.designate_fastener, "carriage_bolt", "M4", 12)
    _raises("non-metric size", E, ord_.designate_fastener, "hex_nut", "1/4-20")
    _raises("a screw needs a length", E,
            ord_.designate_fastener, "socket_head_cap_screw", "M4")


def test_nut_and_screw_grades_are_different_vocabularies():
    print("test_nut_and_screw_grades_are_different_vocabularies")
    # ISO 898-2 classifies a nut "8", ISO 898-1 classifies a screw "8.8". Writing
    # "ISO 4032 M8 8.8" is a real and expensive mistake, so it must not parse.
    E = ord_.DesignationError
    _check("nut takes '8'",
           ord_.designate_fastener("hex_nut", "M8", None, "8")["designation"],
           "ISO 4032 M8 8")
    _raises("nut rejects '8.8'", E,
            ord_.designate_fastener, "hex_nut", "M8", None, "8.8")
    _raises("screw rejects the bare nut class '8'", E,
            ord_.designate_fastener, "socket_head_cap_screw", "M8", 20, "8")


def test_the_seal_suffix_is_part_of_the_bearing_identity():
    print("test_the_seal_suffix_is_part_of_the_bearing_identity")
    # 608 / 608-2Z / 608-2RS share an envelope and are three different purchases.
    ids = {ord_.designate_bearing("608", s)["designation"]
           for s in ("open", "2Z", "2RS")}
    _check("three distinct designations", sorted(ids), ["608", "608-2RS", "608-2Z"])
    _check("a suffix already on the number is kept",
           ord_.designate_bearing("608-2RS")["designation"], "608-2RS")
    _check("an explicit seals= overrides it",
           ord_.designate_bearing("608-2RS", "2Z")["designation"], "608-2Z")
    _check("case and spacing don't matter",
           ord_.designate_bearing(" 608 ", "2rs")["designation"], "608-2RS")
    _raises("an unknown seal code is loud", ord_.DesignationError,
            ord_.designate_bearing, "608", "2QQ")


def test_a_bearing_with_no_catalog_number_is_not_designatable():
    print("test_a_bearing_with_no_catalog_number_is_not_designatable")
    # There is no such thing as a generic orderable bearing: bore/OD/width is an
    # envelope. Refusing beats naming a catalog number nobody chose.
    _raises("empty designation", ord_.DesignationError, ord_.designate_bearing, "")
    _raises("None designation", ord_.DesignationError, ord_.designate_bearing, None)


# --- the AS568 table ----------------------------------------------------------

def test_as568_anchors_match_the_published_table():
    print("test_as568_anchors_match_the_published_table")
    # The table is a rule plus anchors; these are the published values it must
    # reproduce, and they are what catches a wrong step or a wrong base.
    by_dash = {r["dash"]: r for r in std.as568_sizes()}
    _check("-110 ID", by_dash[110]["id_mm"], 9.19)
    _check("-110 CS", by_dash[110]["cs_mm"], 2.62)
    _check("-149 ID", by_dash[149]["id_mm"], 71.12)
    _check("-214 ID", by_dash[214]["id_mm"], 24.99)
    _check("-214 CS", by_dash[214]["cs_mm"], 3.53)
    _check("-201 ID", by_dash[201]["id_mm"], 4.34)


def test_an_off_table_oring_gets_no_designation():
    print("test_an_off_table_oring_gets_no_designation")
    # Between -214 (24.99) and -215 (26.57) sits nothing standard. A ring that
    # isn't a standard size is a custom tooled part; naming the neighbour would
    # produce a seal that doesn't fit its gland.
    card = ord_.designate_oring(25.8, 3.53, "NBR70")
    _check("no designation", card["designation"], None)
    _ok("nearest sizes are named", len(card["nearest"]) >= 2, card["reason"])
    _ok("the reason gives the miss distance", "mm off" in card["reason"],
        card["reason"])
    off_cs = ord_.designate_oring(24.99, 3.0, "NBR70")
    _check("a non-series cross-section is refused too",
           off_cs["designation"], None)
    _ok("and it lists the sections that exist",
        "tabulated sections" in off_cs["reason"], off_cs["reason"])


def test_an_oring_without_a_compound_is_incomplete():
    print("test_an_oring_without_a_compound_is_incomplete")
    card = ord_.designate_oring(24.99, 3.53)
    _check("size named", card["designation"], "AS568-214")
    _check("not orderable", card["complete"], False)
    _ok("because NBR70 and FKM75 are the same size, different parts",
        "FKM75" in card["reason"], card["reason"])


# --- purchased-ness is two-sided ---------------------------------------------

def test_looks_purchased_is_two_sided():
    print("test_looks_purchased_is_two_sided")
    for name in ("SocketHeadCapScrew", "HexBolt001", "Bearing", "M6Screw",
                 "hex_nut_2", "ORingGroove", "washer"):
        _ok(f"{name} reads purchased", ord_.looks_purchased(name)["purchased"])
    for name in ("Bracket", "Housing", "Plate", "Box", "Cut", "MotorMount",
                 "Shaft", "Body"):
        _ok(f"{name} does NOT read purchased",
            not ord_.looks_purchased(name)["purchased"],
            f"matched {ord_.looks_purchased(name)['matched']}")


def test_nothing_infers_a_designation_from_a_name():
    print("test_nothing_infers_a_designation_from_a_name")
    # looks_purchased raises a QUESTION; it never answers it. There is no code path
    # from a name to a designation, which is what keeps false designations out.
    hit = ord_.looks_purchased("M6Screw")
    _ok("no designation key", "designation" not in hit)
    _ok("it labels itself a heuristic", "heuristic" in hit["basis"])
    _check("and arbitrary text is not a designation",
           ord_.normalize("Bracket"), None)


def test_designation_check_flags_only_what_it_should():
    print("test_designation_check_flags_only_what_it_should")
    rows = [
        {"part": "Bracket", "count": 1},                                    # made
        {"part": "SocketHeadCapScrew", "count": 4, "part_class": "purchased",
         "designation": "ISO 4762 M4×12 A2", "complete": True},             # fine
        {"part": "Bearing", "count": 2, "part_class": "purchased"},         # no des.
        {"part": "M6Screw", "count": 8},                            # looks purchased
        {"part": "HexNut", "count": 4, "part_class": "purchased",
         "designation": "ISO 4032 M6", "complete": False,
         "reason": "no material grade specified"},                       # incomplete
    ]
    v = ord_.designation_check(rows)
    _check("not ok", v["ok"], False)
    _check("the machined bracket is untouched",
           [f["part"] for f in v["findings"]], ["Bearing", "M6Screw", "HexNut"])
    codes = {f["part"]: f["code"] for f in v["findings"]}
    _check("stamped-but-undesignated", codes["Bearing"], "no_designation")
    _check("name-heuristic finding", codes["M6Screw"], "no_designation")
    _check("under-specified designation", codes["HexNut"],
           "incomplete_designation")
    cert = {f["part"]: f["certainty"] for f in v["findings"]}
    _check("certainty is honest about which is which",
           (cert["Bearing"], cert["M6Screw"]), ("stamped", "name_heuristic"))
    _check("counts", (v["purchased"], v["designated"], v["undesignated"]),
           (4, 2, 2))


def test_a_fully_designated_bom_passes_the_check():
    print("test_a_fully_designated_bom_passes_the_check")
    rows = [
        {"part": "Bracket", "count": 1},
        {"part": "SocketHeadCapScrew", "count": 4, "part_class": "purchased",
         "designation": "ISO 4762 M4×12 A2", "complete": True},
        {"part": "Bearing", "count": 2, "part_class": "purchased",
         "designation": "608-2RS", "complete": True},
    ]
    v = ord_.designation_check(rows)
    _check("ok", v["ok"], True)
    _check("no findings", v["findings"], [])
    _check("both purchased rows counted", v["purchased"], 2)


# --- the ladder is discrete ---------------------------------------------------

def test_nearest_on_a_ladder_never_interpolates():
    print("test_nearest_on_a_ladder_never_interpolates")
    ladder = [5, 6, 8, 10, 12, 16, 20]
    exact = ord_.nearest_on_ladder(ladder, 12)
    _check("an exact rung is exact", exact["exact"], True)
    _check("and resolves to itself", exact["value"], 12.0)

    miss = ord_.nearest_on_ladder(ladder, 13)
    _check("13 is not a rung", miss["exact"], False)
    _check("bracketed below", miss["below"], 12.0)
    _check("bracketed above", miss["above"], 16.0)
    _check("nearest is the closer rung, not an average",
           [n["length"] for n in miss["nearest"]], [12.0, 16.0])
    _check("with signed deltas", [n["delta"] for n in miss["nearest"]],
           [-1.0, 3.0])
    _ok("nothing between the rungs is ever returned",
        all(n["length"] in ladder for n in miss["nearest"]))

    # off either end: one bracket exists, the other legitimately does not
    low = ord_.nearest_on_ladder(ladder, 3)
    _check("below the ladder has no lower bracket", low["below"], None)
    _check("and snaps up", low["value"], 5.0)
    high = ord_.nearest_on_ladder(ladder, 40)
    _check("above the ladder has no upper bracket", high["above"], None)
    _check("and snaps down", high["value"], 20.0)


def test_a_tie_resolves_upward():
    print("test_a_tie_resolves_upward")
    # Exactly between two rungs, prefer the longer: a too-short screw doesn't
    # engage, a too-long one is usually a washer or a counterbore from working.
    tie = ord_.nearest_on_ladder([10, 12, 16], 14)
    _check("both rungs are 2 mm away",
           [abs(n["delta"]) for n in tie["nearest"]], [2.0, 2.0])
    _check("the longer one is recommended", tie["value"], 16.0)


def test_catalog_nearest_answers_the_m4x13_question():
    print("test_catalog_nearest_answers_the_m4x13_question")
    # The gate the issue names: a modelled M4×12 resolves, an M4×13 does not, and
    # the answer to the M4×13 names 12 and 16.
    good = ord_.catalog_nearest("ISO 4762", "M4", 12, "A2")
    _check("M4×12 is stocked", good["ok"], True)
    _check("exact", good["exact"], True)
    _check("designation handed back ready to use", good["designation"],
           "ISO 4762 M4×12 A2")

    bad = ord_.catalog_nearest("ISO 4762", "M4", 13, "A2")
    _check("M4×13 is not", bad["ok"], False)
    _check("below", bad["below"], 12.0)
    _check("above", bad["above"], 16.0)
    _check("it recommends the nearest real part", bad["designation"],
           "ISO 4762 M4×12 A2")
    _ok("and the reason spells out the ladder", "12" in bad["reason"]
        and "16" in bad["reason"], bad["reason"])


def test_catalog_nearest_resolves_aliases_and_no_length_products():
    print("test_catalog_nearest_resolves_aliases_and_no_length_products")
    _check("DIN 912 is ISO 4762",
           ord_.catalog_nearest("DIN 912", "M4", 12)["standard"], "ISO 4762")
    nut = ord_.catalog_nearest("ISO 4032", "M6")
    _check("a nut has no length dimension", nut["lengths"], [])
    _check("and the size alone is the answer", nut["ok"], True)
    ladder = ord_.catalog_nearest("ISO 4762", "M4")
    _check("omitting the length lists the whole ladder", ladder["lengths"],
           [5, 6, 8, 10, 12, 16, 20, 25, 30, 35, 40, 45, 50])


def test_a_size_off_the_end_of_the_range_is_reported_not_raised():
    print("test_a_size_off_the_end_of_the_range_is_reported_not_raised")
    # An unstocked SIZE is the same kind of answer as an unstocked length, so it
    # comes back the same shape rather than as an exception the caller must catch.
    out = ord_.catalog_nearest("ISO 4762", "M22", 40)
    _check("not stocked", out["ok"], False)
    _check("no ladder", out["lengths"], [])
    _ok("the stocked sizes are named", "M20" in out["reason"], out["reason"])
    _raises("but an unknown STANDARD is a coverage error, not a design finding",
            std.StandardNotFound, ord_.catalog_nearest, "ISO 9999", "M4", 12)


# --- catalog_search -----------------------------------------------------------

def test_catalog_search_finds_what_exists_at_a_size():
    print("test_catalog_search_finds_what_exists_at_a_size")
    hits = ord_.catalog_search(family="screw", size="M4", length=12)
    stds = hits["standards"]
    _ok("socket cap, button and countersunk all exist in M4×12",
        {"ISO 4762", "ISO 7380-1", "ISO 10642"} <= set(stds), stds)
    _ok("every hit really carries that length",
        all(i["lengths"] == [12] for i in hits["items"]), hits["items"])
    _ok("and each names its drive",
        all(i["drive"] == "hex_socket" for i in hits["items"]))


def test_catalog_search_filters_compose():
    print("test_catalog_search_filters_compose")
    win = ord_.catalog_search(standard="ISO 4762", size="M6",
                              min_length=20, max_length=40)
    _check("one row per (standard, size)", win["count"], 1)
    _check("the ladder is narrowed to the window", win["items"][0]["lengths"],
           [20, 25, 30, 35, 40])
    none = ord_.catalog_search(family="nut", length=12)
    _check("a length filter cannot be satisfied by a nut", none["count"], 0)
    _check("and ok says so", none["ok"], False)
    drive = ord_.catalog_search(family="screw", drive="hex")
    _ok("hex-drive screws are the hex-head standards",
        set(drive["standards"]) == {"ISO 4014", "ISO 4017"}, drive["standards"])


def test_catalog_search_declares_what_it_does_not_cover():
    print("test_catalog_search_declares_what_it_does_not_cover")
    # An empty result must not read as "this part does not exist" — the corpus says
    # out loud what it deliberately omits.
    out = ord_.catalog_search(family="screw")
    _check("fidelity is labelled", out["fidelity"], "curated snapshot")
    _ok("dated", bool(out["captured"]), out)
    _ok("the market is named, not implied", "metric" in out["market"].lower())
    _ok("and the omissions are declared", len(out["not_covered"]) >= 3,
        out["not_covered"])
    _ok("including the inch market", any("inch" in n.lower()
                                         for n in out["not_covered"]))


def test_search_results_are_usable_designations():
    print("test_search_results_are_usable_designations")
    hits = ord_.catalog_search(standard="ISO 4762", size="M6", length=20,
                               grade="A2-70")
    item = hits["items"][0]
    _check("a designation is handed back", item["designation"],
           "ISO 4762 M6×20 A2-70")
    _check("and it round-trips", ord_.normalize(item["designation"]),
           item["designation"])
    _check("and it checks out as stocked",
           ord_.catalog_check(item["designation"])["code"], "stocked")


# --- catalog_check ------------------------------------------------------------

def test_catalog_check_is_two_sided():
    print("test_catalog_check_is_two_sided")
    good = ord_.catalog_check("ISO 4762 M4×12 A2")
    _check("a stocked part passes", good["ok"], True)
    _check("code", good["code"], "stocked")

    bad = ord_.catalog_check("ISO 4762 M4×13 A2")
    _check("an unstocked length fails", bad["ok"], False)
    _check("code", bad["code"], "not_stocked")
    _check("with both brackets", [n["length"] for n in bad["nearest"]],
           [12.0, 16.0])
    _ok("no false 'available' anywhere", bad["stocked"] is False)


def test_catalog_check_codes_cover_each_way_a_part_can_be_unbuyable():
    print("test_catalog_check_codes_cover_each_way_a_part_can_be_unbuyable")
    cases = {
        "ISO 4762 M4×12 A2": "stocked",
        "ISO 4762 M4×13 A2": "not_stocked",
        "ISO 4762 M22×40 A2": "size_not_stocked",
        "ISO 4762 M4×12 4.6": "grade_not_listed",
        "608-2RS": "stocked",
        "6205-2Z": "stocked",
        "AS568-214 NBR70": "stocked",
    }
    for designation, want in cases.items():
        _check(designation, ord_.catalog_check(designation)["code"], want)
    _check("and no designation at all is its own code",
           ord_.catalog_check("Bracket")["code"], "undesignated")


def test_an_uncovered_standard_is_not_a_claim_of_unavailability():
    print("test_an_uncovered_standard_is_not_a_claim_of_unavailability")
    # The corpus's coverage is finite. Reporting "outside coverage" as if it meant
    # "does not exist" would make the tool actively misleading.
    v = ord_.catalog_check({"designation": "DIN 6885 A 8×7×40",
                            "standard": "DIN 6885", "family": "key"})
    _check("code", v["code"], "not_catalogued")
    _ok("and it says so in as many words",
        "absence of evidence" in v["reason"], v["reason"])
    rows = [{"part": "Key", "count": 1, "part_class": "purchased",
             "designation": "DIN 6885 A 8×7×40", "complete": True,
             "standard": "DIN 6885"}]
    out = ord_.orderable_bom(rows)
    _check("so it never becomes a not_stocked finding", out["not_stocked"], [])
    _check("and it does not condemn the BOM", out["ok"], True)


def test_threaded_rod_is_cut_to_length_not_off_the_ladder():
    print("test_threaded_rod_is_cut_to_length_not_off_the_ladder")
    # Studding is bought by the bar and cut, so a 250 mm length is normal practice —
    # treating it as "not stocked" would be pedantic and wrong.
    cut = ord_.catalog_check("DIN 976-1 M8×250 A2")
    _check("a cut length is stocked", cut["code"], "stocked")
    _ok("with a note saying it is a cut", "cut to length" in cut.get("note", ""),
        cut.get("note"))
    too_long = ord_.catalog_check("DIN 976-1 M8×5000 A2")
    _check("but longer than the longest bar is not", too_long["code"],
           "not_stocked")
    _ok("and it says why", "joined" in too_long["reason"], too_long["reason"])


def test_a_stainless_grade_matches_its_strength_class():
    print("test_a_stainless_grade_matches_its_strength_class")
    # 'A2' names the alloy and '-70' the strength class, so a catalog listing A2-70
    # satisfies a designation that says A2 — but NOT the other way round.
    _check("A2 is satisfied by A2-70",
           ord_.catalog_check("ISO 4762 M4×12 A2")["code"], "stocked")
    _check("A2-70 is satisfied exactly",
           ord_.catalog_check("ISO 4762 M4×12 A2-70")["code"], "stocked")
    _check("A2-80 is NOT satisfied by A2-70",
           ord_.catalog_check("ISO 4762 M4×12 A2-80")["code"], "grade_not_listed")


# --- the corpus itself --------------------------------------------------------

def test_every_catalog_size_has_a_usable_ladder():
    print("test_every_catalog_size_has_a_usable_ladder")
    # The corpus's own coverage gate. A product that claims a diameter must say
    # which lengths it comes in, every rung must come from the declared nominal
    # length series, and no ladder may contain a length shorter than the size it
    # belongs to (an M8×5 socket cap screw is nonsense, not a product).
    empty, off_series, nonsense, unsorted = [], [], [], []
    for pid in std.list_catalog_products():
        product = std.catalog_product(pid)
        series_name = product.get("length_series")
        series = set(std.catalog_length_series(series_name)) if series_name else None
        for size, ladder in product["sizes"].items():
            if ladder != sorted(set(ladder)):
                unsorted.append((pid, size))
            if series is None:
                continue
            if not ladder:
                empty.append((pid, size))
                continue
            if not set(ladder) <= series:
                off_series.append((pid, size, sorted(set(ladder) - series)))
            try:
                nominal = float(str(size).lstrip("Mm-"))
            except ValueError:
                continue
            if min(ladder) < nominal:
                nonsense.append((pid, size, min(ladder)))
    _check("every ladder is sorted and duplicate-free", unsorted, [])
    _check("no size claims a length range and then supplies none", empty, [])
    _check("every rung is a nominal length from the declared series",
           off_series, [])
    _check("no ladder contains a length shorter than its own diameter",
           nonsense, [])


def test_the_catalog_declares_its_provenance():
    print("test_the_catalog_declares_its_provenance")
    meta = std.catalog_meta()
    for field in ("description", "market", "captured", "fidelity", "source",
                  "not_covered"):
        _ok(f"_meta carries {field}", bool(meta.get(field)), meta.keys())
    _ok("the source names the length series it drew from",
        "ISO 888" in meta["source"], meta["source"])
    _ok("and the market claim is scoped, not global",
        "metric" in meta["market"].lower() and "inch" in meta["market"].lower(),
        meta["market"])


def test_bearings_and_orings_are_reused_never_duplicated():
    print("test_bearings_and_orings_are_reused_never_duplicated")
    # A second copy of the bearing list would be a second source of truth, which is
    # the failure a catalog exists to prevent.
    sizes = set(std.catalog_sizes("ISO 15"))
    _ok("every rated bearing is addressable through the catalog",
        set(std.list_bearings()) <= sizes,
        sorted(set(std.list_bearings()) - sizes))
    _ok("plus the ubiquitous miniature series bearings.json does not rate",
        {"608", "625", "6800"} <= sizes)
    dashes = set(std.catalog_sizes("SAE AS568"))
    _ok("O-ring sizes come from the AS568 rule",
        len(dashes) == len(std.as568_sizes()) and "-214" in dashes)


def test_every_catalog_standard_resolves_by_alias():
    print("test_every_catalog_standard_resolves_by_alias")
    bad = []
    for pid in std.list_catalog_products():
        product = std.catalog_product(pid)
        for name in [pid, *(product.get("aliases") or [])]:
            if std.catalog_product(name)["id"] != pid:
                bad.append((name, pid))
    _check("every id and alias resolves to its own product", bad, [])
    _raises("and an unknown one is loud", std.StandardNotFound,
            std.catalog_product, "ISO 99999")


def test_results_are_deterministic():
    print("test_results_are_deterministic")
    # No wall clock, no set-iteration order: the capture date comes from the corpus
    # and every listing is built from sorted collections.
    for label, fn in (
        ("catalog_search", lambda: ord_.catalog_search(family="screw", size="M6")),
        ("catalog_nearest", lambda: ord_.catalog_nearest("ISO 4762", "M4", 13)),
        ("catalog_check", lambda: ord_.catalog_check("ISO 4762 M4×13 A2")),
    ):
        _check(f"{label} agrees with itself",
               json.dumps(fn(), sort_keys=True), json.dumps(fn(), sort_keys=True))


# --- the orderable BOM --------------------------------------------------------

def _bom_rows():
    return [
        {"part": "Bracket", "count": 1, "total_volume_mm3": 12000.0},
        {"part": "SocketHeadCapScrew", "count": 4, "total_volume_mm3": 400.0,
         "part_class": "purchased", "designation": "ISO 4762 M4×12 A2",
         "complete": True, "standard": "ISO 4762"},
        {"part": "Bearing", "count": 2, "total_volume_mm3": 900.0,
         "part_class": "purchased", "designation": "608-2RS", "complete": True,
         "standard": "ISO 15"},
    ]


def test_orderable_bom_marks_the_purchased_rows_and_leaves_the_rest():
    print("test_orderable_bom_marks_the_purchased_rows_and_leaves_the_rest")
    out = ord_.orderable_bom(_bom_rows())
    _check("ok", out["ok"], True)
    by_part = {r["part"]: r for r in out["rows"]}
    _check("every row survives", len(out["rows"]), 3)
    _ok("the machined bracket gets no catalog verdict",
        "catalog" not in by_part["Bracket"])
    _check("the screws are stocked", by_part["SocketHeadCapScrew"]["stocked"],
           True)
    _check("the bearings are stocked", by_part["Bearing"]["catalog_code"],
           "stocked")
    _check("counted", out["stocked_count"], 2)
    _ok("with the corpus's date attached", bool(out["captured"]))
    _check("and its fidelity labelled", out["fidelity"], "curated snapshot")


def test_one_unstocked_part_condemns_the_whole_bom():
    print("test_one_unstocked_part_condemns_the_whole_bom")
    # The failure this exists to prevent: a parts list that reads as buildable while
    # containing a screw nobody sells.
    rows = _bom_rows() + [
        {"part": "OddScrew", "count": 1, "total_volume_mm3": 100.0,
         "part_class": "purchased", "designation": "ISO 4762 M4×13 A2",
         "complete": True, "standard": "ISO 4762"}]
    out = ord_.orderable_bom(rows)
    _check("not ok despite three good rows", out["ok"], False)
    _check("the offender is named",
           [f["designation"] for f in out["not_stocked"]], ["ISO 4762 M4×13 A2"])
    _check("with the alternatives",
           [n["length"] for n in out["not_stocked"][0]["nearest"]], [12.0, 16.0])
    _ok("and it is still IN the parts list, not dropped",
        any(r["part"] == "OddScrew" for r in out["rows"]))
    _check("the good rows are still marked stocked", out["stocked_count"], 2)


def test_an_undesignated_purchased_part_condemns_the_bom():
    print("test_an_undesignated_purchased_part_condemns_the_bom")
    rows = _bom_rows() + [
        {"part": "HexNut", "count": 4, "total_volume_mm3": 80.0,
         "part_class": "purchased"}]
    out = ord_.orderable_bom(rows)
    _check("not ok", out["ok"], False)
    _check("reported as undesignated", [f["part"] for f in out["undesignated"]],
           ["HexNut"])
    _check("nothing is not_stocked (it never got that far)",
           out["not_stocked"], [])


def test_check_stock_false_designates_without_checking_availability():
    print("test_check_stock_false_designates_without_checking_availability")
    rows = _bom_rows() + [
        {"part": "OddScrew", "count": 1, "part_class": "purchased",
         "designation": "ISO 4762 M4×13 A2", "complete": True,
         "standard": "ISO 4762"}]
    out = ord_.orderable_bom(rows, check_stock=False)
    _check("designations alone pass", out["ok"], True)
    _check("no availability findings", out["not_stocked"], [])
    _ok("and no verdicts leaked in", all("catalog" not in r for r in out["rows"]))


def main():
    for fn in (
        test_the_three_designations_the_issue_names,
        test_every_family_round_trips_through_the_parser,
        test_two_spellings_of_one_part_are_one_bom_line,
        test_the_standard_number_is_part_of_the_identity,
        test_a_washer_is_designated_by_bare_nominal_size,
        test_a_missing_grade_is_reported_not_invented,
        test_a_typod_grade_is_loud,
        test_nut_and_screw_grades_are_different_vocabularies,
        test_the_seal_suffix_is_part_of_the_bearing_identity,
        test_a_bearing_with_no_catalog_number_is_not_designatable,
        test_as568_anchors_match_the_published_table,
        test_an_off_table_oring_gets_no_designation,
        test_an_oring_without_a_compound_is_incomplete,
        test_looks_purchased_is_two_sided,
        test_nothing_infers_a_designation_from_a_name,
        test_designation_check_flags_only_what_it_should,
        test_a_fully_designated_bom_passes_the_check,
        test_nearest_on_a_ladder_never_interpolates,
        test_a_tie_resolves_upward,
        test_catalog_nearest_answers_the_m4x13_question,
        test_catalog_nearest_resolves_aliases_and_no_length_products,
        test_a_size_off_the_end_of_the_range_is_reported_not_raised,
        test_catalog_search_finds_what_exists_at_a_size,
        test_catalog_search_filters_compose,
        test_catalog_search_declares_what_it_does_not_cover,
        test_search_results_are_usable_designations,
        test_catalog_check_is_two_sided,
        test_catalog_check_codes_cover_each_way_a_part_can_be_unbuyable,
        test_an_uncovered_standard_is_not_a_claim_of_unavailability,
        test_threaded_rod_is_cut_to_length_not_off_the_ladder,
        test_a_stainless_grade_matches_its_strength_class,
        test_every_catalog_size_has_a_usable_ladder,
        test_the_catalog_declares_its_provenance,
        test_bearings_and_orings_are_reused_never_duplicated,
        test_every_catalog_standard_resolves_by_alias,
        test_results_are_deterministic,
        test_orderable_bom_marks_the_purchased_rows_and_leaves_the_rest,
        test_one_unstocked_part_condemns_the_whole_bom,
        test_an_undesignated_purchased_part_condemns_the_bom,
        test_check_stock_false_designates_without_checking_availability,
    ):
        fn()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
