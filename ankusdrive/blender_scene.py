"""Studio scene + render, run INSIDE Blender — issue #335.

    blender --background --factory-startup --python blender_scene.py -- job.json

Checked in (never agent-generated) so a render is repeatable and testable. Reads the
job written by ``ankusdrive/blender_render.py``, builds the parts from the worker's
raw tessellation buffers, applies per-part Principled BSDF materials, sets up the
studio (seamless cyclorama, key/fill/rim area lights, camera framed from the view),
renders with Cycles and saves the ``.blend`` for hand refinement (e.g. through
Blender's own MCP server). Writes ``result.json`` either way.

Imports only ``bpy`` and the stdlib (plus Blender's bundled numpy); targets Blender
4.2 LTS and newer. Not importable outside Blender.
"""
import json
import math
import os
import sys
import time
import traceback

import bpy
import numpy as np
from mathutils import Matrix, Vector


def _args():
    argv = sys.argv
    if "--" not in argv:
        raise SystemExit("usage: blender --background --python blender_scene.py -- job.json")
    return argv[argv.index("--") + 1:]


def _socket(node, *names):
    """First existing input socket among ``names`` (names moved between versions)."""
    for n in names:
        if n in node.inputs:
            return node.inputs[n]
    return None


def _set(node, value, *names):
    s = _socket(node, *names)
    if s is not None:
        s.default_value = value


# --- scene reset -------------------------------------------------------------------

def _reset():
    for coll in (bpy.data.objects, bpy.data.meshes, bpy.data.materials, bpy.data.lights,
                 bpy.data.cameras, bpy.data.worlds):
        for block in list(coll):
            coll.remove(block)


# --- parts -------------------------------------------------------------------------

def _load(job_dir, name, dtype, width):
    return np.fromfile(os.path.join(job_dir, name), dtype=dtype).reshape(-1, width)


def _build_part(job_dir, part):
    verts = _load(job_dir, part["vertices"], "<f4", 3)
    tris = _load(job_dir, part["triangles"], "<i4", 3)
    mesh = bpy.data.meshes.new(part["name"])
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", verts.ravel())
    mesh.loops.add(tris.size)
    mesh.loops.foreach_set("vertex_index", tris.ravel())
    mesh.polygons.add(len(tris))
    mesh.polygons.foreach_set("loop_start", np.arange(0, tris.size, 3, dtype=np.int32))
    mesh.update(calc_edges=True)
    mesh.validate(clean_customdata=False)
    mesh.polygons.foreach_set("use_smooth", np.ones(len(mesh.polygons), dtype=bool))
    if part.get("normals"):
        # Exact B-rep normals per vertex. Vertices are not shared across B-rep faces,
        # so face boundaries stay crisp while curved faces shade smooth.
        normals = _load(job_dir, part["normals"], "<f4", 3)
        if len(normals) == len(mesh.vertices):
            mesh.normals_split_custom_set_from_vertices([tuple(n) for n in normals])
    mesh.update()
    obj = bpy.data.objects.new(part["name"], mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


# --- materials ---------------------------------------------------------------------

def _principled_material(name, ap):
    mat = bpy.data.materials.new(name)
    if hasattr(mat, "use_nodes") and not mat.use_nodes:
        mat.use_nodes = True                         # always-on (and deprecated) in 5.x
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    color = list(ap["color"]) + [1.0]
    _set(bsdf, color, "Base Color")
    _set(bsdf, ap.get("metallic", 0.0), "Metallic")
    _set(bsdf, ap.get("roughness", 0.5), "Roughness")
    _set(bsdf, ap.get("transmission", 0.0), "Transmission Weight", "Transmission")
    _set(bsdf, ap.get("ior", 1.45), "IOR")
    _set(bsdf, ap.get("coat", 0.0), "Coat Weight", "Clearcoat")
    if ap.get("emission"):
        _set(bsdf, list(ap["emission"]) + [1.0], "Emission Color", "Emission")
        _set(bsdf, ap.get("emission_strength", 1.0), "Emission Strength")
    mat.diffuse_color = color                        # solid-viewport colour in the .blend
    finish = ap.get("finish", "none")
    if finish == "fdm_layers":
        _fdm_layers(nt, bsdf, ap.get("layer_height_mm", 0.2) / 1000.0)
    elif finish == "brushed":
        _brushed(nt, bsdf, ap.get("roughness", 0.3))
    return mat


def _fdm_layers(nt, bsdf, layer_m):
    """Horizontal print layer lines: a Z-banded wave driving a shallow bump."""
    geo = nt.nodes.new("ShaderNodeNewGeometry")
    wave = nt.nodes.new("ShaderNodeTexWave")
    wave.wave_type = "BANDS"
    wave.bands_direction = "Z"
    bump = nt.nodes.new("ShaderNodeBump")
    nt.links.new(geo.outputs["Position"], wave.inputs["Vector"])
    # Wave BANDS repeat every 1/scale units: one band per printed layer.
    _set(wave, 1.0 / max(layer_m, 1e-6), "Scale")
    _set(wave, 0.0, "Distortion")
    _set(bump, 0.25, "Strength")
    _set(bump, layer_m * 0.5, "Distance")
    nt.links.new(wave.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])


