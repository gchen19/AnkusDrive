"""Photoreal renders of the rev 2.1 scanner with Blender (bpy 4.2, Cycles).
  /home/user/miniconda3/envs/bpy311/bin/python render_blender.py [quick]
Reads the STLs in this folder (mm), writes renders/*.png.
"""
import sys, os, json, math, time
import bpy
from mathutils import Vector, Matrix
bpy.ops.wm.read_factory_settings(use_empty=True)   # BEFORE any data is created

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, os.environ.get("RENDER_OUT", "renders")); os.makedirs(OUT, exist_ok=True)
QUICK = "quick" in sys.argv
META = json.load(open(os.path.join(HERE, "parts.json")))
C = META["_constants"]; HINGE = META["_hinge"]; FLIP_Z = META["_plate_flip_center_z"]
S = 0.001  # mm -> m

# ---------------------------------------------------------------- materials
def mat(name, color, rough=0.5, metal=0.0, trans=0.0, ior=1.5, emit=None, emit_str=0.0, alpha=1.0):
    m = bpy.data.materials.new(name); m.use_nodes = True
    bsdf = m.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Roughness"].default_value = rough
    bsdf.inputs["Metallic"].default_value = metal
    bsdf.inputs["Transmission Weight"].default_value = trans
    bsdf.inputs["IOR"].default_value = ior
    if emit:
        bsdf.inputs["Emission Color"].default_value = (*emit, 1.0); bsdf.inputs["Emission Strength"].default_value = emit_str
    if alpha < 1.0:
        bsdf.inputs["Alpha"].default_value = alpha; m.blend_method = "BLEND"
    return m

def srgb(h):  # hex -> linear rgb
    r, g, b = [int(h[i:i+2], 16) / 255 for i in (0, 2, 4)]
    f = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return (f(r), f(g), f(b))

def add_layer_lines(m, period_mm=0.2, strength=0.25):
    nt = m.node_tree; bsdf = nt.nodes["Principled BSDF"]
    tc = nt.nodes.new("ShaderNodeTexCoord"); sep = nt.nodes.new("ShaderNodeSeparateXYZ"); nt.links.new(tc.outputs["Object"], sep.inputs[0])
    wave = nt.nodes.new("ShaderNodeTexWave"); wave.wave_type = "BANDS"; wave.bands_direction = "Z"; wave.inputs["Scale"].default_value = 1.0 / (period_mm * S) / (2 * math.pi) * 0.5
    nt.links.new(tc.outputs["Object"], wave.inputs["Vector"])
    bump = nt.nodes.new("ShaderNodeBump"); bump.inputs["Strength"].default_value = strength; bump.inputs["Distance"].default_value = 0.00008
    nt.links.new(wave.outputs["Fac"], bump.inputs["Height"]); nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return m
MATS = {
    "print":   mat("PETG black", srgb("232327"), rough=0.5),
    "pad":     mat("pad", srgb("e9e8e4"), rough=0.22),
    "padlit":  mat("pad lit", srgb("ffffff"), rough=0.15, emit=(1, 1, 0.97), emit_str=1.6),
    "screw":   mat("screw", srgb("8d9196"), rough=0.35, metal=1.0),
    "plate":   mat("polystyrene", srgb("f2f6f9"), rough=0.06, trans=0.95, ior=1.59),
    "pcb":     mat("pcb", srgb("14502c"), rough=0.5),
    "metal":   mat("anodised", srgb("2b2b2e"), rough=0.35, metal=0.85),
    "alu":     mat("aluminium", srgb("b9bec4"), rough=0.35, metal=0.9),
    "steel":   mat("stainless", srgb("d0d3d6"), rough=0.22, metal=1.0),
    "screen":  mat("screen", srgb("cfe0f2"), rough=0.2, emit=(0.75, 0.86, 1.0), emit_str=2.5),
    "ribbon":  mat("ribbon", srgb("c9d3e0"), rough=0.6),
    "rubber":  mat("cable", srgb("111111"), rough=0.8),
    "harness": mat("harness", srgb("c8102e"), rough=0.6),
    "plug":    mat("plug", srgb("0d0d0f"), rough=0.32),
    "magnet":  mat("magnet", srgb("c7c9cc"), rough=0.25, metal=1.0),
    "bench":   mat("bench", srgb("8c8783"), rough=0.75),
    "cutface": mat("cut", srgb("d9483b"), rough=0.6),
}
add_layer_lines(MATS["print"])
PART_MAT = {"frame": "print", "tower": "print", "pi_sled": "print", "cam_cover": "print", "door": "print",
            "pad": "pad", "plate": "plate", "cam_board": "pcb", "lens": "metal", "pi": "pcb", "button": "steel",
            "display": "pcb", "screen": "screen", "ribbon": "ribbon", "cables": "rubber", "harness": "harness", "plugs": "plug", "hat": "pcb", "magnets": "magnet", "pad_lit": "padlit", "screws": "screw"}
