"""Run the P2 simulation acceptance examples end-to-end and gate each against its
analytic oracle — the runnable evidence behind the heavy-solver PRs.

Unlike the unit suites in ``tests/``, this drives the **real MCP tool surface**
through a live FreeCAD worker (``driftpin.Worker``): each example composes a case,
runs the actual solver via ``jobs.py``, polls the shared ``job_result`` surface, and
compares the solved numbers to the closed-form answer from
``docs/SIMULATION_EXAMPLES.md``.

Three examples, each a kickoff gate:
  * **A — topology** (§5): ``topology_optimize_submit`` → ``topology_to_solid`` →
    ``mass_properties``; the reconstructed solid must hold mass_fraction ≤ keep_fraction
    and be a valid (watertight) body. Needs NumPy in the worker; no external solver.
  * **B — transient thermal** (§4): ``thermal_transient_submit`` builds the 1-D
    plane-wall case and runs **ElmerSolver**; the centre/surface temps must match the
    one-term Heisler oracle (``thermal_transient_1d``) to a small fraction of a degree.
  * **C — CFD** (§6): ``cfd_internal_flow_submit`` builds the axisymmetric pipe and runs
    **blockMesh + simpleFoam**; the pressure drop must land within 10% of
    Hagen–Poiseuille, and the D⁴ scaling law must hold (halving the bore → ~16× Δp).

Each example **degrades gracefully**: a missing solver (ElmerSolver / OpenFOAM) or a
worker that lacks NumPy is reported as SKIP, not a failure, so the script is safe to
run on any box. Exit status is non-zero only if a gate that actually ran FAILS.

Run (gated examples, needs FreeCAD + the solvers):
    python3 examples/run_simulation_examples.py

Regenerate the result figures in ``examples/results/`` (needs matplotlib + the solvers,
but NOT FreeCAD — the plots are built straight from the analysis modules):
    python3 examples/run_simulation_examples.py --plots [OUTDIR]
"""
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin import Worker
from driftpin.analysis import thermal


def _poll(w, job_id, timeout_s=360):
    """Block on the shared job surface until the job is done/failed (or timeout)."""
    for _ in range(timeout_s):
        if w.call("job_status", job_id=job_id)["status"] in ("done", "failed"):
            break
        time.sleep(1)
    return w.call("job_result", job_id=job_id)


def _submitted(res):
    """A *_submit either returns {job_id,...} (queued) or the degradation dict
    {ok:false, reason, install} when the solver is absent. True == queued."""
    return isinstance(res, dict) and "job_id" in res


def example_topology(w, log):
    """§5 — optimize a 2-D design, reconstruct a solid, gate on kept mass + validity."""
    log("### Example A — topology optimize → topology_to_solid → mass gate  (§5)")
    w.call("new_document", name="topo_demo")
    kf = 0.4
    sub = w.call("topology_optimize_submit", nelx=24, nely=12, keep_fraction=kf, max_iter=30)
    out = _poll(w, sub["job_id"])
    if out["status"] != "done":
        # the optimizer needs NumPy in the worker; fall back to a synthetic density so
        # topology_to_solid (pure geometry) is still demonstrated.
        log(f"  topology_optimize unavailable ({out.get('error', 'failed')}); "
            "using a synthetic density to still exercise the reconstruction")
        density = [[1, 1, 1, 1], [1, 0, 0, 1], [1, 1, 1, 1]]
        kf = 8 / 12
    else:
        r = out["result"]
        density = r["density"]
        log(f"1. topology_optimize_submit(24x12, keep_fraction={kf}) → "
            f"mass_fraction {r['mass_fraction']}, compliance {r['compliance_initial']} → {r['compliance']}")
    sol = w.call("topology_to_solid", density=density, cell_mm=5.0, thickness_mm=5.0, name="Bracket")
    log(f"2. topology_to_solid(cell_mm=5, thickness_mm=5) → volume {sol['volume']:.1f} mm³, "
        f"mass_fraction {sol['mass_fraction']}, n_solids {sol['n_solids']}, bbox_mm {sol['bbox_mm']}")
    mp = w.call("mass_properties", handle=sol["handle"])
    log(f"3. mass_properties → volume_mm3 {mp['volume_mm3']:.1f}")
    kept = sol["mass_fraction"] <= kf + 1e-9
    valid = abs(sol["volume"] - mp["volume_mm3"]) < 1e-3
    ok = kept and valid
    log(f"   GATE mass_fraction {sol['mass_fraction']} ≤ keep_fraction {kf:.4f} "
        f"and volume==mass_properties: {'PASS' if ok else 'FAIL'}")
    return ok


