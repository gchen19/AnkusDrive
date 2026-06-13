"""Mechanism mobility + ratio oracle — does the assembly actually MOVE?

Pure-Python, FreeCAD-free. The static merge gates (interference, bore_fit,
gear_mesh, mass) are *pose* oracles: they confirm the parts fit in one frozen
snapshot. They say nothing about whether the mechanism has the degrees of freedom
to move, or whether it transmits motion at the intended ratio. A parallel-shaft
gearbox whose every gear is rigidly keyed to its shaft passes all of them and is
still a dead lump of steel — six pairs rigidly demanding six different ratios of
the same two shafts is over-constrained, so the only consistent motion is none.

This module is the closed-form motion oracle. It sits on the exact Grübler /
Kutzbach criterion in :mod:`driftpin.analysis.kinematics` and adds the two things a
gear network needs on top of a raw DOF count:

  * **Ratio consistency** — propagate a rate through the mesh graph; if two paths
    (or two parallel meshes) between the same shafts imply different speed ratios,
    the network is over-constrained and locks. This *names the conflict*, which a
    bare ``mobility_dof = -4`` does not.
  * **Selective engagement** — a real constant-mesh gearbox is functional because
    only ONE pair is dog-clutched to the output at a time; the rest freewheel. Given
    an engagement schedule, each state is analysed on its own: the power path must be
    a determinate single DOF and its realised ratio must hit the design ratio.

The same geometry can be *declared* two ways — every gear rigid (locked) or output
gears freewheeling with selective engagement (functional) — and the oracle
separates them. That separation is the whole point: it is an emergent, system-level
property no single component builder can verify from its own slice.

Conventions. A mesh carries integer ``teeth_a``/``teeth_b`` (preferred) or a size
``ratio`` = Nb/Na. The signed *speed* ratio it imposes is ω_b/ω_a = ±Na/Nb, minus
for an external pair (teeth reverse rotation), plus for internal. Design ratios are
expressed the kinematic way, ω_out/ω_in. See ``tests/test_mechanism.py`` for the
gearbox worked both ways (rigid → locked; selective → +1 per speed).
"""
from __future__ import annotations

from driftpin.analysis.kinematics import gruebler_dof

_RATIO_TOL = 0.02   # relative tolerance on a realised vs design / cross-path ratio


def _speed_ratio(mesh: dict) -> float:
    """Signed kinematic speed ratio ω_b/ω_a imposed by one mesh.

    From integer teeth (ω_b/ω_a = ±Na/Nb) when present, else from a size ``ratio``
    = Nb/Na (ω_b/ω_a = ±1/ratio). External pairs reverse direction (negative); set
    ``external: false`` for an internal/annular mesh or a belt/chain."""
    sign = 1.0 if mesh.get("external") is False else -1.0
    if "teeth_a" in mesh and "teeth_b" in mesh:
        return sign * float(mesh["teeth_a"]) / float(mesh["teeth_b"])
    if "ratio" in mesh:                                   # ratio = Nb/Na (size)
        return sign / float(mesh["ratio"])
    raise ValueError(f"mesh {mesh.get('id', mesh)} needs teeth_a/teeth_b or ratio")


def meshes_from_checks(checks: list) -> list:
    """Derive mesh edges from already-validated gear_mesh `checks` (same edges, named
    by instance) so a manifest's `mechanism` block need not duplicate them. Each
    gear_mesh check carries a size ``ratio`` = Nb/Na; the id defaults to ``a__b``."""
    out = []
    for chk in checks:
        if chk.get("kind") == "gear_mesh":
            m = {"a": chk["a"], "b": chk["b"], "id": chk.get("id", f"{chk['a']}__{chk['b']}")}
            if "ratio" in chk:
                m["ratio"] = chk["ratio"]
            out.append(m)
    return out


def validate(mech: dict, inst_names, checks) -> list:
    """Cross-reference the `mechanism` block against the assembly: link members and
    mesh endpoints must be real instances; engagement states must name declared
    meshes; freewheel gears must be instances. Returns a list of problems (empty ==
    valid) so the merge can reject a contract typo at the front door (RFC §11.7)."""
    problems = []
    if not isinstance(mech, dict):
        return ["mechanism must be an object"]
    links = mech.get("links", {})
    if not isinstance(links, dict) or not links:
        problems.append("mechanism.links must be a non-empty object")
        links = links if isinstance(links, dict) else {}
    for lname, ldef in links.items():
        for member in (ldef or {}).get("members", [lname]):
            if member not in inst_names:
                problems.append(
                    f"mechanism link {lname!r} member {member!r} is not an instance")
    mesh_ids = set()
    for m in mech.get("meshes", []):
        for ref in (m.get("a"), m.get("b")):
            if ref is not None and ref not in inst_names:
                problems.append(f"mechanism mesh references unknown instance {ref!r}")
        mesh_ids.add(m.get("id", f"{m.get('a')}__{m.get('b')}"))
    for chk in checks:
        if chk.get("kind") == "gear_mesh":
            mesh_ids.add(chk.get("id", f"{chk['a']}__{chk['b']}"))
    eng = mech.get("engagement")
    if isinstance(eng, dict):
        for sname, ids in (eng.get("states") or {}).items():
            for mid in ids:
                if mid not in mesh_ids:
                    problems.append(
                        f"engagement state {sname!r} names unknown mesh {mid!r}")
        for g in eng.get("freewheel", []):
            if g not in inst_names:
                problems.append(f"engagement freewheel {g!r} is not an instance")
    return problems