FLEX = {"ribbon", "cables", "harness"}          # built as curves from cables.json, not from STL

# ---------------------------------------------------------------- scene
scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.samples = 24 if QUICK else 256
scene.cycles.use_denoising = True
scene.render.film_transparent = False
scene.view_settings.view_transform = "AgX"; scene.view_settings.look = "AgX - Medium High Contrast"; scene.view_settings.exposure = -0.9
if os.environ.get("RENDER_DEVICE") == "CPU":
    scene.cycles.device = "CPU"; print("cycles device: CPU (forced)")
    prefs = None
else:
  try:
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for dt in (os.environ.get("RENDER_GPU", "OPTIX"),):
        try:
            prefs.compute_device_type = dt; prefs.get_devices()
            for d in prefs.devices: d.use = (d.type == dt)
            scene.cycles.device = "GPU"; print("cycles device:", dt, [d.name for d in prefs.devices if d.use]); break
        except Exception as e: print("no", dt, e)
  except Exception as e:
    print("GPU setup failed, CPU:", e); scene.cycles.device = "CPU"
scene.render.threads_mode = "AUTO"

# world: studio HDRI (Poly Haven, CC0) with a neutral grey backdrop visible to the camera
w = bpy.data.worlds.new("w"); scene.world = w; w.use_nodes = True
nt = w.node_tree; bg = nt.nodes["Background"]
env = nt.nodes.new("ShaderNodeTexEnvironment"); env.image = bpy.data.images.load(os.path.join(HERE, "env", "studio_small_09_2k.hdr"))
mapn = nt.nodes.new("ShaderNodeMapping"); tc = nt.nodes.new("ShaderNodeTexCoord")
nt.links.new(tc.outputs["Generated"], mapn.inputs["Vector"]); mapn.inputs["Rotation"].default_value = (0, 0, math.radians(35)); nt.links.new(mapn.outputs["Vector"], env.inputs["Vector"])
bg2 = nt.nodes.new("ShaderNodeBackground"); bg2.inputs[0].default_value = (0.86, 0.86, 0.88, 1); bg2.inputs[1].default_value = 1.0
lp = nt.nodes.new("ShaderNodeLightPath"); mix = nt.nodes.new("ShaderNodeMixShader")
nt.links.new(env.outputs["Color"], bg.inputs["Color"]); bg.inputs[1].default_value = 1.0
nt.links.new(lp.outputs["Is Camera Ray"], mix.inputs["Fac"]); nt.links.new(bg.outputs["Background"], mix.inputs[1]); nt.links.new(bg2.outputs["Background"], mix.inputs[2])
out = nt.nodes["World Output"]; nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])

def area(name, loc, target, size, energy, color=(1, 1, 1)):
    l = bpy.data.lights.new(name, "AREA"); l.energy = energy; l.size = size; l.color = color
    o = bpy.data.objects.new(name, l); scene.collection.objects.link(o); o.location = loc
    d = Vector(target) - Vector(loc); o.rotation_euler = d.to_track_quat("-Z", "Y").to_euler(); return o