def _brushed(nt, bsdf, roughness):
    """Directional brushed metal: stretched noise into roughness + anisotropy."""
    coord = nt.nodes.new("ShaderNodeTexCoord")
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = (1.0, 400.0, 400.0)
    noise = nt.nodes.new("ShaderNodeTexNoise")
    _set(noise, 60.0, "Scale")
    ramp = nt.nodes.new("ShaderNodeMapRange")
    _set(ramp, max(0.05, roughness - 0.12), "To Min")
    _set(ramp, min(1.0, roughness + 0.12), "To Max")
    nt.links.new(coord.outputs["Object"], mapping.inputs["Vector"])
    nt.links.new(mapping.outputs["Vector"], noise.inputs["Vector"])
    nt.links.new(noise.outputs["Fac"], ramp.inputs["Value"])
    nt.links.new(ramp.outputs["Result"], bsdf.inputs["Roughness"])
    _set(bsdf, 0.6, "Anisotropic")


# --- studio ------------------------------------------------------------------------

def _bounds(objs):
    pts = []
    for o in objs:
        n = len(o.data.vertices)
        if n:
            co = np.empty(n * 3, dtype=np.float32)
            o.data.vertices.foreach_get("co", co)
            co = co.reshape(-1, 3)
            pts.append(co.min(axis=0))
            pts.append(co.max(axis=0))
    arr = np.array(pts)
    return Vector(arr.min(axis=0)), Vector(arr.max(axis=0))


def _cyclorama(center, radius, floor_z, azimuth):
    """Seamless floor-to-wall backdrop behind the model (relative to the camera)."""
    import bmesh
    bm = bmesh.new()
    r = 3.0 * radius                                 # cove radius
    depth, height, width = 14.0 * radius, 14.0 * radius, 30.0 * radius
    profile = [(-depth, 0.0), (0.0, 0.0)]            # (y, z): floor toward the camera
    for i in range(1, 17):                           # quarter-circle cove
        a = (math.pi / 2) * i / 16
        profile.append((r * math.sin(a), r * (1 - math.cos(a))))
    profile.append((r, height))
    rows = []
    for x in (-width / 2, width / 2):
        rows.append([bm.verts.new((x, y + 2.5 * radius, z)) for y, z in profile])
    for i in range(len(profile) - 1):
        bm.faces.new((rows[0][i], rows[1][i], rows[1][i + 1], rows[0][i + 1]))
    mesh = bpy.data.meshes.new("Cyclorama")
    bm.to_mesh(mesh)
    bm.free()
    for p in mesh.polygons:
        p.use_smooth = True
    obj = bpy.data.objects.new("Cyclorama", mesh)
    # The profile's wall sits at +Y; spin it to stand opposite the camera.
    obj.rotation_euler = (0.0, 0.0, azimuth + math.pi / 2)
    obj.location = (center.x, center.y, floor_z)
    bpy.context.scene.collection.objects.link(obj)
    mat = bpy.data.materials.new("Backdrop")
    if hasattr(mat, "use_nodes") and not mat.use_nodes:
        mat.use_nodes = True
    bsdf = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    _set(bsdf, (0.62, 0.63, 0.65, 1.0), "Base Color")
    _set(bsdf, 0.7, "Roughness")
    obj.data.materials.append(mat)
    return obj


