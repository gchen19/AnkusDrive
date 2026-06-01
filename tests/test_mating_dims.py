"""
Mating-dimension golden table for the parametric generators.

The value of a generator (add_gear, add_rack, ...) is the mating/reference
dimensions it returns — pitch_radius, pitch_diameter, spring_rate, the
fastener/bearing table rows — because a coordinator uses those numbers to place
and size neighbouring parts. This locks those formulas: each is recomputed from
first principles HERE and checked against the returned value across SEVERAL
inputs, so a refactor can't silently shift a coefficient in a way that a single
hard-coded example would miss.

This is the dimensional counterpart to the watertight-solid smoke (which checks
the geometry is sound) — here we check the NUMBERS are sound.

Run:  python3 tests/test_mating_dims.py
"""
import math
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402


def _close(got, want, tol=1e-3, label=""):
    assert abs(got - want) <= tol + 1e-9 * abs(want), (
        f"{label}: got {got}, expected {want} (tol {tol})")


# --- gear family --------------------------------------------------------------

def test_gear_family_mating_dims():
    with Worker() as w:
        w.call("new_document", name="md_gears")

        # GEAR: pitch_radius = m*N/2, tip = pr + m, root = pr - 1.25*m
        for teeth, m in [(12, 2.0), (24, 1.5), (40, 3.0), (9, 1.0)]:
            r = w.call("add_gear", teeth=teeth, module=m, height=6.0)
            pr = m * teeth / 2.0
            _close(r["pitch_radius"], pr, label=f"gear pr N={teeth} m={m}")
            _close(r["tip_radius"], pr + m, label=f"gear tip N={teeth} m={m}")
            _close(r["root_radius"], pr - 1.25 * m, label=f"gear root N={teeth} m={m}")

        # RACK: pitch = pi*m, length = N*pi*m, tooth_height = 2.25*m
        for teeth, m in [(10, 2.0), (6, 1.5), (20, 3.0)]:
            r = w.call("add_rack", teeth=teeth, module=m)
            _close(r["pitch"], math.pi * m, label=f"rack pitch N={teeth} m={m}")
            _close(r["length"], teeth * math.pi * m, label=f"rack length N={teeth} m={m}")
            _close(r["tooth_height"], 2.25 * m, label=f"rack tooth_h N={teeth} m={m}")

        # SPROCKET: pitch_diameter = chain_pitch / sin(pi/N); tip = pd/2 + 0.3*cp
        for teeth, cp, roller in [(17, 12.7, 7.92), (11, 9.525, 5.08), (25, 15.875, 10.16)]:
            r = w.call("add_sprocket", teeth=teeth, chain_pitch=cp, roller_diameter=roller)
            pd = cp / math.sin(math.pi / teeth)
            _close(r["pitch_diameter"], pd, label=f"sprocket pd N={teeth} cp={cp}")
            _close(r["tip_radius"], pd / 2.0 + cp * 0.3, label=f"sprocket tip N={teeth} cp={cp}")

        # PULLEY: pitch_diameter = belt_pitch * N / pi
        for teeth, bp in [(20, 2.0), (36, 3.0), (15, 5.0)]:
            r = w.call("add_pulley", teeth=teeth, belt_pitch=bp, width=6)
            _close(r["pitch_diameter"], bp * teeth / math.pi, label=f"pulley pd N={teeth} bp={bp}")


# --- spring + thread ----------------------------------------------------------

def test_spring_and_thread_mating_dims():
    with Worker() as w:
        w.call("new_document", name="md_spring_thread")

        # SPRING: mean = OD - d; solid_height = coils*d;
        #         rate = G*d^4 / (8*D^3*coils), G(steel)=79300 MPa -> N/mm
        G = 79300.0
        for d, od, fl, coils in [(2.0, 20.0, 40.0, 8.0), (1.5, 15.0, 30.0, 6.0), (3.0, 25.0, 50.0, 10.0)]:
            r = w.call("add_spring", wire_diameter=d, outer_diameter=od, free_length=fl, coils=coils)
            mean = od - d
            _close(r["mean_diameter"], mean, label=f"spring mean d={d} od={od}")
            _close(r["solid_height"], coils * d, label=f"spring sh d={d} coils={coils}")
            rate = G * d ** 4 / (8.0 * mean ** 3 * coils)
            _close(r["spring_rate_n_per_mm"], rate, tol=1e-2, label=f"spring k d={d} od={od}")

        # THREAD: major = diameter; minor = diameter - 1.0825*pitch (ISO 60-deg)
        for dia, pitch in [(8.0, 1.25), (6.0, 1.0), (12.0, 1.75), (10.0, 1.5)]:
            r = w.call("add_thread", diameter=dia, pitch=pitch, length=10.0)
            _close(r["major_diameter"], dia, label=f"thread major M{dia}")
            _close(r["minor_diameter"], dia - 1.0825 * pitch, label=f"thread minor M{dia}x{pitch}")