area("key", (0.6, -0.7, 0.9), (0, 0, 0.12), 0.8, 140)
area("fill", (-0.8, -0.3, 0.5), (0, 0, 0.12), 1.2, 40, (0.95, 0.97, 1.0))
area("rim", (0.2, 0.9, 0.7), (0, 0, 0.12), 0.6, 120)
bench = bpy.data.objects.new("bench", bpy.data.meshes.new("bench"))
import bmesh
bm = bmesh.new(); bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=1.5); bm.to_mesh(bench.data); bm.free()
scene.collection.objects.link(bench); bench.location = (0, 0, -0.0051); bench.data.materials.append(MATS["bench"])

def import_stl(name, fn):
    before = set(bpy.data.objects)
    bpy.ops.wm.stl_import(filepath=os.path.join(HERE, fn))
    o = [x for x in bpy.data.objects if x not in before][0]; o.name = name
    o.data.transform(Matrix.Diagonal((S, S, S, 1.0)))       # bake mm->m into the mesh; the importer only set object scale
    o.scale = (1, 1, 1); o.matrix_world = Matrix.Identity(4)
    o.data.materials.clear(); o.data.materials.append(MATS[PART_MAT.get(name.replace("cut_", ""), "print")])
    for p in o.data.polygons: p.use_smooth = False
    if PART_MAT.get(name.replace("cut_", "").replace("_w", "").replace("_cut", ""), "") == "print":
        bv = o.modifiers.new("bevel", "BEVEL"); bv.width = 0.0004; bv.segments = 2; bv.limit_method = "ANGLE"
    # smooth cylinders: auto-smooth by angle
    with bpy.context.temp_override(object=o, selected_editable_objects=[o]):
        try: bpy.ops.object.shade_smooth_by_angle(angle=math.radians(35))
        except Exception:
            try: bpy.ops.object.shade_auto_smooth(angle=math.radians(35))
            except Exception: pass
    return o

PARTS = {}
for n in PART_MAT:
    if n in FLEX: continue
    if os.path.exists(os.path.join(HERE, n + ".stl")): PARTS[n] = import_stl(n, n + ".stl")
exec(open(os.path.join(HERE, "cables_blender.py")).read())
PARTS.update(build_cables(json.load(open(os.path.join(HERE, "cables.json")))))

def set_visible(names):
    for n, o in PARTS.items(): o.hide_render = n not in names
def reset():
    for o in PARTS.values(): o.matrix_world = Matrix.Identity(4); o.hide_render = False
def move(n, dx=0, dy=0, dz=0):
    PARTS[n].matrix_world = Matrix.Translation(Vector((dx, dy, dz)) * S) @ PARTS[n].matrix_world
def rotate_about(n, axis, angle_deg, pivot_mm):
    p = Vector(pivot_mm) * S
    R = Matrix.Rotation(math.radians(angle_deg), 4, axis)
    PARTS[n].matrix_world = Matrix.Translation(p) @ R @ Matrix.Translation(-p) @ PARTS[n].matrix_world

