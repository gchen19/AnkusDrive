"""Blender (Cycles) studio-render backend for ``render_photoreal`` — issue #335.

The Render add-on path (``worker.py`` ``_RENDERERS``) renders one handle with one
library card through each renderer's stock template. This backend runs **full
Blender headless** instead::

    blender --background --factory-startup --python ankusdrive/blender_scene.py -- job.json

The worker tessellates every part into ``job_dir`` (raw little-endian buffers, see
``write_part``), and ``blender_scene.py`` — a checked-in script, not agent-written
code — builds the studio scene, renders the PNG and saves the ``.blend`` so the
scene can be refined by hand or through Blender's own MCP server.

This module is pure stdlib and FreeCAD-free, so the request contract (appearance
schema, quality presets, renderer selection) is testable on the no-FreeCAD CI lane.
Blender itself is discovered through ``solvers.find_solver("blender")``: the same
``ANKUSDRIVE_BLENDER_PATH`` -> PATH -> per-OS install dirs resolution and the same
``install_hint`` every solver family hands out.

Blender is GPL-3.0. AnkusDrive only ever launches it as a subprocess running a
script we ship, so nothing is linked into AnkusDrive.
"""
from __future__ import annotations

import array
import json
import os
import re
import subprocess
import sys
import time

from ankusdrive import solvers

SCENE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blender_scene.py")

# Oldest Blender the scene script is written against: Principled BSDF v2 input
# names (4.0), Smooth-by-Angle-free custom normals (4.1), AgX + OIDN defaults (4.2 LTS).
MIN_VERSION = (4, 2)

SCENES = ("studio",)

# quality -> Cycles samples + denoise. Resolution stays the caller's width/height.
QUALITY = {
    "draft":   {"samples": 16,  "denoise": True, "max_bounces": 4},
    "preview": {"samples": 64,  "denoise": True, "max_bounces": 8},
    "final":   {"samples": 384, "denoise": True, "max_bounces": 12},
}

DEVICES = ("auto", "cpu", "gpu")

FINISHES = ("none", "fdm_layers", "brushed")

# The Render add-on's 14 library card names, translated to Principled BSDF inputs so
# existing `material=` calls keep working on the Blender path. Values are linear RGB.
MATERIAL_PRESETS = {
    "Aluminium":     {"color": [0.91, 0.92, 0.92], "metallic": 1.0, "roughness": 0.28},
    "Brass":         {"color": [0.89, 0.70, 0.35], "metallic": 1.0, "roughness": 0.25},
    "Carpaint":      {"color": [0.45, 0.02, 0.02], "metallic": 0.6, "roughness": 0.3,
                      "coat": 1.0},
    "Disney":        {"color": [0.80, 0.80, 0.80], "metallic": 0.0, "roughness": 0.5},
    "Emission":      {"color": [1.0, 1.0, 1.0], "roughness": 0.5,
                      "emission": [1.0, 0.95, 0.85], "emission_strength": 4.0},
    "Glass":         {"color": [1.0, 1.0, 1.0], "roughness": 0.0, "transmission": 1.0,
                      "ior": 1.5},
    "GlossyPlastic": {"color": [0.55, 0.55, 0.57], "metallic": 0.0, "roughness": 0.12},
    "Gold":          {"color": [1.0, 0.77, 0.34], "metallic": 1.0, "roughness": 0.2},
    "GreenMarble":   {"color": [0.08, 0.25, 0.14], "metallic": 0.0, "roughness": 0.15},
    "Iron":          {"color": [0.53, 0.51, 0.49], "metallic": 1.0, "roughness": 0.45},
    "Magnetite":     {"color": [0.05, 0.05, 0.05], "metallic": 0.4, "roughness": 0.5},
    "Matte":         {"color": [0.60, 0.60, 0.60], "metallic": 0.0, "roughness": 0.9},
    "RoughPlastic":  {"color": [0.55, 0.55, 0.57], "metallic": 0.0, "roughness": 0.55},
    "Terrazzo":      {"color": [0.72, 0.70, 0.66], "metallic": 0.0, "roughness": 0.4},
}

# Neutral PBR schema a caller may pass instead of (or on top of, via `base`) a card.
_PBR_FIELDS = {
    "base": str, "color": (list, tuple, str), "metallic": (int, float),
    "roughness": (int, float), "emission": (list, tuple, str),
    "emission_strength": (int, float), "transmission": (int, float),
    "ior": (int, float), "coat": (int, float), "finish": str,
    "layer_height_mm": (int, float),
}