# --- o-ring gland (pure calculator path) --------------------------------------

def test_oring_gland_dims():
    with Worker() as w:
        w.call("new_document", name="md_oring")
        # depth = cs*0.75 (in the 0.70..0.80 clamp band -> unclamped); width = cs*1.30;
        # groove_outer_diameter = id + 2*width; squeeze = 25%.
        for cs, idia in [(1.78, 15.0), (2.62, 20.0), (3.53, 40.0)]:
            r = w.call("oring_groove", cross_section=cs, inner_diameter=idia, cut=False)
            _close(r["groove_depth"], cs * 0.75, label=f"oring depth cs={cs}")
            _close(r["groove_width"], cs * 1.30, label=f"oring width cs={cs}")
            _close(r["groove_inner_diameter"], idia, label=f"oring id cs={cs}")
            _close(r["groove_outer_diameter"], idia + 2.0 * cs * 1.30, label=f"oring od cs={cs}")
            _close(r["squeeze_pct"], 25.0, tol=1e-2, label=f"oring squeeze cs={cs}")
            assert "handle" not in r, f"calc path must emit no handle: {r}"


# --- table-driven generators: lock the catalog rows ---------------------------

def test_fastener_table_dims():
    """A wrong table edit (e.g. a fat-fingered head diameter) must fail here."""
    with Worker() as w:
        w.call("new_document", name="md_fastener")

        # socket-head cap screw: head_diameter + head_height come from the ISO row
        m3 = w.call("add_fastener", kind="socket_head_cap_screw", size="M3", length=10)
        _close(m3["major_diameter"], 3.0, label="M3 SHCS major")
        _close(m3["pitch"], 0.5, label="M3 pitch")
        _close(m3["head_diameter"], 5.5, label="M3 head_dia")
        _close(m3["head_height"], 3.0, label="M3 head_h")

        m8 = w.call("add_fastener", kind="socket_head_cap_screw", size="M8", length=20)
        _close(m8["major_diameter"], 8.0, label="M8 major")
        _close(m8["pitch"], 1.25, label="M8 pitch")
        _close(m8["head_diameter"], 13.0, label="M8 head_dia")

        # hex nut: head_diameter carries the across-flats wrench size, head_height the nut height
        n6 = w.call("add_fastener", kind="hex_nut", size="M6")
        _close(n6["head_diameter"], 10.0, label="M6 nut AF")
        _close(n6["head_height"], 5.2, label="M6 nut h")

        # washer: head_diameter = OD, head_height = thickness
        wsh = w.call("add_fastener", kind="washer", size="M5")
        _close(wsh["head_diameter"], 10.0, label="M5 washer OD")
        _close(wsh["head_height"], 1.0, label="M5 washer thk")


def test_bearing_table_dims():
    with Worker() as w:
        w.call("new_document", name="md_bearing")
        for desig, bore, od, width in [("608", 8.0, 22.0, 7.0), ("6200", 10.0, 30.0, 9.0),
                                       ("625", 5.0, 16.0, 5.0), ("6000", 10.0, 26.0, 8.0)]:
            r = w.call("add_bearing", designation=desig)
            _close(r["bore"], bore, label=f"{desig} bore")
            _close(r["outer_diameter"], od, label=f"{desig} OD")
            _close(r["width"], width, label=f"{desig} width")


# --- runner (mirrors tests/test_worker.py) ------------------------------------

def _discover():
    return [(n, fn) for n, fn in sorted(globals().items())
            if n.startswith("test_") and callable(fn)]


def main():
    tests = _discover()
    failures = []
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:40s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:40s} ({time.time() - t0:.2f}s)")
    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()
