"""Render the optics engines' results to PNGs for visual inspection.

Headless (Agg) — writes to examples/optics_gallery/*.png. Covers both lanes:
  sequential (optiland, in-process): singlet layout, spot diagram, optimize before/after
  non-sequential (KrakenOS, GPL subprocess via the production runner): prism TIR ray paths

Run:  .venv/bin/python examples/optics_gallery.py
"""
import json
import os
import subprocess
import sys
import tempfile

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "optics_gallery")
os.makedirs(OUT, exist_ok=True)


def _singlet(r1=50.0, r2=-50.0, epd=20.0):
    from optiland.optic import Optic
    L = Optic()
    L.add_surface(index=0, thickness=np.inf)
    L.add_surface(index=1, radius=r1, thickness=4.0, material="N-BK7", is_stop=True)
    L.add_surface(index=2, radius=r2, thickness=45.0, material="air")
    L.add_surface(index=3)
    L.set_aperture(aperture_type="EPD", value=epd)
    L.set_field_type(field_type="angle")
    L.add_field(y=0.0)
    L.add_wavelength(value=0.5876, is_primary=True)
    L.image_solve()
    return L


def fig_lens_layout():
    L = _singlet()
    fig, ax = L.draw(fields=[(0, 0)], wavelengths="primary", num_rays=9,
                     title="optiland · N-BK7 singlet (EPD 20, f≈%.1f mm)" % float(L.paraxial.f2()))
    p = os.path.join(OUT, "01_lens_layout.png")
    fig.savefig(p, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return p


def fig_spot():
    from optiland.analysis import SpotDiagram
    L = _singlet(epd=10.0)
    sd = SpotDiagram(L, fields=[(0, 0)], wavelengths=[0.5876], num_rings=14)
    rms = float(np.ravel(np.array(sd.rms_spot_radius()))[0]) * 1000.0
    fig = sd.view()
    fig = fig[0] if isinstance(fig, tuple) else (fig or plt.gcf())
    fig.suptitle("optiland · on-axis spot (RMS %.1f µm)" % rms, y=1.02)
    p = os.path.join(OUT, "02_spot_diagram.png")
    fig.savefig(p, dpi=130, bbox_inches="tight")
    plt.close("all")
    return p


def fig_optimize_before_after():
    from ankusdrive.analysis import optics_design as od
    system = {"surfaces": [
        {"radius": 80.0, "thickness": 4.0, "material": "N-BK7", "stop": True},
        {"radius": -80.0, "thickness": 96.0, "material": "air"}],
        "epd": 10.0}
    res = od.optimize(system,
                      variables=[{"type": "radius", "surface": 1},
                                 {"type": "radius", "surface": 2}],
                      targets=[{"operand": "f2", "target": 100.0}])
    r_opt = res["surfaces"]

    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    for ax, (r1, r2, tag, efl) in zip(axes, [
            (80.0, -80.0, "before", res["before"]["efl_mm"]),
            (r_opt[0]["radius"], r_opt[1]["radius"], "after", res["after"]["efl_mm"])]):
        L = _singlet(r1=r1, r2=r2, epd=10.0)
        L.draw(fields=[(0, 0)], wavelengths="primary", num_rays=9, ax=ax)
        ax.set_title("%s · R=(%.1f, %.1f) · EFL %.2f mm" % (tag, r1, r2, efl))
    fig.suptitle("optiland optimize → target EFL 100 mm  (converged in %d evals)" % res["n_fev"],
                 y=1.03, fontsize=12)
    p = os.path.join(OUT, "03_optimize_before_after.png")
    fig.savefig(p, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return p, res


def _prism_stl(path, L=20.0, W=20.0):
    A, B, C = (0.0, 0.0), (L, 0.0), (0.0, L)
    x0, x1 = -W / 2, W / 2
    V = []

    def v(x, y, z):
        V.append((x, y, z)); return len(V) - 1
    a0, b0, c0 = v(x0, *A), v(x0, *B), v(x0, *C)
    a1, b1, c1 = v(x1, *A), v(x1, *B), v(x1, *C)
    tris = [(a0, c0, b0), (a1, b1, c1), (a0, c1, c0), (a0, a1, c1),
            (a0, b0, b1), (a0, b1, a1), (b0, c0, c1), (b0, c1, b1)]

    def nrm(p, q, r):
        n = np.cross(np.array(q) - np.array(p), np.array(r) - np.array(p))
        m = np.linalg.norm(n)
        return n / m if m else n
    with open(path, "w") as f:
        f.write("solid prism\n")
        for i, j, k in tris:
            p, q, r = V[i], V[j], V[k]
            n = nrm(p, q, r)
            f.write(f"facet normal {n[0]:.6e} {n[1]:.6e} {n[2]:.6e}\n outer loop\n")
            for P in (p, q, r):
                f.write(f"  vertex {P[0]:.6e} {P[1]:.6e} {P[2]:.6e}\n")
            f.write(" endloop\nendfacet\n")
        f.write("endsolid prism\n")
    return L


def fig_prism_tir():
    """Trace a bundle through the prism via the PRODUCTION GPL runner (subprocess,
    KrakenOS never imported here) and draw the ray polylines in the Y-Z plane."""
    stl = tempfile.NamedTemporaryFile(suffix=".stl", delete=False)
    stl.close()
    Ltri = _prism_stl(stl.name)
    runner = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "ankusdrive", "optics_gpl_runner.py")
    rays = [{"origin": [0.0, 6.0 + dy, -4.0], "dir": [0, 0, 1.0]} for dy in (-3, 0, 3, 6)]
    problem = {"problem": "solid_trace", "stl_path": stl.name, "glass": "BK7",
               "wavelength_um": 0.55, "solid": {"diameter": 40, "thickness": 30, "axis_move": 1},
               "rays": rays, "want_paths": True}
    proc = subprocess.run([sys.executable, runner], input=json.dumps(problem),
                          capture_output=True, text=True, timeout=120)
    res = json.loads(proc.stdout.partition("@@JSON@@")[2].partition("@@END@@")[0])

    fig, ax = plt.subplots(figsize=(7, 6))
    # prism cross-section in (z=horizontal, y=vertical): A(0,0) B(y=L) C(z=L)
    tri = np.array([[0, 0], [0, Ltri], [Ltri, 0], [0, 0]])  # (z, y)
    ax.fill(tri[:, 0], tri[:, 1], color="#bfe3ff", alpha=0.6, zorder=1,
            label="BK7 prism (n=1.5168)")
    ax.plot(tri[:, 0], tri[:, 1], color="#3a7bd5", lw=1.5, zorder=2)
    paths = res.get("paths") or []
    for poly in paths:
        a = np.asarray(poly)                       # (k,3) XYZ
        ax.plot(a[:, 2], a[:, 1], color="#d9480f", lw=1.6, zorder=3)
        ax.scatter(a[:, 2], a[:, 1], s=10, color="#d9480f", zorder=4)
    ax.annotate("in (+Z)", (-3.8, 6.6), color="#d9480f", fontsize=10)
    ax.annotate("TIR → 90° turn → out (−Y)", (6.5, -3.2), color="#d9480f", fontsize=10)
    ax.set_aspect("equal")
    ax.set_xlabel("Z (mm)"); ax.set_ylabel("Y (mm)")
    ax.set_title("KrakenOS (subprocess) · 45° prism total internal reflection\n"
                 "%d/%d rays valid · mean turn %.1f°"
                 % (res.get("n_valid", 0), res.get("n_launched", 0),
                    res.get("mean_turn_deg") or 0.0))
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(alpha=0.25)
    p = os.path.join(OUT, "04_prism_tir.png")
    fig.savefig(p, dpi=130, bbox_inches="tight")
    plt.close(fig)
    os.unlink(stl.name)
    return p, res


if __name__ == "__main__":
    print("layout :", fig_lens_layout())
    print("spot   :", fig_spot())
    p3, opt = fig_optimize_before_after()
    print("optim  :", p3, "| EFL %.3f mm in %d evals" % (opt["after"]["efl_mm"], opt["n_fev"]))
    p4, tir = fig_prism_tir()
    print("prism  :", p4, "| mean turn %.1f° (%d/%d valid)"
          % (tir.get("mean_turn_deg") or 0, tir.get("n_valid", 0), tir.get("n_launched", 0)))
    print("\ngallery dir:", OUT)
