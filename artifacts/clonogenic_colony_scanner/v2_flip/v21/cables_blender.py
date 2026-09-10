"""Real cables for the Blender renders: every flexible run in cables.json becomes a smooth,
tangent-continuous curve (fillets of the route's bend radius) with a round bevel, per-conductor
insulation colours for the looms and a flat strip for the camera FPC. Exec'd by render_blender.py
with bpy, S (mm->m), mat(), srgb() and MATS in scope; defines build_cables(routes) -> {part: object}."""
import math
import bpy, bmesh
from mathutils import Vector, Matrix

INSUL = {  # PVC insulation, sRGB
    "grey": "b9bbbd", "purple": "6f3aa8", "blue": "1e4fd0", "green": "2aa14a", "yellow": "f0cf1c", "orange": "ee7a1c",
    "red": "cf2621", "brown": "64391a", "black": "151515", "white": "ececec", "ivory": "e7e1d1",
}
_mats = {}
def insul_mat(name):
    if name not in _mats:
        m = mat("wire " + name, srgb(INSUL[name]), rough=0.42)
        m.node_tree.nodes["Principled BSDF"].inputs["Specular IOR Level"].default_value = 0.35
        _mats[name] = m
    return _mats[name]

def fillet_polyline(pts, r, n_arc=14):
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        p0, p1, p2 = pts[i - 1], pts[i], pts[i + 1]
        d0, d2 = p0 - p1, p2 - p1
        l0, l2 = d0.length, d2.length
        if l0 < 1e-6 or l2 < 1e-6: continue
        u0, u2 = d0 / l0, d2 / l2
        ang = math.acos(max(-1.0, min(1.0, u0.dot(u2))))
        if ang > math.pi - 1e-3: continue
        rr = min(r, 0.49 * l0, 0.49 * l2)
        tlen = rr / math.tan(ang / 2)
        a, b = p1 + u0 * tlen, p1 + u2 * tlen
        bis = (u0 + u2).normalized()
        c = p1 + bis * (rr / math.sin(ang / 2))
        va, vb = a - c, b - c
        w = math.acos(max(-1.0, min(1.0, va.dot(vb) / (va.length * vb.length))))
        for k in range(n_arc + 1):
            t = k / n_arc
            q = va if w < 1e-6 else va * (math.sin((1 - t) * w) / math.sin(w)) + vb * (math.sin(t * w) / math.sin(w))
            out.append(c + q)
    out.append(pts[-1])
    # drop duplicates
    res = [out[0]]
    for p in out[1:]:
        if (p - res[-1]).length > 1e-4: res.append(p)
    return res

def frames(pts, hint):
    """Parallel-transport frames: returns (tangents, side vectors) with side ⟂ tangent, starting near `hint`."""
    n = len(pts); T = []
    for i in range(n):
        a = pts[max(i - 1, 0)]; b = pts[min(i + 1, n - 1)]
        T.append((b - a).normalized())
    s0 = (hint - T[0] * hint.dot(T[0]))
    if s0.length < 1e-6: s0 = T[0].orthogonal()
    Sv = [s0.normalized()]
    for i in range(1, n):
        axis = T[i - 1].cross(T[i])
        if axis.length < 1e-8: Sv.append(Sv[-1].copy()); continue
        ang = math.asin(max(-1.0, min(1.0, axis.length)))
        if T[i - 1].dot(T[i]) < 0: ang = math.pi - ang
        R = Matrix.Rotation(ang, 3, axis.normalized())
        s = R @ Sv[-1]; s = (s - T[i] * s.dot(T[i])).normalized(); Sv.append(s)
    return T, Sv

def curve_object(name, pts_mm, radius_mm, material):
    cu = bpy.data.curves.new(name, "CURVE"); cu.dimensions = "3D"
    cu.bevel_depth = radius_mm * S; cu.bevel_resolution = 8; cu.use_fill_caps = True; cu.resolution_u = 2
    sp = cu.splines.new("POLY"); sp.points.add(len(pts_mm) - 1)
    for p, q in zip(sp.points, pts_mm): p.co = (q.x * S, q.y * S, q.z * S, 1.0)
    sp.use_smooth = True
    o = bpy.data.objects.new(name, cu); bpy.context.scene.collection.objects.link(o)
    cu.materials.append(material)
    return o

