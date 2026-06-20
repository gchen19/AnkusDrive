#!/usr/bin/env python3
"""Arm's-length runner for the Bempp boundary-element acoustics engine.

THIS STANDALONE SCRIPT IS THE ONLY PLACE ``bempp_cl`` (and its gmsh/meshio>=5
stack) IS IMPORTED. The isolation here is NOT a license boundary — Bempp is MIT,
the same permissive footing as DriftPin. It is a DEPENDENCY-CLASH boundary:
Bempp needs ``meshio>=4`` (``cells_dict``), but DriftPin's shared venv pins
``meshio==3.0`` for ``solidspy`` (``driftpin/analysis/topology.py``). Upgrading
meshio in the shared venv would break topology optimisation, so Bempp lives in a
DEDICATED venv (``.venv-bempp``) and is invoked OUT-OF-PROCESS exactly like the
GPL optics/EM runners — ``subprocess.run([bempp_python, bempp_runner.__file__])``
exchanging sentinel-delimited JSON over stdin/stdout.

Bempp's shape generators shell out to a ``gmsh`` CLI binary; that binary ships in
the dedicated venv's ``bin/`` (the pip ``gmsh`` wheel), so this runner prepends
its own interpreter's ``bin/`` to ``PATH`` before importing the shapes module.

Protocol
--------
stdin  : one JSON object  {"problem": "...", ...}
stdout : the machine-readable result is emitted on its OWN line wrapped in
         sentinels:  @@JSON@@{...}@@END@@
         (Bempp / gmsh may print to stdout; the caller splits on the sentinels —
         see worker._run_bempp.)

Problems
--------
"radiation": exterior Helmholtz radiation from a pulsating (monopole) sphere of
    radius ``a_m`` with uniform surface normal velocity ``u_amp`` at ``freq_hz``.
    Solve the Neumann problem with the Burton–Miller combined formulation, recover
    the surface pressure, then evaluate the far-field pressure at ``r_m`` and
    integrate the radiated sound power W = ∮ ½ Re(p · v*) dS over the sphere.
    Args: a_m, freq_hz, u_amp, r_m, h (mesh size, fraction of a), rho, c.
    Returns {ka, radiated_power_w, farfield_pressure_abs, farfield_pressure_x_r,
        surface_pressure_abs_mean, n_elements, wall_s}. The BEM W / far-field vs
        the monopole_sphere oracle (ratio≈1) is the gate.

"scattering": exterior plane-wave scattering off a RIGID (sound-hard) sphere of
    radius ``a_m`` over a list of wavenumbers ``ka_list``. Burton–Miller Neumann
    solve (∂p_total/∂n = 0 ⇒ ∂p_sc/∂n = −∂p_inc/∂n), evaluate the scattered field
    in the far zone at the requested ``theta_deg`` angles and report the form
    function f∞ = 2r·p_sc/(a·e^{ikr}). Args: a_m, ka_list, theta_deg (list),
    h_per_wl (elements per wavelength). Returns per-ka {ka, form_function_abs{},
    backscatter_abs, n_elements}. The BEM |f∞(θ)| vs the rigid_sphere_scattering
    Mie oracle (ratio≈1) is the gate.

"ping": {"problem":"ping"} -> {"ok":true, "engine":"bempp-cl", "version": ...}

"mesh_solve": radiation problem on an EXTERNALLY SUPPLIED triangular surface mesh
    (vertices[3][nv], elements[3][ne]) — the path the worker uses to feed a real
    FreeCAD-exported surface. Same outputs as "radiation" (no closed-form r-field
    unless the geometry is a sphere of radius a_m, used only for the gate).
"""
import json
import os
import sys
import time

SENTINEL_HEAD = "@@JSON@@"
SENTINEL_TAIL = "@@END@@"
C_AIR = 343.0
RHO_AIR = 1.204


def _ensure_gmsh_on_path():
    """Bempp's shapes module invokes a ``gmsh`` CLI binary; the pip ``gmsh`` wheel
    drops it in this interpreter's bin/ dir. Make sure that dir is on PATH so
    sphere()/__generate_grid_from_geo_string can find it."""
    bindir = os.path.dirname(sys.executable)
    path = os.environ.get("PATH", "")
    if bindir not in path.split(os.pathsep):
        os.environ["PATH"] = bindir + os.pathsep + path


def _sphere_grid(a_m, h):
    import bempp_cl.api as bem
    g = bem.shapes.sphere(r=a_m, h=h)
    return g


