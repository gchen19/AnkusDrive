"""Planar mechanism kinematics — the closed-form, solver-free core of family 8.

Pure-Python, FreeCAD-free. These are the *exact* anchors the multibody-dynamics
family (``docs/SIMULATION_TOOLS.md`` §8) is gated against — the closed-form truths
that a PyBullet/MuJoCo dynamics run must reproduce (``driftpin/analysis/mbd.py``):

  * **Grübler/Kutzbach mobility** — DOF = 3·(n−1) − 2·j₁ − j₂ for a planar linkage;
    a four-bar is exactly 1.
  * **Grashof classification** — which links can fully rotate, from the four link
    lengths alone (the same relation ``tests/.../fourbar_crankrocker`` already gates).
  * **Slider-crank stroke** — piston travel is exactly 2·R, independent of the
    conrod length L (>R) — a hard-edged check a mis-scaled solver fails.
  * **Four-bar position analysis** — the vector-loop closed form (circle-circle
    intersection) for the coupler trajectory and reachable envelope, and whether
    the input crank can be driven a full revolution (vs hitting a limit).

Physical collision *through* the motion (the time-varying interference a static
``interference_check`` misses) is NOT done here — 2D link-segment crossing is a
poor proxy (a coupler routinely sweeps over the fixed ground frame). That check
belongs to the dynamics engine in ``driftpin/analysis/mbd.py``, which sees real
3D geometry contacts at each timestep.

Units: lengths mm, angles degrees at the API boundary (radians internally). See
``docs/SIMULATION_EXAMPLES.md`` §8 for the worked toy (four-bar DOF=1; stroke=2R).
"""
from __future__ import annotations

import math

# Planar lower pairs (1-DOF joints): each removes 2 of the 3 planar DOFs.
_LOWER_PAIRS = {"revolute", "pin", "prismatic", "slider", "pin-in-slot-1"}
# Planar higher pairs (2-DOF: cam/gear contact): each removes 1.
_HIGHER_PAIRS = {"cam", "gear", "rolling", "higher"}


def gruebler_dof(n_links: int, joints, planar: bool = True) -> int:
    """Degrees of freedom of a linkage by the Grübler/Kutzbach criterion.

    ``n_links`` counts every link INCLUDING ground. ``joints`` is a list of joint
    dicts (``{"type": "revolute", ...}``) or bare type strings. Planar:
    DOF = 3·(n−1) − 2·j₁ − j₂ (j₁ lower pairs, j₂ higher pairs); spatial uses 6 and
    counts each joint's removed DOFs. A four-bar (n=4, four revolutes) -> 1.

    Unknown joint types are treated as lower pairs (the common case) — pass explicit
    higher pairs to override. Returns the integer DOF (may be ≤0 for a structure)."""
    j1 = j2 = 0
    for j in joints:
        jtype = (j.get("type") if isinstance(j, dict) else j) or "revolute"
        if jtype in _HIGHER_PAIRS:
            j2 += 1
        else:
            j1 += 1                                   # lower pair (default)
    if planar:
        return 3 * (n_links - 1) - 2 * j1 - j2
    # spatial: each lower pair removes 5, higher removes 4 (cam), full = 6·(n−1)
    return 6 * (n_links - 1) - 5 * j1 - 4 * j2


