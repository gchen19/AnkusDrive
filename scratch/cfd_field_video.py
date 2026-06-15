"""CFD field animation (simulation-video-capture kickoff item D — the deferred spike,
docs/KICKOFF_simulation_video_capture.md). Turn a transient OpenFOAM solve, whose fields
live only as native results, into a GIF a reviewer can watch: the velocity field of a
lid-driven cavity developing from rest into its steady primary vortex, beside the
centre-line profile converging onto the Ghia (1982) benchmark.

The kickoff flagged this as the highest-effort item because "OpenFOAM writes full transient
U/p fields, but only as native binaries; rendering them needs field parsing + mesh
reconstruction (or a VTK/ParaView dependency)." The tractable path, no heavy dep: run the
solver, let OpenFOAM's own `foamToVTK -legacy -ascii` write each time step as a legacy
.vtk (meshio reads it without lxml/vtk), and contour the velocity per step.

Why the lid-driven cavity: it is the canonical incompressible-CFD verification case, it is
genuinely TRANSIENT (icoFoam, real time — the vortex forms before your eyes, not a steady
solve's convergence), the mesh is tiny, and it has a *published benchmark* — so this video
backs its claim the §11.10 way: the simulated vertical-centre-line u-velocity is overlaid
on Ghia, Ghia & Shin (1982) Re=100, and you watch it land on the dots.

Outputs (artifacts/): cfd_cavity_field.gif + cfd_cavity_field_filmstrip.png.

  .venv/bin/python3 scratch/cfd_field_video.py
"""
import glob
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scratch"))

import io                                                   # noqa: E402
import numpy as np                                          # noqa: E402
import matplotlib                                           # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                             # noqa: E402
import matplotlib.tri as mtri                               # noqa: E402
from matplotlib import colormaps                            # noqa: E402
from matplotlib.colors import Normalize                     # noqa: E402
from matplotlib.cm import ScalarMappable                    # noqa: E402
from PIL import Image                                       # noqa: E402
import meshio                                               # noqa: E402

from driftpin import solvers                                # noqa: E402
import sim_video as sv                                      # noqa: E402

ART = REPO / "artifacts"
N = 64                       # cells per side (64×64 — smooth field, tight Ghia match)
RE = 100.0                   # Reynolds number U·L/nu
U_LID, L = 1.0, 1.0          # lid speed, cavity side
NU = U_LID * L / RE          # kinematic viscosity for the target Re
DT, END_T = 0.004, 25.0      # timestep (Courant<1) and run length (≈steady by here)
WRITE_EVERY = 210            # write interval (steps) -> ~30 frames

# Ghia, Ghia & Shin (1982), Re=100: u-velocity along the VERTICAL centre-line (x=0.5),
# the standard verification data for the lid-driven cavity. (y, u/U_lid).
GHIA_Y = [0.0, 0.0547, 0.0625, 0.0703, 0.1016, 0.1719, 0.2813, 0.4531, 0.5,
          0.6172, 0.7344, 0.8516, 0.9531, 0.9609, 0.9688, 0.9766, 1.0]
GHIA_U = [0.0, -0.03717, -0.04192, -0.04775, -0.06434, -0.10150, -0.15662, -0.21090,
          -0.20581, -0.13641, 0.00332, 0.23151, 0.68717, 0.73722, 0.78871, 0.84123, 1.0]


def _header(cls, obj):
    return ("FoamFile\n{\n    version 2.0; format ascii; class %s; object %s;\n}\n"
            % (cls, obj))