cam_data = bpy.data.cameras.new("cam"); cam = bpy.data.objects.new("cam", cam_data); scene.collection.objects.link(cam); scene.camera = cam
ONLY = [x for x in os.environ.get("RENDER_ONLY", "").split(",") if x]
def shoot(fn, loc_mm, target_mm, lens=50, res=(1800, 1350), fstop=None):
    if ONLY and fn.split(".")[0] not in ONLY: return
    cam.location = Vector(loc_mm) * S
    cam_data.dof.use_dof = fstop is not None
    if fstop: cam_data.dof.aperture_fstop = fstop; cam_data.dof.focus_distance = (Vector(target_mm) * S - cam.location).length
    d = Vector(target_mm) * S - cam.location; cam.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    cam_data.lens = lens
    scene.render.resolution_x, scene.render.resolution_y = (res[0] // 2, res[1] // 2) if QUICK else res
    scene.render.filepath = os.path.join(OUT, fn); t = time.time(); bpy.ops.render.render(write_still=True); print(fn, f"{time.time()-t:.0f}s")

ALL = set(PARTS)
OPEN = -170   # the door flips up and parks against the tower's front wall
INSIDE_HIDE = {"frame", "pad", "pad_lit", "plate", "door", "magnets", "cables"}
# 1. hero: the face you work at (door, display, button on the roof), from the front-left
reset(); set_visible(ALL); shoot("hero.png", (-430, -600, 330), (0, -20, 120), lens=55, res=(2000, 1500))
reset(); set_visible(ALL); shoot("front.png", (360, -560, 320), (0, -20, 118), lens=55, res=(2000, 1500))
# 2. rear 3/4: ports, vents and cables on the back and right walls
reset(); set_visible(ALL); shoot("rear.png", (420, 520, 320), (0, 25, 130), lens=55)
# 3. exploded
reset(); set_visible(ALL - {"ribbon", "cables", "magnets", "harness", "plugs"})
move("plate", dz=45); move("door", dy=-55)
move("tower", dz=110); move("cam_board", dz=190); move("lens", dz=190); move("screws", dz=190); move("pi", dz=190); move("button", dz=250)
move("display", dz=110, dy=-45); move("screen", dz=110, dy=-45)
shoot("exploded.png", (-560, -660, 520), (0, -10, 190), lens=52, res=(1800, 1500))
# 4. section at X=45 (display, lens, button, the Pi cut through)
for o in PARTS.values(): o.hide_render = True
CUT = {}
for n in PART_MAT:
    fn = "cut_" + n + ".stl"
    if os.path.exists(os.path.join(HERE, fn)): CUT[n] = import_stl(n + "_cut", fn)
shoot("section.png", (560, -320, 280), (0, -5, 120), lens=60)
for o in CUT.values(): bpy.data.objects.remove(o)
# 5. door parked open, plate half inserted lid-up
reset(); set_visible(ALL)
rotate_about("door", "X", OPEN, (0, HINGE["y"], HINGE["z"])); move("plate", dy=-75)
shoot("loading.png", (-300, -560, 200), (0, -40, 70), lens=60)
# 6. flipped plate seated, door parked open
reset(); set_visible(ALL)
rotate_about("door", "X", OPEN, (0, HINGE["y"], HINGE["z"])); rotate_about("plate", "X", 180, (0, 0, FLIP_Z))
shoot("flipped.png", (-260, -520, 200), (0, -30, 40), lens=60)
# 7. camera detail: looking up at the ceiling from inside the tube (tower cut at x=45 hides nothing; hide the base instead)
reset(); set_visible(ALL - INSIDE_HIDE)
shoot("camera.png", (-40, -30, 60), (0, 0, 228), lens=28, fstop=8)
# 8. inside: the ceiling with camera, Pi, button body and the display's back, seen from below at an angle
reset(); set_visible(ALL - INSIDE_HIDE)
shoot("inside.png", (38, -28, 92), (18, 6, 226), lens=22, fstop=8)
# 8c. wiring: display cable and button wires to the Pi header, ribbon along the ceiling
reset(); set_visible(ALL - INSIDE_HIDE - {"tower"})
shoot("wiring.png", (-60, -120, 130), (8, -8, 212), lens=45, fstop=8)
# 8d. cables leaving the back and right walls
reset(); set_visible(ALL)
shoot("cables.png", (520, 300, 260), (60, 40, 170), lens=60, fstop=8)
# 8b. control face close-up: display in the front wall, button on the roof
reset(); set_visible(ALL)
shoot("panel.png", (-130, -330, 300), (-20, -48, 200), lens=70, fstop=5.6)
# 9. door hinge detail, door half open
reset(); set_visible({"frame", "door", "magnets", "tower"})
rotate_about("door", "X", -60, (0, HINGE["y"], HINGE["z"]))
shoot("hinge.png", (-190, -170, 95), (-78, -52, 42), lens=85, fstop=4)
print("done")