def _member_to_link(links: dict) -> dict:
    """Map every rigid member instance → the name of the link it is welded into."""
    m2l = {}
    for lname, ldef in links.items():
        for member in ldef.get("members", [lname]):
            m2l[member] = lname
    return m2l


def _propagate_rates(link_nodes, edges):
    """Assign each link node a relative rate by walking the mesh graph, checking
    consistency as we close loops. ``edges`` is a list of (u, v, ratio) with ratio =
    ω_v/ω_u. Returns (rates dict, conflicts list). A conflict is a closed path whose
    implied ratio disagrees with the rate already assigned — the over-constraint that
    locks the network."""
    adj = {n: [] for n in link_nodes}
    for u, v, r in edges:
        adj[u].append((v, r))
        adj[v].append((u, 1.0 / r if r else 0.0))
    rates, conflicts = {}, []
    for seed in link_nodes:
        if seed in rates:
            continue
        rates[seed] = 1.0
        stack = [seed]
        while stack:
            u = stack.pop()
            for v, r in adj[u]:
                implied = rates[u] * r
                if v not in rates:
                    rates[v] = implied
                    stack.append(v)
                elif abs(implied - rates[v]) > _RATIO_TOL * max(1.0, abs(rates[v])):
                    conflicts.append({
                        "between": [u, v],
                        "implied": round(implied, 4),
                        "established": round(rates[v], 4),
                        "reason": f"mesh path {u}->{v} implies rate {implied:.3f} but "
                                  f"{v} is already fixed at {rates[v]:.3f} by another "
                                  f"path — conflicting ratios over-constrain the train",
                    })
    return rates, conflicts


def _rigid_dof(links: dict, meshes: list, m2l: dict) -> dict:
    """Grübler DOF and ratio consistency with EVERY member welded into its link."""
    n_links = len(links) + 1                              # + ground
    joints = []
    for ldef in links.values():
        g = ldef.get("ground")
        if g:
            joints.append({"type": g})                    # lower pair to ground
    edges = []
    for mesh in meshes:
        la, lb = m2l.get(mesh["a"]), m2l.get(mesh["b"])
        if la is None or lb is None:
            continue
        joints.append({"type": "gear"})                   # higher pair
        if la != lb:
            edges.append((la, lb, _speed_ratio(mesh)))
    dof = gruebler_dof(n_links, joints)
    _, conflicts = _propagate_rates(list(links), edges)
    return {"mobility_dof": dof, "consistent": not conflicts, "conflicts": conflicts}


def _state_dof(links, meshes, m2l, engagement, engaged_ids):
    """Analyse one engagement state. Freewheel gears not engaged in this state become
    their own idle links (revolute to the shaft they ride on); engaged freewheel gears
    are dog-clutched (welded) to their host shaft. Returns DOF, the determinate
    power-path DOF, and the realised input→output speed ratio."""
    freewheel = set(engagement.get("freewheel", []))
    rides_on = engagement.get("rides_on", {})
    engaged = {m["id"]: m for m in meshes if m.get("id") in engaged_ids}
    # which freewheel gears get welded this state (they are in an engaged mesh)
    welded = {g for m in engaged.values() for g in (m["a"], m["b"]) if g in freewheel}

    def link_of(gear):
        if gear in m2l:                                   # rigid member of a shaft
            return m2l[gear]
        if gear in welded:                                # dog-clutched this state
            return rides_on.get(gear, gear)
        return f"free:{gear}"                             # idle, own link

    idle_links, joints, edges = set(), [], []
    for ldef_name, ldef in links.items():
        if ldef.get("ground"):
            joints.append({"type": ldef["ground"]})
    for mesh in meshes:
        la, lb = link_of(mesh["a"]), link_of(mesh["b"])
        for g, lk in ((mesh["a"], la), (mesh["b"], lb)):
            if lk.startswith("free:") and lk not in idle_links:
                idle_links.add(lk)
                host = rides_on.get(g)
                if host:
                    joints.append({"type": "revolute"})   # idle gear spins on its shaft
        joints.append({"type": "gear"})
        if la != lb:
            edges.append((la, lb, _speed_ratio(mesh)))
    n_links = len(links) + len(idle_links) + 1
    dof = gruebler_dof(n_links, joints)

    # power path: ground + the two shafts + the engaged (welded) meshes only
    inp, out = engagement.get("input"), engagement.get("output")
    pp_edges = []
    pp_joints = []
    for lk in (inp, out):
        if lk and links.get(lk, {}).get("ground"):
            pp_joints.append({"type": links[lk]["ground"]})
    for m in engaged.values():
        la, lb = link_of(m["a"]), link_of(m["b"])
        pp_joints.append({"type": "gear"})
        if la != lb:
            pp_edges.append((la, lb, _speed_ratio(m)))
    pp_nodes = {inp, out} | {e[0] for e in pp_edges} | {e[1] for e in pp_edges}
    pp_nodes.discard(None)
    pp_dof = gruebler_dof(len(pp_nodes) + 1, pp_joints)
    rates, pp_conflicts = _propagate_rates(list(pp_nodes), pp_edges)
    realised = None
    if inp in rates and out in rates and abs(rates[inp]) > 1e-12:
        realised = rates[out] / rates[inp]
    return {"mobility_dof": dof, "power_path_dof": pp_dof,
            "ratio_realised": None if realised is None else round(realised, 5),
            "conflicts": pp_conflicts}


