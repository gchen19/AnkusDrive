#!/usr/bin/env python3
"""End-to-end toy demo of the **openInjMoldSim** (OF7-org) injection-molding fill
solver, the headline path of GitHub issue #105.

Generates a small 2-D plaque-cavity case programmatically (Cross-WLF + 2-domain
Tait pulled from the #106 materials corpus), runs ``blockMesh`` → ``setFields`` →
``openInjMoldSim -fillEnd`` on the from-source OpenFOAM-7 build (auto-resolved by
``ankusdrive.solvers``), parses the fill result through the same ``molding_fill``
parser/gate the worker uses, and renders an animated GIF of the melt front
advancing across the cavity.

This is the runnable proof that the GPL solver is actually wired in — not the
interFoam fallback. Usage::

    python3 tools/openinjmoldsim_toy.py            # default toy, writes to ./build/oims_toy
    python3 tools/openinjmoldsim_toy.py --case-dir /tmp/foo --resin HDPE --no-run  # gen only

The solver is held at the subprocess boundary (GPL-3.0); we never import it.
Requires the OF7-org build (see ``tools/build_openinjmoldsim.sh`` and
``docs/MOLDING_FILL_SOLVER.md``). The GIF needs ``matplotlib`` + ``PIL`` (Pillow).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ankusdrive import solvers                      # noqa: E402
from ankusdrive.analysis import molding_fill as mf  # noqa: E402


def run_solver(case_dir: str, bashrc: str, fill_end: float = 0.98) -> int:
    """Run blockMesh → setFields → openInjMoldSim -fillEnd in ``case_dir``,
    sourcing the OF7-org ``bashrc`` and **unsetting** ``FOAM_SIGFPE`` (this build's
    bashrc exports it, turning on the FPE trap that aborts on transient ``exp``
    infinities during the violent fill startup). Logs land in ``case_dir/log.*``."""
    script = (
        f"source '{bashrc}' >/dev/null 2>&1\n"
        "unset FOAM_SIGFPE\n"
        "blockMesh > log.blockMesh 2>&1 || exit 11\n"
        "setFields > log.setFields 2>&1 || exit 12\n"
        f"openInjMoldSim -fillEnd {fill_end} > log.openInjMoldSim 2>&1\n"
    )
    proc = subprocess.run(["bash", "-c", script], cwd=case_dir)
    return proc.returncode


def _cell_grid(case_dir: str, time_dir: str, nx: int, ny: int):
    """The melt fraction field at ``time_dir`` as an (ny, nx) row-major grid
    (x fastest), clamped to [0,1]; or None if absent."""
    af = mf._alpha_file(case_dir, time_dir)
    if af is None:
        return None
    vals = mf._read_internal_scalar_field(af)
    if not vals or len(vals) < nx * ny:
        return None
    import numpy as np
    a = np.clip(np.array(vals[: nx * ny], dtype=float), 0.0, 1.0)
    return a.reshape(ny, nx)


def _masked_temp_grid(case_dir: str, time_dir: str, nx: int, ny: int):
    """Temperature (°C) over the **melt** cells at ``time_dir`` as an (ny, nx) grid,
    with non-melt (air) cells set to NaN so the part reads cleanly against the
    background. Returns None if fields are missing."""
    af = mf._alpha_file(case_dir, time_dir)
    T = mf._read_internal_scalar_field(os.path.join(case_dir, time_dir, "T"))
    a = mf._read_internal_scalar_field(af) if af else None
    if not T or not a or len(T) < nx * ny:
        return None
    import numpy as np
    Tg = np.array(T[: nx * ny], dtype=float) - 273.15
    ag = np.array(a[: nx * ny], dtype=float)
    Tg[ag < 0.5] = np.nan
    return Tg.reshape(ny, nx)


def render_gif(case_dir: str, *, nx: int, ny: int, length_m: float,
               height_m: float, out_path: str, resin: str) -> str | None:
    """Render the melt front (alpha.poly) over all written time directories into an
    animated GIF. Returns the path, or None if matplotlib/PIL are unavailable."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import cm
        from PIL import Image
    except Exception as exc:                       # pragma: no cover
        print(f"[gif] skipped (no matplotlib/PIL): {exc}")
        return None

    times = [t for t in mf._time_dirs(case_dir)]
    frames = []
    Lmm, Hmm = length_m * 1000.0, height_m * 1000.0
    for t in times:
        grid = _cell_grid(case_dir, t, nx, ny)
        if grid is None:
            continue
        fig, ax = plt.subplots(figsize=(7.0, max(1.4, 7.0 * Hmm / Lmm + 0.9)))
        ax.imshow(grid, origin="lower", aspect="auto", cmap=cm.inferno,
                  vmin=0.0, vmax=1.0, extent=[0, Lmm, 0, Hmm])
        filled = float(grid.mean())
        ax.set_title(f"openInjMoldSim — {resin} fill   t = {float(t)*1e3:6.1f} ms"
                     f"   filled {filled*100:5.1f}%", fontsize=11)
        ax.set_xlabel("flow length x  [mm]   (gate at left)")
        ax.set_ylabel("gap y [mm]")
        fig.tight_layout()
        fig.canvas.draw()
        frames.append(Image.frombytes(
            "RGB", fig.canvas.get_width_height(),
            fig.canvas.tostring_rgb()))
        plt.close(fig)

    if not frames:
        print("[gif] no frames (no alpha field found)")
        return None
    # hold the last frame a beat so the full cavity reads clearly
    durations = [180] * (len(frames) - 1) + [1200]
    frames[0].save(out_path, save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True)
    print(f"[gif] wrote {out_path}  ({len(frames)} frames)")
    return out_path