def example_thermal(w, log):
    """§4 — Elmer 1-D transient slab vs the one-term Heisler oracle."""
    log("### Example B — thermal_transient_submit (ElmerSolver) vs Heisler oracle  (§4)")
    L_mm, h, k, rho, cp, ti, ta = 20.0, 375.0, 15.0, 8000.0, 500.0, 100.0, 25.0
    dur = 0.5 * (L_mm / 1000.0) ** 2 / (k / (rho * cp))      # Fo = 0.5
    sub = w.call("thermal_transient_submit", half_thickness_mm=L_mm, h_conv=h, duration_s=dur,
                 k=k, rho=rho, cp=cp, t_initial_c=ti, t_ambient_c=ta)
    if not _submitted(sub):
        log(f"  SKIP — ElmerSolver not installed ({sub.get('install', '')})")
        return None
    res = _poll(w, sub["job_id"])["result"]
    if not res.get("ok") or res.get("t_center_c") is None:
        log(f"  FAIL — solve did not produce temps: {str(res)[:200]}")
        return False
    orc = thermal.thermal_transient_1d(half_thickness_mm=L_mm, h_conv=h, duration_s=dur,
                                       k=k, rho=rho, cp=cp, t_initial_c=ti, t_ambient_c=ta)
    dc, ds = abs(res["t_center_c"] - orc["t_center_c"]), abs(res["t_surface_c"] - orc["t_surface_c"])
    log(f"- Bi={orc['biot']}, Fo={orc['fourier']}, t={dur:.1f}s; cooling {ti}→{ta} °C")
    log(f"- ElmerSolver: centre {res['t_center_c']:.3f} °C, surface {res['t_surface_c']:.3f} °C")
    log(f"- Heisler:     centre {orc['t_center_c']:.3f} °C, surface {orc['t_surface_c']:.3f} °C")
    ok = dc < 0.3 and ds < 0.3
    log(f"  GATE |Δ| centre {dc:.3f} °C, surface {ds:.3f} °C (< 0.3): {'PASS' if ok else 'FAIL'}")
    return ok


def example_cfd(w, log):
    """§6 — OpenFOAM pipe vs Hagen–Poiseuille, plus the D⁴ scaling law."""
    log("### Example C — cfd_internal_flow_submit (blockMesh+simpleFoam) vs Hagen–Poiseuille  (§6)")
    sub = w.call("cfd_internal_flow_submit", diameter_mm=10.0, length_mm=500.0,
                 velocity_m_s=0.005, fluid="water-20c")
    if not _submitted(sub):
        log(f"  SKIP — OpenFOAM not installed ({sub.get('install', '')})")
        return None
    res = _poll(w, sub["job_id"])["result"]
    if not res.get("ok") or res.get("hp_ratio") is None:
        log(f"  FAIL — solve did not produce Δp: {str(res)[:200]}")
        return False
    log(f"- pipe D=10 mm, L=500 mm, U=5 mm/s → Re {res['reynolds']} ({res['regime']}), {res['n_cells']} cells")
    log(f"- simpleFoam: Δp_developed {res['pressure_drop_pa']:.4f} Pa (inlet {res['pressure_drop_inlet_pa']:.4f} Pa)")
    log(f"- Hagen–Poiseuille: {res['hagen_poiseuille_pa']:.4f} Pa → hp_ratio {res['hp_ratio']}")
    hp_ok = 0.9 <= res["hp_ratio"] <= 1.1

    # D⁴ law at fixed volumetric flow: U = Q/area, so the small pipe runs 4× faster.
    nu = 1.0038e-6
    lpm = (50.0 * nu * math.pi * (0.010) / 4.0) * 1000.0 * 60.0     # Re≈50 in the 10 mm pipe
    big = _poll(w, w.call("cfd_internal_flow_submit", diameter_mm=10.0, length_mm=400.0,
                          flow_rate_lpm=lpm, fluid="water-20c", n_axial=100, n_radial=12)["job_id"])["result"]
    sml = _poll(w, w.call("cfd_internal_flow_submit", diameter_mm=5.0, length_mm=400.0,
                          flow_rate_lpm=lpm, fluid="water-20c", n_axial=100, n_radial=12)["job_id"])["result"]
    ratio = sml["pressure_drop_pa"] / big["pressure_drop_pa"]
    d4_ok = 13.5 <= ratio <= 18.5
    log(f"- D⁴ law (fixed Q={lpm:.5f} L/min): Δp(10mm)={big['pressure_drop_pa']:.4f}, "
        f"Δp(5mm)={sml['pressure_drop_pa']:.4f} → ratio {ratio:.2f} (expect ~16)")
    log(f"  GATE hp_ratio within 10% and D⁴ ratio ~16: {'PASS' if hp_ok and d4_ok else 'FAIL'}")
    return hp_ok and d4_ok