def _area_light(name, location, target, size, power, color=(1.0, 1.0, 1.0)):
    light = bpy.data.lights.new(name, "AREA")
    light.shape = "DISK"
    light.size = size
    light.energy = power
    light.color = color
    obj = bpy.data.objects.new(name, light)
    obj.location = location
    obj.rotation_euler = (target - Vector(location)).to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _camera(center, radius, view_dir, view_up, aspect):
    cam_data = bpy.data.cameras.new("Camera")
    cam_data.lens = 70.0
    cam_data.sensor_fit = "AUTO"
    # half-angle of the NARROWER field of view (AUTO fit spans the wider side), so
    # the bounding sphere fits both ways
    half = math.atan(math.tan(cam_data.angle / 2) * min(aspect, 1.0 / aspect))
    dist = radius * 1.35 / math.sin(half)            # breathing room around the part
    d = Vector(view_dir).normalized()
    cam = bpy.data.objects.new("Camera", cam_data)
    cam.location = center + d * dist
    up = Vector(view_up).normalized()
    if abs(d.dot(up)) > 0.999:
        up = Vector((0.0, 1.0, 0.0))
    # camera local X=right, Y=up, Z=toward the camera (it looks down -Z)
    right = up.cross(d).normalized()
    true_up = d.cross(right).normalized()
    cam.rotation_euler = Matrix((right, true_up, d)).transposed().to_euler()
    cam_data.clip_start = dist * 0.01
    cam_data.clip_end = dist * 20.0 + 40.0 * radius
    bpy.context.scene.collection.objects.link(cam)
    bpy.context.scene.camera = cam
    return cam, dist


def _studio(objs, job):
    lo, hi = _bounds(objs)
    center = (lo + hi) / 2
    radius = max((hi - lo).length / 2, 1e-4)
    d = Vector(job["view_dir"]).normalized()
    aspect = job["width"] / job["height"]
    _cam, dist = _camera(center, radius, d, job["view_up"], aspect)

    horizontal = Vector((d.x, d.y, 0.0))
    backdrop = d.z < 0.95 and job["view"] != "bottom"
    azimuth = math.atan2(d.y, d.x) if horizontal.length > 1e-6 else -math.pi / 2
    if backdrop:
        _cyclorama(center, radius, lo.z, azimuth)

    # Three-point rig placed relative to the camera, powers scaled with distance^2
    # so the exposure does not depend on the model's size.
    fwd = horizontal.normalized() if horizontal.length > 1e-6 else Vector((0.0, -1.0, 0.0))
    side = Vector((0.0, 0.0, 1.0)).cross(fwd).normalized()
    s = radius * 6.0
    unit = s * s                                     # W at the light's distance
    key = center + (fwd * 0.9 + side * 1.0 + Vector((0, 0, 1.3))).normalized() * s
    fill = center + (fwd * 1.0 - side * 1.2 + Vector((0, 0, 0.4))).normalized() * s
    rim = center + (-fwd * 1.2 - side * 0.4 + Vector((0, 0, 1.4))).normalized() * s
    top = center + Vector((0, 0, 1)) * s
    _area_light("Key", key, center, radius * 4.0, 6.0 * unit, (1.0, 0.97, 0.93))
    _area_light("Fill", fill, center, radius * 6.0, 1.6 * unit, (0.93, 0.96, 1.0))
    _area_light("Rim", rim, center, radius * 3.0, 5.0 * unit)
    _area_light("Top", top, center, radius * 8.0, 1.0 * unit)

    world = bpy.data.worlds.new("Studio")
    if hasattr(world, "use_nodes") and not world.use_nodes:
        world.use_nodes = True
    bg = next(n for n in world.node_tree.nodes if n.type == "BACKGROUND")
    _set(bg, (0.62, 0.63, 0.65, 1.0), "Color")
    _set(bg, 0.08, "Strength")
    bpy.context.scene.world = world
    return {"center": list(center), "radius": radius, "camera_distance": dist,
            "backdrop": backdrop}