# --- shared exterior-Helmholtz Neumann solve ----------------------------------
# Direct boundary-integral formulation for the EXTERIOR Neumann (sound-hard /
# prescribed-velocity) problem, solved for the surface pressure p:
#
#       (−½I + D) p = S · (∂p/∂n)          (n outward, fluid exterior)
#
# and the matching Helmholtz exterior representation, for any field point x:
#
#       p(x) = D_pot(p)(x) − S_pot(∂p/∂n)(x).
#
# Both the boundary equation and the representation were calibrated against an
# exact point-monopole trace (surface pressure AND far field reproduced to machine
# precision) before wiring; see the module test. The plain (non-Burton–Miller)
# form is exact away from the sphere's interior-Dirichlet eigenfrequencies
# (jₙ(ka)=0); the canonical ka used here sit clear of them.


def _solve_neumann(space, k, dn_fun):
    """Solve (−½I + D) p = S·dn for the exterior surface pressure p. Returns the
    GridFunction p and the gmres iteration count."""
    import bempp_cl.api as bem
    from bempp_cl.api.operators.boundary import helmholtz, sparse
    identity = sparse.identity(space, space, space)
    dlp = helmholtz.double_layer(space, space, space, k)
    slp = helmholtz.single_layer(space, space, space, k)
    lhs = -0.5 * identity + dlp
    rhs = slp * dn_fun
    sol = bem.linalg.gmres(lhs, rhs, tol=1e-5)
    p_surf = sol[0]
    info = sol[1] if len(sol) > 1 else None
    return p_surf, info


def _field_at(space, k, p_surf, dn_fun, points):
    """Exterior representation p(x) = D_pot(p) − S_pot(dn) at the given 3×N points."""
    import numpy as np
    from bempp_cl.api.operators.potential import helmholtz as helmholtz_pot
    slp_pot = helmholtz_pot.single_layer(space, points, k)
    dlp_pot = helmholtz_pot.double_layer(space, points, k)
    return np.asarray((dlp_pot * p_surf - slp_pot * dn_fun)).ravel()


def _radiated_power(space, p_surf, u_amp):
    """Radiated sound power W = ½ Re ∮ p · vₙ* dS for a uniform real normal
    velocity vₙ = U: W = ½U·Re(∮ p dS). The global sign of p follows the engine's
    e^{−iωt} convention, so flip to the radiating (e^{+iωt}) convention."""
    import numpy as np
    import bempp_cl.api as bem
    from bempp_cl.api.operators.boundary import sparse
    mass = sparse.identity(space, space, space).weak_form()
    ones = bem.GridFunction(space, coefficients=np.ones(space.global_dof_count))
    integral_p = np.vdot(ones.coefficients, mass @ p_surf.coefficients)  # ∮ p dS
    return -0.5 * float(u_amp) * float(np.real(integral_p))


def _radiation_on_grid(problem, grid):
    """Exterior radiation from a uniformly pulsating surface (the monopole twin):
    prescribe ∂p/∂n = −iωρ·U (outward velocity U), solve for the surface pressure,
    recover the radiated power and the far-field pressure at r_m along +z."""
    import numpy as np
    import bempp_cl.api as bem

    a = float(problem["a_m"])
    freq = float(problem["freq_hz"])
    u_amp = float(problem.get("u_amp", 1.0))
    r_m = float(problem.get("r_m", 4.0 * a))
    rho = float(problem.get("rho", RHO_AIR))
    c = float(problem.get("c", C_AIR))
    k = 2.0 * np.pi * freq / c
    omega = c * k

    space = bem.function_space(grid, "P", 1)
    n_el = grid.number_of_elements
    dn_value = -1j * omega * rho * u_amp                # ∂p/∂n = −iωρ·vₙ, vₙ = U
    dn_fun = bem.GridFunction(
        space, coefficients=np.full(space.global_dof_count, dn_value))

    p_surf, info = _solve_neumann(space, k, dn_fun)
    W = _radiated_power(space, p_surf, u_amp)
    point = np.array([[0.0], [0.0], [r_m]])
    p_far = complex(_field_at(space, k, p_surf, dn_fun, point)[0])

    return {
        "a_m": a, "freq_hz": freq, "ka": round(float(k * a), 6),
        "rho": rho, "c": c, "u_amp": u_amp, "r_m": r_m,
        "radiated_power_w": float(W),
        "farfield_pressure_abs": float(abs(p_far)),
        "farfield_pressure_x_r": float(abs(p_far) * r_m),
        "surface_pressure_abs_mean": float(np.mean(np.abs(p_surf.coefficients))),
        "n_elements": int(n_el),
        "gmres_iterations": (int(info) if isinstance(info, int) else None),
    }