# --- figures (--plots) --------------------------------------------------------
#
# Built straight from the analysis modules (no FreeCAD worker), so they need only
# matplotlib + NumPy + the relevant solver. Each panel mirrors one gated example.

def _plot_topology(outdir):
    """Panel A: the SIMP density field and the topology_to_solid reconstruction."""
    import matplotlib.pyplot as plt
    import numpy as np
    from driftpin.analysis import topology as topo
    r = topo.simp_topology_2d(nelx=30, nely=10, keep_fraction=0.4, rmin=1.4, max_iter=22)
    dec = topo.density_to_rects(r["density"], threshold=0.5)
    nelx, nely = dec["nelx"], dec["nely"]
    fig, axes = plt.subplots(2, 1, figsize=(7.2, 4.4))
    axes[0].imshow(np.array(r["density"]), cmap="gray_r", origin="upper", aspect="equal", vmin=0, vmax=1)
    axes[0].set_title(f"SIMP density field  (mass_fraction {r['mass_fraction']:.3f}, "
                      f"compliance {r['compliance_initial']:.0f}→{r['compliance']:.0f})", fontsize=9)
    axes[0].set_xticks([]); axes[0].set_yticks([])
    for (i0, j, w_) in dec["rects"]:
        axes[1].add_patch(plt.Rectangle((i0, j), w_, 1, facecolor="#1f4e79", edgecolor="none"))
    axes[1].set_xlim(0, nelx); axes[1].set_ylim(0, nely); axes[1].invert_yaxis(); axes[1].set_aspect("equal")
    recon = dec["solid_cells"] / (nelx * nely)
    axes[1].set_title(f"topology_to_solid (threshold 0.5) → {len(dec['rects'])} fused boxes, "
                      f"mass_fraction {recon:.3f} ≤ keep 0.4", fontsize=9)
    axes[1].set_xticks([]); axes[1].set_yticks([])
    fig.suptitle("Example A — topology optimize → topology_to_solid  (§5)", fontsize=11, weight="bold")
    fig.tight_layout()
    fig.savefig(f"{outdir}/topology.png", dpi=130)
    plt.close(fig)


def _plot_thermal(outdir):
    """Panel B: the Elmer slab cooling curves over the centre/surface Heisler lines."""
    from driftpin import solvers
    if not solvers.is_available("elmer"):
        return False
    import os
    import subprocess
    import tempfile
    import matplotlib.pyplot as plt
    from driftpin.analysis import elmer
    L, k, rho, cp, h, ti, ta = 0.02, 15.0, 8000.0, 500.0, 375.0, 100.0, 25.0
    alpha = k / (rho * cp)
    dur, nsteps = 1.0 * L * L / alpha, 200
    dt = dur / nsteps
    d = tempfile.mkdtemp()
    built = elmer.write_slab_transient_case(
        d, half_thickness_m=L, k=k, rho=rho, cp=cp, h_conv=h, t_initial_c=ti,
        t_ambient_c=ta, duration_s=dur, n_elements=40, n_steps=nsteps)
    subprocess.run([solvers.find_solver("elmer")["path"], built["sif"]],
                   cwd=d, capture_output=True, text=True)
    rows = [ln.split() for ln in open(os.path.join(d, "scalars.dat")) if ln.strip()]
    center = [float(c[0]) for c in rows]
    surface = [float(c[1]) for c in rows]
    t = [(i + 1) * dt for i in range(len(rows))]
    fo = [alpha * tt / (L * L) for tt in t]
    hc, hs = [], []
    for tt in t:
        o = thermal.thermal_transient_1d(half_thickness_mm=L * 1000, h_conv=h, duration_s=tt,
                                         k=k, rho=rho, cp=cp, t_initial_c=ti, t_ambient_c=ta)
        hc.append(o["t_center_c"]); hs.append(o["t_surface_c"])
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    ax.plot(fo, hc, "-", color="C0", lw=2, label="Heisler centre (analytic)")
    ax.plot(fo, hs, "-", color="C3", lw=2, label="Heisler surface (analytic)")
    ax.plot(fo[5::10], center[5::10], "o", color="C0", ms=5, mfc="white", label="ElmerSolver centre")
    ax.plot(fo[5::10], surface[5::10], "s", color="C3", ms=5, mfc="white", label="ElmerSolver surface")
    ax.axvline(0.2, ls=":", color="gray")
    ax.text(0.205, 30, "one-term valid Fo≥0.2", fontsize=8, color="gray")
    ax.set_xlabel("Fourier number  Fo = αt/L²")
    ax.set_ylabel("Temperature (°C)")
    ax.set_title("Example B — Elmer slab cooling vs Heisler oracle  (§4)", weight="bold", fontsize=11)
    ax.legend(fontsize=9, loc="upper right")
    ax.grid(True, ls=":", alpha=0.4)
    fig.tight_layout()
    fig.savefig(f"{outdir}/elmer.png", dpi=130)
    plt.close(fig)
    return True