def render_cooling_gif(case_dir: str, *, nx: int, ny: int, length_m: float,
                       height_m: float, out_path: str, resin: str,
                       fill_end_time_s: float, vmin_c: float = 40.0,
                       vmax_c: float = 230.0) -> str | None:
    """Render the part **cooling** (melt temperature, °C) over the PACK time
    directories (t ≥ fill end) into an animated GIF. Air cells are masked out.
    Returns the path, or None if matplotlib/PIL are unavailable."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import cm
        from PIL import Image
    except Exception as exc:                       # pragma: no cover
        print(f"[gif] skipped (no matplotlib/PIL): {exc}")
        return None
    Lmm, Hmm = length_m * 1000.0, height_m * 1000.0
    frames = []
    for t in mf._time_dirs(case_dir):
        try:
            if float(t) < fill_end_time_s:
                continue
        except ValueError:
            continue
        grid = _masked_temp_grid(case_dir, t, nx, ny)
        if grid is None:
            continue
        import numpy as np
        fig, ax = plt.subplots(figsize=(7.0, max(1.4, 7.0 * Hmm / Lmm + 0.9)))
        cmap = cm.inferno.copy()
        cmap.set_bad("0.85")                       # air = light grey
        im = ax.imshow(grid, origin="lower", aspect="auto", cmap=cmap,
                       vmin=vmin_c, vmax=vmax_c, extent=[0, Lmm, 0, Hmm])
        tmax = float(np.nanmax(grid)) if np.isfinite(np.nanmax(grid)) else float("nan")
        ax.set_title(f"openInjMoldSim — {resin} PACK/COOL   "
                     f"t = {float(t)*1e3:6.0f} ms   hottest melt {tmax:5.0f} °C",
                     fontsize=11)
        ax.set_xlabel("flow length x  [mm]   (gate sealed at left)")
        ax.set_ylabel("gap y [mm]")
        fig.colorbar(im, ax=ax, fraction=0.025, pad=0.01, label="T [°C]")
        fig.tight_layout()
        fig.canvas.draw()
        frames.append(Image.frombytes("RGB", fig.canvas.get_width_height(),
                                       fig.canvas.tostring_rgb()))
        plt.close(fig)
    if not frames:
        print("[gif] no pack frames (no T field found)")
        return None
    durations = [220] * (len(frames) - 1) + [1400]
    frames[0].save(out_path, save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True)
    print(f"[gif] wrote {out_path}  ({len(frames)} cooling frames)")
    return out_path


def run_pack(case_dir: str, bashrc: str, binp: str, *, n_phases: int,
             cool_window_s: float, pack_wall_h: float) -> tuple:
    """Run the packing/cooling continuation in a filled ``case_dir`` (the same
    sequence the worker uses): reset the restart step, switch the walls to cooling,
    seal the gate/outlet, then per pack phase extend the time controls and re-run
    openInjMoldSim. Returns (returncode, fill_end_time_s)."""
    fe = mf._latest_time_dir(case_dir)
    fe_t = float(fe)
    plan = mf.pack_phase_plan(fe_t, n_phases=n_phases, cool_window_s=cool_window_s)
    cmds = ([mf.reset_restart_deltaT_cmd(fe), mf.set_walls_h_cmd(fe, pack_wall_h)]
            + mf.close_outlet_cmds(fe))
    for (end_s, wi_s, mdt_s) in plan:
        cmds += mf.time_extend_cmds(end_time_s=end_s, write_interval_s=wi_s,
                                    max_deltaT_s=mdt_s)
        cmds += [[binp]]
    chain = " && ".join(" ".join(a) for a in cmds)
    script = (f"source '{bashrc}' >/dev/null 2>&1\nunset FOAM_SIGFPE\n{chain}")
    proc = subprocess.run(["bash", "-c", script], cwd=case_dir,
                          stdout=open(os.path.join(case_dir, "log.pack"), "w"),
                          stderr=subprocess.STDOUT)
    return proc.returncode, fe_t


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case-dir", default=os.path.join("build", "oims_toy"))
    ap.add_argument("--resin", default="PS")
    ap.add_argument("--length-mm", type=float, default=20.0)
    ap.add_argument("--wall-mm", type=float, default=1.0)
    ap.add_argument("--nx", type=int, default=60)
    ap.add_argument("--ny", type=int, default=8)
    ap.add_argument("--peak-mpa", type=float, default=2.0)
    ap.add_argument("--gif", default=None, help="GIF output path (default: <case>/fill.gif)")
    ap.add_argument("--no-run", action="store_true", help="generate the case only")
    ap.add_argument("--no-gif", action="store_true")
    ap.add_argument("--pack", action="store_true",
                    help="also run the packing/cooling continuation + a cooling GIF")
    ap.add_argument("--cool-window-s", type=float, default=1.0)
    ap.add_argument("--pack-wall-h", type=float, default=1250.0)
    args = ap.parse_args()

    case_dir = os.path.abspath(args.case_dir)
    os.makedirs(case_dir, exist_ok=True)
    L, H = args.length_mm / 1000.0, args.wall_mm / 1000.0

    meta = mf.write_openinjmoldsim_case(
        case_dir, resin=args.resin, length_m=L, height_m=H, depth_m=H,
        nx=args.nx, ny=args.ny, peak_pressure_pa=args.peak_mpa * 1e6)
    print(f"[gen] wrote openInjMoldSim case → {case_dir}")
    print(f"      resin={meta['resin']}  {args.length_mm}mm × {args.wall_mm}mm  "
          f"flow-length ratio {meta['flow_length_ratio']:.0f}  "
          f"peak {args.peak_mpa} MPa")

    if args.no_run:
        return 0

    binp = solvers.openinjmoldsim_bin()
    bashrc = solvers.openinjmoldsim_bashrc()
    if not binp or not bashrc:
        print(f"[run] openInjMoldSim not resolvable (bin={binp}, bashrc={bashrc}); "
              "build it with tools/build_openinjmoldsim.sh --build", file=sys.stderr)
        return 2
    print(f"[run] solver={binp}\n      bashrc={bashrc}")
    rc = run_solver(case_dir, bashrc)
    print(f"[run] solver returncode={rc}")

    log = os.path.join(case_dir, "log.openInjMoldSim")
    filled_line = ""
    if os.path.isfile(log):
        with open(log) as f:
            for line in f:
                if "Filled to" in line or "terminating" in line:
                    filled_line = line.strip()
        if filled_line:
            print(f"[run] {filled_line}")

    parsed = mf.parse_fill(case_dir, nx=args.nx, ny=args.ny, length_m=L)
    if parsed:
        gate = mf.fill_gate(parsed, expected_fill_time_s=meta["expected_fill_time_s"])
        print(f"[gate] pass={gate['pass']}  filled={gate['filled_fraction']:.3f}  "
              f"fidelity={gate['fidelity']}  front_x={parsed.get('front_x_frac')}  "
              f"peak={gate['max_pressure_pa']/1e6:.2f} MPa")
        if gate["warnings"]:
            for w in gate["warnings"]:
                print(f"       ! {w}")
    else:
        print("[gate] no alpha field parsed — solve likely failed")

    if not args.no_gif:
        gif = args.gif or os.path.join(case_dir, "fill.gif")
        render_gif(case_dir, nx=args.nx, ny=args.ny, length_m=L, height_m=H,
                   out_path=gif, resin=args.resin)

    fill_ok = bool(parsed) and parsed.get("filled_fraction", 0) >= 0.9

    # --- packing / cooling continuation (issue #113) -------------------------
    if args.pack and fill_ok:
        print(f"[pack] running cooling continuation (window {args.cool_window_s}s, "
              f"walls h={args.pack_wall_h})…")
        fstats = mf._melt_stats(case_dir, mf._latest_time_dir(case_dir))
        prc, fe_t = run_pack(case_dir, bashrc, binp, n_phases=2,
                             cool_window_s=args.cool_window_s,
                             pack_wall_h=args.pack_wall_h)
        print(f"[pack] returncode={prc}  fill_end={fe_t:.4f}s")
        tait = mf._resin_cross_wlf_tait(args.resin)[1]
        ppar = mf.parse_pack(case_dir, fill_rho_mean=fstats["rho_mean"],
                             fill_end_time_s=fe_t)
        if ppar:
            pg = mf.pack_gate(ppar, tait=tait, fill_T_mean_k=fstats["T_mean"])
            print(f"[pack-gate] pass={pg['pass']}  score={pg['score']}  "
                  f"shrinkage={pg['volumetric_shrinkage_pct']}%  "
                  f"(Tait expects {pg['expected_densification_pct']}%, "
                  f"faithful={pg['pvt_faithful']})  sink_risk={pg['sink_risk']}  "
                  f"rho {ppar['rho_min']:.0f}…{ppar['rho_mean_final']:.0f} kg/m³")
            for w in pg["warnings"]:
                print(f"           ! {w}")
        if not args.no_gif:
            cgif = os.path.join(case_dir, "pack_cool.gif")
            render_cooling_gif(case_dir, nx=args.nx, ny=args.ny, length_m=L,
                               height_m=H, out_path=cgif, resin=args.resin,
                               fill_end_time_s=fe_t)

    ok = fill_ok and (not args.pack or os.path.isfile(
        os.path.join(case_dir, "log.pack")))
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