# --- render settings ------------------------------------------------------------------

def _device(scene, want):
    """Pick the Cycles device: GPU backends in preference order, CPU fallback."""
    scene.cycles.device = "CPU"
    if want == "cpu":
        return "CPU"
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
    except KeyError:
        return "CPU"
    for kind in ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"):
        try:
            prefs.compute_device_type = kind
        except TypeError:
            continue
        prefs.get_devices()
        gpus = [dv for dv in prefs.devices if dv.type == kind]
        if gpus:
            for dv in prefs.devices:
                dv.use = dv.type == kind
            scene.cycles.device = "GPU"
            return kind
    if want == "gpu":
        print("ankusdrive: device='gpu' requested but no GPU compute device; using CPU")
    return "CPU"


def _settings(job):
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.render.resolution_x = job["width"]
    scene.render.resolution_y = job["height"]
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.film_transparent = False
    scene.unit_settings.system = "METRIC"               # 1 unit = 1 m; parts are true scale
    cy = scene.cycles
    cy.samples = job["samples"]
    cy.use_adaptive_sampling = True
    cy.max_bounces = job["max_bounces"]
    cy.transmission_bounces = job["max_bounces"]
    cy.use_denoising = False
    denoised = False
    if job["denoise"]:
        try:
            import _cycles
            if getattr(_cycles, "with_openimagedenoise", False):
                cy.use_denoising = True
                cy.denoiser = "OPENIMAGEDENOISE"
                denoised = True
        except Exception:
            pass
    try:
        scene.view_settings.view_transform = "AgX"
        scene.view_settings.look = "AgX - Medium High Contrast"
    except TypeError:
        scene.view_settings.view_transform = "Filmic"
    device = _device(scene, job["device"])
    return device, denoised


# --- main ---------------------------------------------------------------------------

def main():
    job_path = _args()[0]
    job_dir = os.path.dirname(os.path.abspath(job_path))
    with open(job_path, encoding="utf-8") as f:
        job = json.load(f)
    result = {"ok": False}
    t0 = time.time()
    try:
        _reset()
        objs, parts_out = [], []
        for part in job["parts"]:
            obj = _build_part(job_dir, part)
            obj.data.materials.append(_principled_material(part["name"], part["appearance"]))
            objs.append(obj)
            parts_out.append({"name": part["name"], "material": part["appearance"]["name"],
                              "triangles": part["triangle_count"]})
        framing = _studio(objs, job)
        device, denoised = _settings(job)
        bpy.context.scene.render.filepath = job["out_png"]
        t_render = time.time()
        bpy.ops.render.render(write_still=True)
        render_s = time.time() - t_render
        bpy.ops.wm.save_as_mainfile(filepath=job["out_blend"], compress=True,
                                    relative_remap=True)
        result = {
            "ok": True, "png_path": job["out_png"], "blend_path": job["out_blend"],
            "samples": job["samples"], "denoised": denoised, "device": device,
            "blender_version": bpy.app.version_string, "render_s": round(render_s, 2),
            "elapsed_s": round(time.time() - t0, 2), "parts": parts_out,
            "framing": framing,
        }
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}",
                  "traceback": traceback.format_exc()}
        print(result["traceback"], file=sys.stderr)
    with open(job["result"], "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    if not result["ok"]:
        sys.exit(3)


main()