def _radiation(problem):
    a = float(problem["a_m"])
    h = float(problem.get("h", 0.25)) * a
    grid = _sphere_grid(a, h)
    out = _radiation_on_grid(problem, grid)
    out["problem"] = "radiation"
    return out


def _scattering(problem):
    import numpy as np
    import bempp_cl.api as bem

    a = float(problem["a_m"])
    ka_list = [float(x) for x in problem["ka_list"]]
    theta_deg = [float(t) for t in problem.get("theta_deg", [180.0])]
    h_per_wl = float(problem.get("h_per_wl", 10.0))

    results = []
    for ka in ka_list:
        k = ka / a
        wl = 2.0 * np.pi / k
        # resolve BOTH the wavelength (acoustics) AND the sphere geometry: at small ka
        # the wavelength is huge, so the a/3 floor keeps the curvature well-meshed.
        h = min(wl / h_per_wl, a * 0.3)
        grid = _sphere_grid(a, h)
        space = bem.function_space(grid, "P", 1)
        n_el = grid.number_of_elements

        # incident plane wave along +z, p_inc = e^{ikz}; rigid (sound-hard) BC
        #   ∂p_total/∂n = 0  ⇒  ∂p_sc/∂n = −∂p_inc/∂n = −ik·n_z·e^{ikz}
        kk = k                                         # closure capture (numba callable)

        @bem.complex_callable
        def dn_inc(x, n, domain_index, res):
            res[0] = -1j * kk * n[2] * np.exp(1j * kk * x[2])

        dn_fun = bem.GridFunction(space, fun=dn_inc)
        p_sc, _ = _solve_neumann(space, k, dn_fun)

        # scattered far field at the requested angles, far range R = 50a
        R = 50.0 * a
        ff = {}
        for t in theta_deg:
            th = np.radians(t)
            pt = np.array([[R * np.sin(th)], [0.0], [R * np.cos(th)]])
            p_far = complex(_field_at(space, k, p_sc, dn_fun, pt)[0])
            # far-field form function f∞ = (2r/a)·p_sc·e^{−ikr}
            f_inf = 2.0 * R / a * p_far * np.exp(-1j * k * R)
            ff[f"{t:g}"] = float(abs(f_inf))
        results.append({
            "ka": round(ka, 6),
            "form_function_abs": ff,
            "backscatter_abs": ff.get("180"),
            "n_elements": int(n_el),
        })
    return {"problem": "scattering", "a_m": a, "h_per_wl": h_per_wl,
            "results": results}


def _mesh_solve(problem):
    """Radiation on an EXTERNALLY supplied triangular surface mesh (the FreeCAD
    export path). vertices: 3×nv, elements: 3×ne (0-based). a_m is only the
    sphere-radius the oracle gate compares against — the solve itself is on the
    supplied geometry."""
    import numpy as np
    import bempp_cl.api as bem

    verts = np.asarray(problem["vertices"], dtype=float)
    elems = np.asarray(problem["elements"], dtype=np.uint32)
    grid = bem.grid.grid.Grid(verts, elems)
    out = _radiation_on_grid(problem, grid)
    out["problem"] = "mesh_solve"
    return out


def _dispatch(problem):
    kind = problem.get("problem")
    if kind == "ping":
        import bempp_cl
        return {"ok": True, "engine": "bempp-cl",
                "version": getattr(bempp_cl, "__version__", "unknown")}
    _ensure_gmsh_on_path()
    if kind == "radiation":
        return _radiation(problem)
    if kind == "scattering":
        return _scattering(problem)
    if kind == "mesh_solve":
        return _mesh_solve(problem)
    return {"ok": False, "error": f"unknown problem {kind!r}"}


def main():
    raw = sys.stdin.read()
    t0 = time.time()
    try:
        problem = json.loads(raw) if raw.strip() else {}
        result = _dispatch(problem)
        result.setdefault("ok", "error" not in result)
        result["wall_s"] = round(time.time() - t0, 3)
    except Exception as e:                            # never crash the parent — clean miss
        import traceback
        result = {"ok": False, "error": f"{type(e).__name__}: {e}",
                  "trace": traceback.format_exc()[-1200:]}
    sys.stdout.write("\n" + SENTINEL_HEAD + json.dumps(result) + SENTINEL_TAIL + "\n")


if __name__ == "__main__":
    main()