def _write_case(d):
    """Write the lid-driven cavity case (blockMesh + icoFoam) into directory ``d``."""
    (d / "system").mkdir(); (d / "constant").mkdir(); (d / "0").mkdir()
    (d / "system/blockMeshDict").write_text(_header("dictionary", "blockMeshDict") + f"""
scale 1;
vertices ( (0 0 0)({L} 0 0)({L} {L} 0)(0 {L} 0)
           (0 0 0.1)({L} 0 0.1)({L} {L} 0.1)(0 {L} 0.1) );
blocks ( hex (0 1 2 3 4 5 6 7) ({N} {N} 1) simpleGrading (1 1 1) );
edges ();
boundary (
  movingWall {{ type wall; faces ( (3 7 6 2) ); }}
  fixedWalls {{ type wall; faces ( (0 4 7 3) (2 6 5 1) (1 5 4 0) ); }}
  frontAndBack {{ type empty; faces ( (0 3 2 1) (4 5 6 7) ); }}
);
mergePatchPairs ();
""")
    (d / "constant/transportProperties").write_text(
        _header("dictionary", "transportProperties") + f"\nnu {NU:.10g};\n")
    (d / "0/U").write_text(_header("volVectorField", "U") + f"""
dimensions [0 1 -1 0 0 0 0];
internalField uniform (0 0 0);
boundaryField {{
  movingWall {{ type fixedValue; value uniform ({U_LID} 0 0); }}
  fixedWalls {{ type noSlip; }}
  frontAndBack {{ type empty; }}
}}
""")
    (d / "0/p").write_text(_header("volScalarField", "p") + """
dimensions [0 2 -2 0 0 0 0];
internalField uniform 0;
boundaryField {
  movingWall { type zeroGradient; }
  fixedWalls { type zeroGradient; }
  frontAndBack { type empty; }
}
""")
    (d / "system/controlDict").write_text(_header("dictionary", "controlDict") + f"""
application icoFoam; startFrom startTime; startTime 0; stopAt endTime;
endTime {END_T}; deltaT {DT}; writeControl timeStep; writeInterval {WRITE_EVERY};
purgeWrite 0; writeFormat ascii; writePrecision 8; timeFormat general; runTimeModifiable false;
""")
    (d / "system/fvSchemes").write_text(_header("dictionary", "fvSchemes") + """
ddtSchemes { default Euler; }
gradSchemes { default Gauss linear; }
divSchemes { default none; div(phi,U) Gauss linear; }
laplacianSchemes { default Gauss linear orthogonal; }
interpolationSchemes { default linear; }
snGradSchemes { default orthogonal; }
""")
    (d / "system/fvSolution").write_text(_header("dictionary", "fvSolution") + """
solvers {
  p { solver PCG; preconditioner DIC; tolerance 1e-6; relTol 0.05; }
  pFinal { solver PCG; preconditioner DIC; tolerance 1e-6; relTol 0; }
  U { solver PBiCGStab; preconditioner DILU; tolerance 1e-7; relTol 0; }
}
PISO { nCorrectors 2; nNonOrthogonalCorrectors 0; pRefCell 0; pRefValue 0; }
""")


def _foam(d, bashrc, *cmds):
    """Run OpenFOAM apps in ``d`` with the environment sourced (else they abort on the
    missing etc/controlDict). Returns (rc, tail)."""
    script = (f"source '{bashrc}' >/dev/null 2>&1\n" if bashrc else "") + " && ".join(cmds)
    r = subprocess.run(["bash", "-c", script], cwd=str(d), capture_output=True, text=True)
    return r.returncode, ((r.stdout or "") + (r.stderr or ""))[-1500:]


def _load_steps(d):
    """Read every foamToVTK legacy .vtk into (time, xy points, U). The cavity mesh is
    static, so the planar (z≈0) node set is shared; only U changes per step."""
    files = sorted(glob.glob(str(d / "VTK" / "*.vtk")),
                   key=lambda p: int(Path(p).stem.split("_")[-1]))
    steps = []
    for f in files:
        m = meshio.read(f)
        step = int(Path(f).stem.split("_")[-1])
        z = m.points[:, 2]
        plane = z <= 0.5 * (z.min() + z.max())             # one z layer (2-D field)
        xy = m.points[plane, :2]
        U = np.array(m.point_data["U"])[plane]
        steps.append((step * DT, xy, U))
    return steps