DEFAULT_APPEARANCE = {"color": [0.62, 0.63, 0.65], "metallic": 0.0, "roughness": 0.5}

# Unassigned parts of an assembly get distinct muted tones so parts stay legible.
_ASSEMBLY_PALETTE = (
    [0.62, 0.63, 0.65], [0.16, 0.17, 0.19], [0.30, 0.42, 0.55], [0.72, 0.60, 0.42],
    [0.42, 0.50, 0.38], [0.55, 0.30, 0.28],
)

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _color(value, field: str) -> list:
    """[r, g, b] linear 0..1 from a list or a '#rrggbb' (sRGB, as colour pickers give)."""
    if isinstance(value, str):
        m = _HEX.match(value.strip())
        if not m:
            raise ValueError(f"appearance {field}={value!r}: expected '#rrggbb' or [r, g, b]")
        h = m.group(1)
        return [round(_srgb_to_linear(int(h[i:i + 2], 16) / 255.0), 4) for i in (0, 2, 4)]
    if len(value) != 3 or not all(isinstance(v, (int, float)) for v in value):
        raise ValueError(f"appearance {field}={value!r}: expected three numbers")
    if not all(0.0 <= v <= 1.0 for v in value):
        raise ValueError(f"appearance {field}={value!r}: components must be in 0..1 "
                         "(linear RGB), or pass '#rrggbb'")
    return [float(v) for v in value]


def resolve_appearance(value, fallback: dict | None = None) -> dict:
    """Normalise one part's appearance to the full PBR dict the scene script reads.

    ``value`` is None (-> ``fallback`` or the neutral default), a Render library card
    name (``"Aluminium"``), or a dict of the neutral schema — ``color``, ``metallic``,
    ``roughness``, ``emission`` (+ ``emission_strength``), ``transmission``, ``ior``,
    ``coat``, ``finish`` (``none`` | ``fdm_layers`` | ``brushed``), ``layer_height_mm``
    — optionally starting from a card via ``base``. Raises ValueError naming the valid
    cards/fields/finishes on bad input, before any tessellation or Blender launch."""
    if value is None:
        return dict(fallback or DEFAULT_APPEARANCE, name=(fallback or {}).get("name", "default"))
    if isinstance(value, str):
        value = {"base": value}
    if not isinstance(value, dict):
        raise ValueError(f"appearance must be a card name or a dict, got {type(value).__name__}")
    unknown = set(value) - set(_PBR_FIELDS)
    if unknown:
        raise ValueError(f"unknown appearance field(s) {sorted(unknown)}; "
                         f"valid: {sorted(_PBR_FIELDS)}")
    for k, v in value.items():
        if not isinstance(v, _PBR_FIELDS[k]) or isinstance(v, bool):
            raise ValueError(f"appearance {k}={v!r} has the wrong type")
    out = dict(DEFAULT_APPEARANCE)
    name = "custom"
    if "base" in value:
        card = value["base"]
        if card not in MATERIAL_PRESETS:
            raise ValueError(f"unknown render material {card!r}; available: "
                             f"{sorted(MATERIAL_PRESETS)}")
        out.update(MATERIAL_PRESETS[card])
        name = card if len(value) == 1 else f"{card}+custom"
    for k, v in value.items():
        if k == "base":
            continue
        if k in ("color", "emission"):
            out[k] = _color(v, k)
        elif k == "finish":
            if v not in FINISHES:
                raise ValueError(f"unknown finish {v!r}; valid: {list(FINISHES)}")
            out[k] = v
        else:
            v = float(v)
            if k in ("metallic", "roughness", "transmission", "coat") and not 0.0 <= v <= 1.0:
                raise ValueError(f"appearance {k}={v} must be in 0..1")
            if k in ("ior", "layer_height_mm", "emission_strength") and v < 0:
                raise ValueError(f"appearance {k}={v} must be non-negative")
            out[k] = v
    out["name"] = name
    return out


def palette_appearance(index: int) -> dict:
    """The default appearance for the ``index``-th unassigned part of an assembly."""
    return dict(DEFAULT_APPEARANCE, color=list(_ASSEMBLY_PALETTE[index % len(_ASSEMBLY_PALETTE)]),
                name=f"palette{index % len(_ASSEMBLY_PALETTE)}")