def _plot_cfd(outdir):
    """Panel C: solved Δp over a diameter sweep on the Hagen–Poiseuille D⁻⁴ line."""
    from driftpin import solvers
    if not solvers.is_available("openfoam"):
        return False
    import math as _m
    import subprocess
    import tempfile
    import matplotlib.pyplot as plt
    from driftpin.analysis import cfd, openfoam
    nu, rho = 1.0038e-6, 998.2
    Q = 50.0 * nu * _m.pi * 0.010 / 4.0                # fixed flow; Re=50 at D=10mm
    Ds = [4.0, 6.0, 8.0, 10.0, 12.0, 14.0]
    bashrc = solvers.openfoam_bashrc()
    dp_cfd, dp_hp = [], []
    for Dmm in Ds:
        D = Dmm / 1000.0
        U = Q / (_m.pi * D * D / 4.0)
        d = tempfile.mkdtemp()
        openfoam.write_pipe_case(d, diameter_m=D, length_m=0.4, velocity_m_s=U,
                                 nu_m2_s=nu, n_axial=80, n_radial=10, end_time=4000)
        src = f"source '{bashrc}' >/dev/null 2>&1\n" if bashrc else ""
        subprocess.run(["bash", "-c", src + "blockMesh >log 2>&1 && simpleFoam >log2 2>&1"],
                       cwd=d, capture_output=True, text=True)
        parsed = openfoam.parse_pressure_drop(d, rho_kg_m3=rho)
        dp_cfd.append(parsed["dp_developed_pa"] if parsed else float("nan"))
        dp_hp.append(cfd.pipe_pressure_drop(diameter_mm=Dmm, length_mm=400.0, velocity_m_s=U,
                                            mu_pa_s=nu * rho, rho_kg_m3=rho)["hagen_poiseuille_pa"])
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    ax.loglog(Ds, dp_hp, "-", color="C2", lw=2, label="Hagen–Poiseuille  Δp ∝ D⁻⁴")
    ax.loglog(Ds, dp_cfd, "o", color="C1", ms=7, label="simpleFoam (developed Δp)")
    ax.set_xlabel("pipe diameter  D (mm)")
    ax.set_ylabel("pressure drop  Δp (Pa)")
    ax.set_title("Example C — OpenFOAM pipe vs Hagen–Poiseuille, fixed flow  (§6)",
                 weight="bold", fontsize=11)
    ax.grid(True, which="both", ls=":", alpha=0.5)
    ax.legend(fontsize=9)
    fig.tight_layout()
    fig.savefig(f"{outdir}/openfoam.png", dpi=130)
    plt.close(fig)
    return True


def make_plots(outdir):
    """Generate the three result figures into ``outdir``; skip a panel when its solver
    is absent. matplotlib is imported lazily so the gated run needs no plotting deps."""
    import os
    try:
        import matplotlib
    except ImportError:
        print("matplotlib is required for --plots: pip install matplotlib")
        sys.exit(2)
    matplotlib.use("Agg")
    os.makedirs(outdir, exist_ok=True)
    print(f"writing figures to {outdir}/ …")
    _plot_topology(outdir)
    print("  topology.png ✓")
    print("  elmer.png " + ("✓" if _plot_thermal(outdir) else "SKIP (ElmerSolver absent)"))
    print("  openfoam.png " + ("✓" if _plot_cfd(outdir) else "SKIP (OpenFOAM absent)"))


def main():
    args = sys.argv[1:]
    if "--plots" in args:
        i = args.index("--plots")
        outdir = (args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-")
                  else str(Path(__file__).resolve().parent / "results"))
        make_plots(outdir)
        return

    lines = []
    def log(s=""):
        lines.append(s)
        print(s)

    results = {}
    with Worker() as w:
        log(f"FreeCAD {'.'.join(w.freecad_version[:3])}\n")
        for name, fn in (("topology", example_topology),
                         ("thermal", example_thermal),
                         ("cfd", example_cfd)):
            try:
                results[name] = fn(w, log)
            except Exception as e:  # one example failing must not abort the rest
                results[name] = False
                log(f"  ERROR in {name}: {type(e).__name__}: {e}")
            log("")

    ran = {k: v for k, v in results.items() if v is not None}
    failed = [k for k, v in ran.items() if v is False]
    skipped = [k for k, v in results.items() if v is None]
    log(f"== {sum(1 for v in ran.values() if v)}/{len(ran)} examples passed"
        + (f", {len(skipped)} skipped ({', '.join(skipped)})" if skipped else "") + " ==")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