def to_mesh_join(objs, name):
    """Convert curves to meshes and join into one object (keeps material slots)."""
    meshes = []
    for o in objs:
        dg = bpy.context.evaluated_depsgraph_get()
        me = bpy.data.meshes.new_from_object(o.evaluated_get(dg), depsgraph=dg)
        me.transform(o.matrix_world)
        mo = bpy.data.objects.new(o.name + "_m", me); bpy.context.scene.collection.objects.link(mo)
        for m in o.data.materials: mo.data.materials.append(m)
        meshes.append(mo); bpy.data.objects.remove(o)
    bm = bmesh.new(); out = bpy.data.meshes.new(name); slots = []
    for mo in meshes:
        me = mo.data
        base = len(slots)
        for m in me.materials: slots.append(m)
        for p in me.polygons: p.material_index += base
        bm.from_mesh(me)
        bpy.data.objects.remove(mo)
    bm.to_mesh(out); bm.free()
    for m in slots: out.materials.append(m)
    for p in out.polygons: p.use_smooth = True
    o = bpy.data.objects.new(name, out); bpy.context.scene.collection.objects.link(o)
    return o

def ribbon_strip(name, pts_mm, width, t, width_end=None, taper_mm=0.0, material=None):
    T, Sv = frames(pts_mm, Vector((1, 0, 0)))
    # arclength from the end for the taper
    L = [0.0]
    for a, b in zip(pts_mm[:-1], pts_mm[1:]): L.append(L[-1] + (b - a).length)
    tot = L[-1]
    me = bpy.data.meshes.new(name); bm = bmesh.new(); prev = None
    for i, p in enumerate(pts_mm):
        w = width
        if width_end and taper_mm > 0:
            rem = tot - L[i]
            if rem < taper_mm: w = width_end + (width - width_end) * (rem / taper_mm)
        a = (p + Sv[i] * (w / 2)) * S; b = (p - Sv[i] * (w / 2)) * S
        va, vb = bm.verts.new(a), bm.verts.new(b)
        if prev: bm.faces.new((prev[0], prev[1], vb, va))
        prev = (va, vb)
    bm.to_mesh(me); bm.free()
    o = bpy.data.objects.new(name, me); bpy.context.scene.collection.objects.link(o)
    me.materials.append(material)
    for p in me.polygons: p.use_smooth = True
    sol = o.modifiers.new("t", "SOLIDIFY"); sol.thickness = t * S; sol.offset = 0.0; sol.use_even_offset = True
    return o

def loom_objects(name, pts_mm, wire_r, colors, bend_r, row_hint=Vector((1, 0, 0))):
    dense = fillet_polyline(pts_mm, bend_r)
    T, Sv = frames(dense, row_hint)
    n = len(colors); pitch = 2 * wire_r * 1.03; objs = []
    for k, col in enumerate(colors):
        off = (k - (n - 1) / 2) * pitch
        pts = [p + s * off for p, s in zip(dense, Sv)]
        objs.append(curve_object(f"{name}_{k}", pts, wire_r, insul_mat(col)))
    # a short heat-shrink sleeve where the loom leaves each connector
    for end in (0, -1):
        i0 = 0 if end == 0 else len(dense) - 1
        d = T[i0]
        seg = [dense[i0] + d * (1.5 if end == 0 else -1.5), dense[i0] + d * (9.0 if end == 0 else -9.0)]
        ins = insul_mat("black")
        objs.append(curve_object(f"{name}_sleeve{end}", seg, (n * pitch) / 2 * 0.62 + 0.3, ins))
    return objs

def build_cables(routes):
    """-> {'ribbon': obj, 'harness': obj, 'cables': obj}"""
    V = lambda p: Vector(p)
    out = {}
    r = routes["ribbon"]
    dense = fillet_polyline([V(p) for p in r["pts"]], r["bend_r"], n_arc=16)
    rib = ribbon_strip("ribbon", dense, r["width"], r["t"], r.get("width_end"), r.get("taper_mm", 0), insul_mat("ivory"))
    out["ribbon"] = to_mesh_join([rib], "ribbon")
    looms = []
    for key, hint in (("loom_display", Vector((1, 0, 0))), ("loom_button", Vector((1, 0, 0)))):
        r = routes[key]; looms += loom_objects(key, [V(p) for p in r["pts"]], r["wire_r"], r["colors"], r["bend_r"], hint)
    out["harness"] = to_mesh_join(looms, "harness")
    cabs = []
    for key in ("power", "ethernet", "pad_usb"):
        r = routes[key]; dense = fillet_polyline([V(p) for p in r["pts"]], r["bend_r"])
        cabs.append(curve_object(key, dense, r["r"], insul_mat(r["color"])))
    out["cables"] = to_mesh_join(cabs, "cables")
    return out