def validate_request(scene: str, quality: str, device: str) -> None:
    if scene not in SCENES:
        raise ValueError(f"unknown scene {scene!r}; valid: {list(SCENES)}")
    if quality not in QUALITY:
        raise ValueError(f"unknown quality {quality!r}; valid: {list(QUALITY)}")
    if device not in DEVICES:
        raise ValueError(f"unknown device {device!r}; valid: {list(DEVICES)}")


# --- discovery -------------------------------------------------------------------

def find() -> dict:
    """``solvers.find_solver("blender")`` — side-effect-free, carries the install hint."""
    return solvers.find_solver("blender")


def not_installed(requested: str = "Blender") -> dict:
    """The structured miss ``render_photoreal`` returns for an absent Blender: the
    ``require_solver`` shape every solver family uses, plus the renderer fields."""
    miss = solvers.require_solver("blender")
    miss.update(renderer=requested,
                reason="Blender not installed" if miss.get("status") == "absent"
                else miss.get("reason"))
    return miss


_probe_cache: dict = {}

_PROBE_EXPR = r"""
import bpy, json, sys
out = {"version": list(bpy.app.version[:3]), "version_string": bpy.app.version_string,
       "oidn": False, "gpu": []}
try:
    import _cycles
    out["oidn"] = bool(getattr(_cycles, "with_openimagedenoise", False))
except Exception:
    pass
try:
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for kind in ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"):
        try:
            prefs.compute_device_type = kind
        except TypeError:
            continue
        prefs.get_devices()
        names = [d.name for d in prefs.devices if d.type == kind]
        if names:
            out["gpu"].append({"type": kind, "devices": names})
except Exception as e:
    out["gpu_error"] = repr(e)
sys.stdout.write("ANKUSDRIVE_PROBE " + json.dumps(out) + "\n")
sys.stdout.flush()
"""


def probe(path: str, timeout: float = 60.0) -> dict:
    """Launch ``path`` once to read its version, OIDN build flag and GPU compute
    devices. Cached per (path, mtime) — this executes Blender (~1-3 s), so it backs
    ``render_capabilities`` and the version gate, never ``find_solver``. Returns
    ``{version, version_string, oidn, gpu: [{type, devices}], device, supported}`` or
    ``{error}``."""
    try:
        key = (path, os.path.getmtime(path))
    except OSError as e:
        return {"error": f"cannot stat {path}: {e}"}
    if key in _probe_cache:
        return _probe_cache[key]
    try:
        proc = subprocess.run(
            [path, "--background", "--factory-startup", "--python-expr", _PROBE_EXPR],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": f"could not run {path}: {e}"}
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("ANKUSDRIVE_PROBE ")), None)
    if line is None:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        return {"error": f"{path} did not answer the probe (exit {proc.returncode}): "
                         + " | ".join(tail)}
    info = json.loads(line.split(" ", 1)[1])
    info["supported"] = tuple(info["version"][:2]) >= MIN_VERSION
    info["device"] = info["gpu"][0]["type"] if info["gpu"] else "CPU"
    _probe_cache[key] = info
    return info


def upgrade_hint(info: dict) -> str:
    return (f"Blender {info.get('version_string')} is older than the "
            f"{MIN_VERSION[0]}.{MIN_VERSION[1]} the studio scene needs; upgrade — "
            + solvers.install_hint("blender"))


# --- renderer selection ------------------------------------------------------------

def choose_renderer(requested: str, blender_available: bool, addon_available: list) -> dict:
    """Resolve ``renderer="auto"``. Blender first (the studio backend), then POV-Ray
    (the one add-on renderer with a one-line install on every OS), then any other
    add-on renderer that resolves. Returns ``{renderer, auto}``; ``renderer`` is None
    when nothing is usable. A named renderer passes through untouched."""
    if requested != "auto":
        return {"renderer": requested, "auto": False}
    if blender_available:
        return {"renderer": "Blender", "auto": True}
    for name in ["Povray"] + sorted(n for n in addon_available if n != "Povray"):
        if name in addon_available:
            return {"renderer": name, "auto": True}
    return {"renderer": None, "auto": True}


def upgrade_suggestion() -> dict:
    """Attached to an ``auto`` result that fell back off Blender, so the agent can
    tell the user a better renderer is one install away (the solver-family pattern:
    discovery hands out the fix, it never installs anything itself)."""
    return {
        "renderer": "Blender",
        "why": "renderer='auto' fell back because Blender is not installed; Blender adds "
               "assemblies with per-part appearance, the studio scene (soft lights, "
               "cyclorama, contact shadows), OIDN denoising and a saved .blend",
        "install": solvers.install_hint("blender"),
        "check": "render_capabilities (renderers.Blender) or setup_status (studio_render)",
    }