def grashof_classify(crank: float, coupler: float, rocker: float, ground: float) -> dict:
    """Classify a four-bar from its link lengths (in linkage order: input crank,
    coupler, output rocker, fixed ground).

    Grashof's theorem on S(shortest)+L(longest) vs P+Q (the other two):
      S+L < P+Q  -> Grashof (some link fully rotates); the type depends on which
                    link is shortest: ground -> double-crank (drag-link); a side
                    link (crank/rocker) -> crank-rocker; coupler -> double-rocker.
      S+L = P+Q  -> change-point (the linkage can fold / become collinear).
      S+L > P+Q  -> non-Grashof (triple-rocker; no link makes a full revolution).

    Returns {lengths, shortest, condition ('grashof'|'change_point'|'non_grashof'),
    type, input_crank_fully_rotates}. ``input_crank_fully_rotates`` is the practical
    gate: the input crank completes full revolutions iff Grashof and the shortest
    link is the crank or the ground. Raises ValueError on a non-positive length or a
    set that can't close (longest ≥ sum of the other three)."""
    names = ("crank", "coupler", "rocker", "ground")
    lengths = {"crank": float(crank), "coupler": float(coupler),
               "rocker": float(rocker), "ground": float(ground)}
    vals = list(lengths.values())
    if any(v <= 0 for v in vals):
        raise ValueError(f"all link lengths must be > 0: {lengths}")
    longest = max(vals)
    if longest >= sum(vals) - longest:               # quadrilateral inequality
        raise ValueError(
            f"links cannot form a closed four-bar (longest {longest} >= sum of "
            f"the other three {sum(vals) - longest}): {lengths}"
        )

    s = min(vals)
    l = max(vals)
    p_plus_q = sum(vals) - s - l
    if abs((s + l) - p_plus_q) < 1e-9:
        condition, base_type = "change_point", "change-point"
    elif s + l < p_plus_q:
        condition = "grashof"
        shortest_name = min(names, key=lambda n: lengths[n])
        base_type = {
            "ground": "double-crank",         # drag-link
            "coupler": "double-rocker",
        }.get(shortest_name, "crank-rocker")  # crank/rocker shortest -> crank-rocker
    else:
        condition, base_type = "non_grashof", "triple-rocker"

    shortest_name = min(names, key=lambda n: lengths[n])
    input_rotates = condition == "grashof" and shortest_name in ("crank", "ground")
    return {
        "lengths": lengths,
        "shortest": shortest_name,
        "condition": condition,
        "type": base_type,
        "input_crank_fully_rotates": input_rotates,
    }


def slider_crank(crank_mm: float, conrod_mm: float, n_steps: int = 360,
                 wrist_offset_mm: float = 0.0) -> dict:
    """In-line (or offset) slider-crank piston kinematics over a full crank turn.

    Piston position along the slide for crank angle θ (offset e):
    x(θ) = R·cosθ + sqrt(L² − (R·sinθ − e)²). For the in-line case (e=0) the
    peak-to-peak travel is exactly **2·R**, independent of the conrod length L — the
    family's hard-edged anchor. Requires L > R + |e| (else the mechanism locks).

    Returns {crank_mm, conrod_mm, wrist_offset_mm, stroke_mm, x_tdc_mm, x_bdc_mm,
    inline_stroke_exact (stroke == 2R within 1e-6 when e=0)}. Raises ValueError when
    the conrod is too short to assemble."""
    R = float(crank_mm)
    L = float(conrod_mm)
    e = float(wrist_offset_mm)
    if R <= 0 or L <= 0:
        raise ValueError("crank and conrod lengths must be > 0")
    if L <= R + abs(e):
        raise ValueError(
            f"conrod {L} too short to assemble (needs > R+|e| = {R + abs(e)})"
        )
    n = max(int(n_steps), 4)
    xs = []
    for k in range(n):
        th = 2.0 * math.pi * k / n
        x = R * math.cos(th) + math.sqrt(L * L - (R * math.sin(th) - e) ** 2)
        xs.append(x)
    x_max, x_min = max(xs), min(xs)
    stroke = x_max - x_min
    return {
        "crank_mm": R,
        "conrod_mm": L,
        "wrist_offset_mm": e,
        "stroke_mm": round(stroke, 6),
        "x_tdc_mm": round(R + math.sqrt(L * L - e * e), 6),
        "x_bdc_mm": round(-R + math.sqrt(L * L - e * e), 6),
        "inline_stroke_exact": e == 0.0 and abs(stroke - 2.0 * R) < 1e-6,
    }


# --- four-bar position analysis (vector-loop closed form) ----------------------