def analyze(spec: dict) -> dict:
    """Closed-form motion analysis of a mechanism declared as links + gear meshes.

    ``spec`` = {expected_dof, input, output, links: {name: {members:[inst...],
    ground:'revolute'|'prismatic'|None}}, meshes: [{a, b, teeth_a, teeth_b | ratio,
    external?, id}], engagement?: {freewheel:[gear...], rides_on:{gear: shaft},
    states:{name:[mesh_id...]}, design_ratios:{name: ω_out/ω_in}, input, output}}.

    With no ``engagement`` the whole thing is rigid: it passes iff the mobility DOF
    equals ``expected_dof`` (default 1) AND the mesh ratios are consistent — the
    rigid gearbox fails here (DOF < 1 and conflicting ratios). With an engagement
    schedule each state is checked for a determinate single-DOF power path whose
    realised ratio matches the design ratio. Returns {ok, verdict, expected_dof,
    rigid, states, violations}; ``violations`` is empty on a pass (the gate folds it
    into the merge ``ok``)."""
    links = spec["links"]
    meshes = spec.get("meshes", [])
    m2l = _member_to_link(links)
    expected = int(spec.get("expected_dof", 1))
    rigid = _rigid_dof(links, meshes, m2l)
    out = {"expected_dof": expected, "rigid": rigid, "states": {}, "violations": []}

    eng = spec.get("engagement")
    if not eng:
        viol = []
        if rigid["mobility_dof"] < expected:
            verdict = "locked" if rigid["mobility_dof"] < 1 else "overconstrained"
            reason = (f"mobility DOF {rigid['mobility_dof']:+d} < expected {expected} "
                      f"— cannot move")
            if rigid["conflicts"]:
                reason += f"; {rigid['conflicts'][0]['reason']}"
            viol.append({"reason": reason, "mobility_dof": rigid["mobility_dof"]})
        elif rigid["mobility_dof"] > expected:
            verdict = "underconstrained"
            viol.append({"reason": f"mobility DOF {rigid['mobility_dof']} > expected "
                                   f"{expected} — floppy / unconstrained freedom"})
        elif not rigid["consistent"]:
            verdict = "overconstrained"
            viol.append({"reason": rigid["conflicts"][0]["reason"]})
        else:
            verdict = "functional"
        out["verdict"], out["violations"], out["ok"] = verdict, viol, not viol
        return out

    # selective: merge engagement's input/output into the spec view used per state
    eng = {**eng, "input": eng.get("input", spec.get("input")),
           "output": eng.get("output", spec.get("output"))}
    design = eng.get("design_ratios", {})
    viol = []
    for sname, engaged_ids in eng.get("states", {}).items():
        st = _state_dof(links, meshes, m2l, eng, set(engaged_ids))
        st_viol = []
        if st["power_path_dof"] != expected:
            st_viol.append(f"power-path DOF {st['power_path_dof']:+d} != {expected}")
        if st["conflicts"]:
            st_viol.append(st["conflicts"][0]["reason"])
        dr = design.get(sname)
        if dr is not None and st["ratio_realised"] is not None:
            st["ratio_design"] = dr
            if abs(abs(st["ratio_realised"]) - abs(dr)) > _RATIO_TOL * max(1.0, abs(dr)):
                st_viol.append(f"realised ratio {st['ratio_realised']} != design {dr}")
            st["ratio_ok"] = not any("ratio" in v for v in st_viol)
        st["ok"] = not st_viol
        if st_viol:
            viol.append({"state": sname, "reason": "; ".join(st_viol)})
        out["states"][sname] = st
    out["verdict"] = "functional" if not viol else "locked"
    out["violations"], out["ok"] = viol, not viol
    return out