# --- job files -----------------------------------------------------------------------

def write_part(job_dir: str, index: int, vertices, triangles, normals=None) -> dict:
    """Write one part's tessellation as raw little-endian buffers the scene script
    maps straight into ``Mesh.foreach_set``: ``pNNN.v`` float32 xyz (metres),
    ``pNNN.t`` int32 vertex indices, ``pNNN.n`` float32 per-vertex normals (optional).
    Returns the file-name fields for the job's part entry."""
    stem = f"p{index:03d}"
    entry = {"vertices": stem + ".v", "triangles": stem + ".t",
             "vertex_count": len(vertices) // 3, "triangle_count": len(triangles) // 3}
    for suffix, code, data in (("v", "f", vertices), ("t", "i", triangles), ("n", "f", normals)):
        if data is None:
            continue
        buf = array.array(code, data)
        if sys.byteorder != "little":
            buf.byteswap()
        with open(os.path.join(job_dir, f"{stem}.{suffix}"), "wb") as f:
            buf.tofile(f)
        if suffix == "n":
            entry["normals"] = stem + ".n"
    return entry


def build_job(job_dir: str, parts: list, *, view: str, view_dir, view_up, width: int,
              height: int, scene: str, quality: str, device: str, out_png: str,
              out_blend: str) -> str:
    """Write ``job.json`` for the scene script; returns its path."""
    q = QUALITY[quality]
    job = {
        "version": 1, "parts": parts, "view": view,
        "view_dir": list(view_dir), "view_up": list(view_up),
        "width": width, "height": height, "scene": scene, "quality": quality,
        "samples": q["samples"], "denoise": q["denoise"], "max_bounces": q["max_bounces"],
        "device": device, "out_png": out_png, "out_blend": out_blend,
        "result": os.path.join(job_dir, "result.json"),
    }
    path = os.path.join(job_dir, "job.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(job, f, indent=1)
    return path


def command(blender: str, job_path: str) -> list:
    return [blender, "--background", "--factory-startup", "--python-exit-code", "3",
            "--python", SCENE_SCRIPT, "--", job_path]


def launch(blender: str, job_path: str) -> subprocess.Popen:
    """Start the render; stdout+stderr go to ``blender.log`` beside the job."""
    log = open(os.path.join(os.path.dirname(job_path), "blender.log"), "wb")
    try:
        return subprocess.Popen(command(blender, job_path), stdout=log,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    finally:
        log.close()                                  # the child holds its own handle


def collect(job_path: str, returncode: int | None) -> dict:
    """The scene script's ``result.json``, or a RuntimeError carrying the log tail."""
    job_dir = os.path.dirname(job_path)
    result = os.path.join(job_dir, "result.json")
    if os.path.isfile(result):
        with open(result, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("ok") and os.path.isfile(data.get("png_path", "")):
            return data
        if data.get("error"):
            raise RuntimeError(f"Blender scene script failed: {data['error']}")
    tail = ""
    try:
        with open(os.path.join(job_dir, "blender.log"), encoding="utf-8", errors="replace") as f:
            tail = "".join(f.readlines()[-15:])
    except OSError:
        pass
    raise RuntimeError(f"Blender exited {returncode} without an image; log tail:\n{tail}")


def discard_buffers(job_path: str) -> None:
    """Delete a finished job's tessellation buffers (``pNNN.v/.t/.n``) — they can be
    tens of MB and the saved ``.blend`` already carries the meshes. The job/log/result
    files and any PNG/.blend written into the job dir are kept."""
    job_dir = os.path.dirname(job_path)
    for name in os.listdir(job_dir):
        if re.fullmatch(r"p\d{3}\.[vtn]", name):
            try:
                os.remove(os.path.join(job_dir, name))
            except OSError:
                pass


def run(blender: str, job_path: str, timeout: float) -> dict:
    """Blocking render: launch, wait (killing it past ``timeout``), collect."""
    t0 = time.time()
    proc = launch(blender, job_path)
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise RuntimeError(f"Blender render exceeded {timeout:.0f}s and was stopped; use "
                           "quality='draft' or render_photoreal_submit") from None
    data = collect(job_path, rc)
    data.setdefault("elapsed_s", round(time.time() - t0, 2))
    return data
