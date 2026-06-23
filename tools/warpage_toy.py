#!/usr/bin/env python3
"""End-to-end toy demo of the **injection-molding warpage** thermo-elastic post-step
(GitHub issue #113 Part B).

Builds a thin plate, imposes the frozen-in differential cooling as a through-thickness
temperature field, and runs the CalculiX (``ccx``) thermo-elastic free-distortion
solve via :mod:`driftpin.analysis.warpage` — exactly the deck/parse/gate the worker
uses. Renders the deformed mid-surface profile (the bow) against the closed-form
plate-curvature twin, for an **asymmetric** field (bows) and a **balanced** field
(stays flat) side by side.

Self-contained: a structured C3D8I hex mesh is generated in pure Python (no FreeCAD),
so the toy runs anywhere ``ccx`` is on PATH. The GPL solver is held at the subprocess
boundary (never imported). Usage::

    python3 tools/warpage_toy.py                  # writes artifacts/molding_warpage_bow.png
    python3 tools/warpage_toy.py --dT 150 --span 80

The plot needs ``matplotlib``. Requires ``ccx`` (``apt install calculix-ccx``).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from driftpin import solvers                             # noqa: E402
from driftpin.analysis import warpage as W               # noqa: E402

# Abaqus/CalculiX C3D8 corner order: bottom face CCW, then top face CCW.
_HEX = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
        (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]


def _hex_plate_mesh(path, *, L, Wd, t, nx, ny, nz):
    """Write a structured C3D8I hex-plate mesh.inp and return {id: (x, y, z)}."""
    def nid(i, j, k):
        return 1 + i + (nx + 1) * (j + (ny + 1) * k)

    nodes = {nid(i, j, k): (L * i / nx, Wd * j / ny, t * k / nz)
             for k in range(nz + 1) for j in range(ny + 1) for i in range(nx + 1)}
    lines = ["*Node, NSET=Nall"]
    for n in sorted(nodes):
        x, y, z = nodes[n]
        lines.append(f"{n}, {x:.6f}, {y:.6f}, {z:.6f}")
    lines.append("*Element, TYPE=C3D8I, ELSET=Evolumes")
    eid = 1
    for k in range(nz):
        for j in range(ny):
            for i in range(nx):
                conn = [nid(i + dx, j + dy, k + dz) for dx, dy, dz in _HEX]
                lines.append(f"{eid}, " + ", ".join(str(c) for c in conn))
                eid += 1
    open(path, "w").write("\n".join(lines) + "\n")
    return nodes


def _read_frd_field(frd_path):
    """{node: (ux, uy, uz)} from a ccx .frd DISP block (full field, for plotting)."""
    field, in_disp = {}, False
    with open(frd_path) as f:
        for ln in f:
            if not in_disp:
                if ln.startswith(" -4") and "DISP" in ln:
                    in_disp = True
                continue
            if ln.startswith(" -3"):
                break
            if ln.startswith(" -1"):
                try:
                    node = int(ln[3:13])
                    field[node] = (float(ln[13:25]), float(ln[25:37]), float(ln[37:49]))
                except ValueError:
                    continue
    return field


def _solve(case_dir, nodes, *, dT, cte, E, nu):
    os.makedirs(case_dir, exist_ok=True)
    built = W.write_warpage_case(case_dir, nodes=nodes, youngs_mpa=E, poisson=nu,
                                 cte_per_k=cte, dT_through_k=dT)
    ccx = solvers.ccx_bin()
    argv = [ccx] + built["argv"][1:]
    subprocess.run(argv, cwd=case_dir, capture_output=True, text=True, timeout=300)
    frd = os.path.join(case_dir, built["job_name"] + ".frd")
    parsed = W.parse_warp_frd(frd, axis_index=built["axis_index"])
    field = _read_frd_field(frd)
    return built, parsed, field


def _centerline_profile(nodes, field, *, span_axis=0, thick_axis=2):
    """(x, deflection) along the part centreline at the mid-thickness, bottom-y row —
    sorted by the span coordinate, for a clean side-view bow curve."""
    in_plane = [a for a in range(3) if a != thick_axis]
    y_axis = [a for a in in_plane if a != span_axis][0]
    ys = [nodes[n][y_axis] for n in nodes]
    zs = [nodes[n][thick_axis] for n in nodes]
    y0, zmid = min(ys), 0.5 * (min(zs) + max(zs))
    pts = []
    for n, p in nodes.items():
        if abs(p[y_axis] - y0) < 1e-6 and abs(p[thick_axis] - zmid) < 1e-6 and n in field:
            pts.append((p[span_axis], field[n][thick_axis]))
    pts.sort()
    return [x for x, _ in pts], [d for _, d in pts]


def main():
    ap = argparse.ArgumentParser(description="injection-molding warpage toy")
    ap.add_argument("--span", type=float, default=60.0, help="flow length, mm")
    ap.add_argument("--width", type=float, default=12.0, help="part width, mm")
    ap.add_argument("--thickness", type=float, default=1.2, help="wall thickness, mm")
    ap.add_argument("--dT", type=float, default=120.0,
                    help="through-thickness ΔT at ejection, K (asymmetric cooling)")
    ap.add_argument("--cte", type=float, default=7e-5, help="resin CTE, 1/K")
    ap.add_argument("--youngs-mpa", type=float, default=3200.0)
    ap.add_argument("--poisson", type=float, default=0.35)
    ap.add_argument("--out", default=None, help="output PNG (default artifacts/...)")
    args = ap.parse_args()

    if solvers.ccx_bin() is None:
        print("ccx (CalculiX) not found — apt install calculix-ccx, or set "
              "DRIFTPIN_CALCULIX_PATH", file=sys.stderr)
        return 1

    base = tempfile.mkdtemp(prefix="warpage_toy_")
    nx = max(20, int(args.span / 2))
    # nz=4 through-thickness layers: C3D8I needs ≳4 layers to resolve a strong thermal
    # gradient's bending (nz=2 under-predicts the curvature ~20 %; nz=4 lands within ~3 %).
    nodes = _hex_plate_mesh(os.path.join(base, "src.inp"), L=args.span, Wd=args.width,
                            t=args.thickness, nx=nx, ny=4, nz=4)
    common = dict(cte=args.cte, E=args.youngs_mpa, nu=args.poisson)

    def run(tag, dT):
        cd = os.path.join(base, tag)
        os.makedirs(cd)
        import shutil
        shutil.copy(os.path.join(base, "src.inp"), os.path.join(cd, "mesh.inp"))
        return _solve(cd, nodes, dT=dT, **common)

    b_asym, p_asym, f_asym = run("asym", args.dT)
    b_bal, p_bal, f_bal = run("bal", 0.0)

    twin = W.free_plate_thermal_bow(span_mm=b_asym["span_mm"],
                                    thickness_mm=b_asym["thickness_mm"],
                                    dT_through_k=args.dT, cte_per_k=args.cte)
    gate = W.warpage_gate(p_asym, span_mm=b_asym["span_mm"],
                          analytic_bow_mm=twin["bow_mm"])
    print(f"asymmetric ΔT={args.dT:g} K  → max warp {p_asym['max_warp_mm']:.3f} mm "
          f"(analytic {twin['bow_mm']:.3f} mm, faithful={gate['warp_faithful']})")
    print(f"balanced   ΔT=0   K  → max warp {p_bal['max_warp_mm']:.4f} mm  "
          f"(gate pass={W.warpage_gate(p_bal, span_mm=b_bal['span_mm'])['pass']})")

    xa, da = _centerline_profile(nodes, f_asym)
    xb, db = _centerline_profile(nodes, f_bal)
    # analytic overlay: the 3-2-1 pins the out-of-plane dof at BOTH span-end corners
    # (A fully, B in thickness), so the ccx bow is referenced to that chord — a sagitta
    # parabola, zero at both ends, peak κ·L²/8 at mid-span. Match its sign to the solve.
    import numpy as np
    kappa = twin["curvature_per_mm"]
    x0, x1 = min(xa), max(xa)
    sign = -1.0 if (da and da[len(da) // 2] < 0) else 1.0
    xarc = np.linspace(x0, x1, 100)
    darc = sign * 0.5 * kappa * (xarc - x0) * (x1 - xarc)   # 0 at ends, ±κL²/8 at centre

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed — skipping plot", file=sys.stderr)
        return 0

    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.plot(xa, da, "o-", ms=3, lw=1.6, color="#c0392b",
            label=f"ccx warp, asymmetric ΔT={args.dT:g} K  (max {p_asym['max_warp_mm']:.2f} mm)")
    ax.plot(xarc, darc, "--", lw=1.2, color="#7f8c8d",
            label=f"analytic plate twin  κ=α·ΔT/h  ({twin['bow_mm']:.2f} mm)")
    ax.plot(xb, db, "s-", ms=3, lw=1.4, color="#2471a3",
            label=f"ccx warp, balanced ΔT=0  (max {p_bal['max_warp_mm']:.3f} mm — flat)")
    ax.axhline(0, color="k", lw=0.5)
    ax.set_xlabel("position along flow length  x  [mm]")
    ax.set_ylabel("out-of-plane deflection  w  [mm]")
    ax.set_title("Injection-molding warpage — thermo-elastic free distortion (ccx)\n"
                 f"{args.span:g}×{args.width:g}×{args.thickness:g} mm plate, "
                 f"E={args.youngs_mpa:g} MPa, ν={args.poisson:g}, α={args.cte:g}/K")
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(alpha=0.3)
    fig.tight_layout()

    out = args.out
    if out is None:
        adir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "artifacts")
        os.makedirs(adir, exist_ok=True)
        out = os.path.join(adir, "molding_warpage_bow.png")
    fig.savefig(out, dpi=110)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