def _frame(tri, xy, U, cmap, norm, *, t, cl_y, cl_u, ghia_err):
    """One 2-panel frame: (left) |U| contour + velocity vectors; (right) the centre-line
    u(y) developing against the Ghia (1982) Re=100 benchmark dots."""
    speed = np.linalg.norm(U[:, :2], axis=1)
    fig = plt.figure(figsize=(10.6, 4.7), dpi=110)
    ax = fig.add_subplot(1, 2, 1)
    ax.tricontourf(tri, speed, levels=24, cmap=cmap, norm=norm)
    s = slice(None, None, max(1, len(xy) // 360))           # subsample vectors
    ax.quiver(xy[s, 0], xy[s, 1], U[s, 0], U[s, 1], color="white", alpha=0.7,
              scale=18, width=0.0035)
    ax.set_aspect("equal"); ax.set_xlim(0, L); ax.set_ylim(0, L)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title("velocity field — lid driven →  (top wall)", fontsize=11)
    cb = fig.colorbar(ScalarMappable(norm=norm, cmap=cmap), ax=ax, shrink=0.82,
                      pad=0.02, fraction=0.046)
    cb.set_label("|U|  (m/s)", fontsize=9)

    a2 = fig.add_subplot(1, 2, 2)
    a2.plot(GHIA_U, GHIA_Y, "o", ms=5, mfc="none", color="#111",
            label="Ghia 1982 (Re=100)")
    a2.plot(cl_u, cl_y, "-", lw=2, color="#c0392b", label="icoFoam (this run)")
    a2.axvline(0, color="#999", lw=0.8)
    a2.set_xlabel("u / U_lid  (vertical centre-line)"); a2.set_ylabel("y / L")
    a2.set_ylim(0, 1); a2.set_xlim(-0.35, 1.05)
    a2.set_title(f"t = {t:4.1f} s   Re = {RE:.0f}", fontsize=11)
    a2.legend(fontsize=8, loc="upper left"); a2.grid(alpha=0.3)
    fig.text(0.5, 0.015,
             f"primary vortex forming — centre-line lands on Ghia to rms "
             f"{ghia_err * 100:.1f}% of U_lid (the published benchmark, not a model of it)",
             ha="center", fontsize=9.5, color="#333")
    fig.suptitle("CFD field animation — lid-driven cavity (OpenFOAM icoFoam, transient)",
                 fontsize=12.5)
    fig.tight_layout(rect=(0, 0.04, 1, 0.95))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def _centerline(xy, U):
    """u(y) along the vertical centre-line x=L/2, sorted by y."""
    col = np.isclose(xy[:, 0], L / 2.0, atol=L / N)
    y, u = xy[col, 1], U[col, 0]
    o = np.argsort(y)
    return y[o], u[o]


def main():
    ART.mkdir(parents=True, exist_ok=True)
    bashrc = solvers.openfoam_bashrc()
    if not bashrc:
        print("OpenFOAM environment not found — cannot run the CFD case."); return 1
    with tempfile.TemporaryDirectory(prefix="cfd_cavity_") as td:
        d = Path(td)
        _write_case(d)
        print("== CFD field animation — lid-driven cavity (icoFoam, Re=100) ==")
        t0 = time.time()
        rc, tail = _foam(d, bashrc, "blockMesh", "icoFoam", "foamToVTK -legacy -ascii")
        if rc != 0:
            print(f"  OpenFOAM run failed (rc {rc}):\n{tail}"); return 1
        print(f"  solve + foamToVTK: {time.time() - t0:.0f}s")
        steps = _load_steps(d)

    print(f"  {len(steps)} time steps; mesh {N}×{N}")
    tri = mtri.Triangulation(steps[-1][1][:, 0], steps[-1][1][:, 1])
    vmax = max(np.linalg.norm(U[:, :2], axis=1).max() for _, _, U in steps)
    cmap, norm = colormaps["viridis"], Normalize(vmin=0.0, vmax=vmax)

    # final-step centre-line vs Ghia (the convergence target). Report max AND rms — the
    # max is one steep point in the lid boundary layer a coarse grid under-resolves; the
    # rms over the profile is the representative match.
    yf, uf = _centerline(steps[-1][1], steps[-1][2])
    ghia_at = np.interp(yf, GHIA_Y, GHIA_U)
    resid = (uf - ghia_at) / U_LID
    final_err = float(np.abs(resid).max())
    rms_err = float(np.sqrt((resid ** 2).mean()))

    images = []
    for t, xy, U in steps:
        cy, cu = _centerline(xy, U)
        images.append(_frame(tri, xy, U, cmap, norm, t=t, cl_y=cy, cl_u=cu,
                             ghia_err=rms_err))

    gif = sv.encode_gif(images, ART / "cfd_cavity_field", fps=8, hold_last=8)
    nframes = len(images)
    strip = sv.filmstrip(images, ART / "cfd_cavity_field_filmstrip.png",
                         picks=[0, nframes // 4, nframes // 2, 3 * nframes // 4, nframes - 1])
    ok = rms_err < 0.04
    print(f"  final centre-line vs Ghia 1982 (Re=100): rms Δ = {rms_err * 100:.1f}% "
          f"(max {final_err * 100:.1f}% at the lid layer) of U_lid -> "
          f"{'MATCHES benchmark' if ok else 'CHECK'}")
    print(f"  GIF       -> {gif}  ({gif.stat().st_size:,} bytes)")
    print(f"  filmstrip -> {strip}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