def _circle_intersect(c0, r0, c1, r1, branch: int):
    """One intersection of circle(c0,r0) and circle(c1,r1); branch ∈ {+1,−1} picks
    the open/crossed assembly. Returns (x, y) or None if the circles don't meet."""
    (x0, y0), (x1, y1) = c0, c1
    dx, dy = x1 - x0, y1 - y0
    d = math.hypot(dx, dy)
    if d < 1e-12 or d > r0 + r1 + 1e-9 or d < abs(r0 - r1) - 1e-9:
        return None                                  # coincident centers or no overlap
    a = (d * d + r0 * r0 - r1 * r1) / (2.0 * d)
    h2 = r0 * r0 - a * a
    h = math.sqrt(h2) if h2 > 0 else 0.0
    xm, ym = x0 + a * dx / d, y0 + a * dy / d
    return (xm + branch * h * (dy / d), ym - branch * h * (dx / d))


def fourbar_position(ground: float, crank: float, coupler: float, rocker: float,
                     theta2_deg: float, config: str = "open") -> dict | None:
    """Solve a four-bar for one input crank angle by the vector loop.

    Frame: ground pivot O₂ at the origin, O₄ at (ground, 0). The crank tip A is
    O₂ + crank·(cosθ₂, sinθ₂); the coupler-rocker pin B is the circle-circle
    intersection of circle(A, coupler) ∩ circle(O₄, rocker). ``config`` 'open' or
    'crossed' selects the assembly branch.

    Returns {theta2_deg, A, B, theta3_deg (coupler), theta4_deg (rocker)} or **None**
    when the linkage cannot close at this angle (the branch where a non-Grashof
    linkage hits a limit) — callers sweeping angles treat None as 'unreachable'."""
    O2 = (0.0, 0.0)
    O4 = (float(ground), 0.0)
    th2 = math.radians(theta2_deg)
    A = (crank * math.cos(th2), crank * math.sin(th2))
    # 'open' = the non-crossed assembly (e.g. the parallelogram branch of a
    # parallelogram linkage, where the rocker tracks the crank).
    branch = -1 if config == "open" else 1
    B = _circle_intersect(A, float(coupler), O4, float(rocker), branch)
    if B is None:
        return None
    theta3 = math.degrees(math.atan2(B[1] - A[1], B[0] - A[0]))
    theta4 = math.degrees(math.atan2(B[1] - O4[1], B[0] - O4[0]))
    return {
        "theta2_deg": theta2_deg,
        "A": [round(A[0], 6), round(A[1], 6)],
        "B": [round(B[0], 6), round(B[1], 6)],
        "theta3_deg": round(theta3, 6),
        "theta4_deg": round(theta4, 6),
    }


def fourbar_sweep(ground: float, crank: float, coupler: float, rocker: float,
                  config: str = "open", n_steps: int = 72,
                  coupler_point=(0.5, 0.0)) -> dict:
    """Drive a four-bar through a full crank revolution (or as far as it reaches).

    At each of ``n_steps`` crank angles it solves the position and tracks a coupler
    point (``coupler_point`` = (fraction along A→B, perpendicular offset fraction)).
    ``reachable`` is True only if the linkage closes at EVERY angle — i.e. the input
    crank makes a full revolution (a crank-rocker does; a triple-rocker hits a limit
    and ``n_reached`` < ``n_steps``).

    Returns {reachable, n_reached, coupler_path [[x,y],...], reachable_bbox_mm
    [w,h]}."""
    frac, perp = coupler_point
    path = []
    reached = 0
    for k in range(n_steps):
        th2 = 360.0 * k / n_steps
        pos = fourbar_position(ground, crank, coupler, rocker, th2, config)
        if pos is None:
            continue
        reached += 1
        A, B = pos["A"], pos["B"]
        # coupler point: along A->B by `frac`, offset perpendicular by `perp`*|AB|
        abx, aby = B[0] - A[0], B[1] - A[1]
        cp = (A[0] + frac * abx - perp * aby, A[1] + frac * aby + perp * abx)
        path.append([round(cp[0], 4), round(cp[1], 4)])
    if path:
        xs = [p[0] for p in path]
        ys = [p[1] for p in path]
        bbox = [round(max(xs) - min(xs), 4), round(max(ys) - min(ys), 4)]
    else:
        bbox = [0.0, 0.0]
    return {
        "reachable": reached == n_steps,
        "n_reached": reached,
        "coupler_path": path,
        "reachable_bbox_mm": bbox,
    }
