"""Blender studio-render backend contract (issue #335) — the part that needs no Blender.

``ankusdrive/blender_render.py`` is pure stdlib and FreeCAD-free, so the request
contract gates every PR on the no-FreeCAD lane:

  * **appearance** — card names and the neutral PBR dict normalise to the dict the
    scene script reads; bad cards/fields/finishes fail fast listing the valid ones.
  * **selection** — ``renderer="auto"`` picks Blender, then POV-Ray, then any add-on
    renderer; the fallback carries the Blender install suggestion.
  * **discovery + install advice** — Blender is a ``studio_render`` solver entry, so
    ``setup_status`` / ``doctor`` / ``require_solver`` hand out its install hint the
    way every solver family does; versioned install dirs are globbed newest-first.
  * **job files** — tessellation buffers round-trip byte-exactly.
  * **boundaries** — the scene script imports only what Blender ships, nothing in the
    package depends on Blender's own MCP server, and the installers pin one version.

The rendering itself is gated in tests/test_render_photoreal.py (skips without Blender).

Run:  python3 tests/test_blender_render.py
"""
import array
import ast
import json
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import blender_render as br  # noqa: E402
from ankusdrive import solvers  # noqa: E402


def _raises(fn, *needles):
    try:
        fn()
    except ValueError as e:
        for n in needles:
            assert n in str(e), f"{n!r} not in {e}"
        return str(e)
    raise AssertionError("expected ValueError")


# --- appearance ------------------------------------------------------------------

def test_cards_cover_the_render_addon_library():
    """The 14 Render add-on card names keep working as `material=` on Blender."""
    cards = {"Aluminium", "Brass", "Carpaint", "Disney", "Emission", "Glass",
             "GlossyPlastic", "Gold", "GreenMarble", "Iron", "Magnetite", "Matte",
             "RoughPlastic", "Terrazzo"}
    assert set(br.MATERIAL_PRESETS) == cards
    gold = br.resolve_appearance("Gold")
    assert gold["metallic"] == 1.0 and gold["name"] == "Gold"
    assert gold["color"][0] > gold["color"][2]


def test_pbr_dict_normalises():
    ap = br.resolve_appearance({"base": "RoughPlastic", "color": "#ff0000",
                                "finish": "fdm_layers", "layer_height_mm": 0.25})
    assert ap["name"] == "RoughPlastic+custom" and ap["finish"] == "fdm_layers"
    assert ap["color"] == [1.0, 0.0, 0.0] and ap["roughness"] == 0.55
    mid = br.resolve_appearance({"color": "#808080"})["color"][0]
    assert 0.2 < mid < 0.23, f"'#808080' is sRGB and must become linear ~0.216, got {mid}"
    assert br.resolve_appearance({"emission": [0, 0.5, 1], "emission_strength": 3})["emission"] == [0, 0.5, 1]


def test_default_and_palette():
    assert br.resolve_appearance(None)["name"] == "default"
    a, b = br.palette_appearance(0), br.palette_appearance(1)
    assert a["color"] != b["color"], "unassigned assembly parts must stay distinguishable"
    assert br.resolve_appearance(None, b)["color"] == b["color"]


def test_bad_appearance_fails_fast_with_the_valid_names():
    _raises(lambda: br.resolve_appearance("Unobtanium"), "unknown render material", "Aluminium")
    _raises(lambda: br.resolve_appearance({"colour": "#fff"}), "unknown appearance field", "color")
    _raises(lambda: br.resolve_appearance({"finish": "knurl"}), "fdm_layers", "brushed")
    _raises(lambda: br.resolve_appearance({"color": [255, 0, 0]}), "0..1")
    _raises(lambda: br.resolve_appearance({"color": "red"}), "#rrggbb")
    _raises(lambda: br.resolve_appearance({"metallic": 2}), "0..1")
    _raises(lambda: br.resolve_appearance({"roughness": True}), "wrong type")
    _raises(lambda: br.resolve_appearance(3), "card name or a dict")


def test_request_validation():
    br.validate_request("studio", "final", "gpu")
    _raises(lambda: br.validate_request("outdoor", "draft", "auto"), "studio")
    _raises(lambda: br.validate_request("studio", "ultra", "auto"), "draft", "final")
    _raises(lambda: br.validate_request("studio", "draft", "npu"), "cpu")
    assert [br.QUALITY[q]["samples"] for q in ("draft", "preview", "final")] == sorted(
        br.QUALITY[q]["samples"] for q in br.QUALITY)


# --- renderer selection -----------------------------------------------------------

def test_auto_prefers_blender_then_povray_then_any():
    c = br.choose_renderer
    assert c("auto", True, ["Povray"]) == {"renderer": "Blender", "auto": True}
    assert c("auto", False, ["Luxcore", "Povray"])["renderer"] == "Povray"
    assert c("auto", False, ["Pbrt", "Appleseed"])["renderer"] == "Appleseed"
    assert c("auto", False, []) == {"renderer": None, "auto": True}
    assert c("Luxcore", True, []) == {"renderer": "Luxcore", "auto": False}


def test_fallback_suggestion_carries_the_install_hint():
    s = br.upgrade_suggestion()
    assert s["renderer"] == "Blender" and s["install"] == solvers.install_hint("blender")
    assert "render_capabilities" in s["check"] and "studio_render" in s["check"]


# --- discovery + install advice ----------------------------------------------------

class _absent:
    """Force every resolution primitive to 'absent' (see test_solve_degradation)."""
    names = ("_module_available", "_binary_path", "_vm_binary_path", "_unwired_found")

    def __enter__(self):
        self.saved = {n: getattr(solvers, n) for n in self.names}
        solvers._module_available = lambda m: False
        solvers._binary_path = lambda name, spec: None
        solvers._vm_binary_path = lambda name, spec: None
        solvers._unwired_found = lambda name, spec: None

    def __exit__(self, *exc):
        for n, fn in self.saved.items():
            setattr(solvers, n, fn)


def test_blender_is_a_studio_render_solver():
    spec = solvers._SOLVERS["blender"]
    assert spec["family"] == "studio_render" and spec["kind"] == "binary"
    assert spec["license"] == "GPL-3.0" and spec["isolation"] == "subprocess"
    assert set(spec["dir_globs"]) == {"Linux", "Darwin", "Windows"}


def test_absent_blender_degrades_like_every_solver():
    with _absent():
        miss = br.not_installed()
        assert miss["ok"] is False and miss["status"] == "absent"
        assert miss["renderer"] == "Blender" and miss["reason"] == "Blender not installed"
        assert miss["install"] == solvers.install_hint("blender")
        caps = solvers.capabilities()
        fam = caps["families"]["studio_render"]
        assert fam["solvers"] == ["blender"] and fam["any_available"] is False


def test_install_hint_names_a_command_per_os():
    hint = solvers.install_hint("blender")
    for needle in ("scripts/install-renderers.sh blender", "brew install --cask blender",
                   "winget install BlenderFoundation.Blender",
                   "scripts/install-solvers.ps1 blender", "ANKUSDRIVE_BLENDER_PATH", "4.2"):
        assert needle in hint, f"{needle!r} missing from the Blender install hint"
    # not a pip hint, so every install kind (pipx/uvx/mcpb, #347) passes it through
    from ankusdrive import install_kind
    for kind in install_kind.KINDS:
        assert install_kind.adapt(hint, environ={install_kind.ENV: kind}) == hint


def test_env_override_resolves():
    with tempfile.TemporaryDirectory() as d:
        exe = os.path.join(d, "blender")
        Path(exe).write_text("#!/bin/sh\n", encoding="utf-8")
        old = os.environ.get("ANKUSDRIVE_BLENDER_PATH")
        os.environ["ANKUSDRIVE_BLENDER_PATH"] = exe
        try:
            info = solvers.find_solver("blender")
            assert info["available"] and info["path"] == exe, info
        finally:
            if old is None:
                os.environ.pop("ANKUSDRIVE_BLENDER_PATH")
            else:
                os.environ["ANKUSDRIVE_BLENDER_PATH"] = old


def test_versioned_dirs_glob_newest_first():
    with tempfile.TemporaryDirectory() as d:
        for v in ("blender-4.5.3", "blender-10.0.0", "blender-5.2.1"):
            os.makedirs(os.path.join(d, v))
        Path(d, "blender-9.9.9").write_text("a file, not an install", encoding="utf-8")
        got = [os.path.basename(p) for p in solvers._versioned_dirs((os.path.join(d, "blender-*"),))]
        assert got == ["blender-10.0.0", "blender-5.2.1", "blender-4.5.3"], got
        assert solvers._versioned_dirs(("%NO_SUCH_VAR_335%/Blender *",)) == [], \
            "an unexpanded %VAR% must be dropped, not globbed literally"


# --- job files ----------------------------------------------------------------------

def test_part_buffers_round_trip():
    with tempfile.TemporaryDirectory() as d:
        verts = [0.0, 0.0, 0.0, 0.001, 0.0, 0.0, 0.0, 0.002, 0.0]
        entry = br.write_part(d, 7, verts, [0, 1, 2], [0.0, 0.0, 1.0] * 3)
        assert entry == {"vertices": "p007.v", "triangles": "p007.t", "normals": "p007.n",
                         "vertex_count": 3, "triangle_count": 1}
        v = array.array("f")
        with open(os.path.join(d, "p007.v"), "rb") as f:
            v.fromfile(f, 9)
        assert [round(x, 6) for x in v] == verts
        assert os.path.getsize(os.path.join(d, "p007.t")) == 12
        job = br.build_job(d, [dict(entry, name="p", appearance=br.resolve_appearance("Gold"))],
                           view="iso", view_dir=(1, 1, 1), view_up=(0, 0, 1), width=64, height=48,
                           scene="studio", quality="final", device="auto",
                           out_png=os.path.join(d, "r.png"), out_blend=os.path.join(d, "r.blend"))
        data = json.loads(Path(job).read_text(encoding="utf-8"))
        assert data["samples"] == br.QUALITY["final"]["samples"] and data["denoise"] is True
        assert data["result"] == os.path.join(d, "result.json")
        br.discard_buffers(job)
        assert sorted(os.listdir(d)) == ["job.json"], os.listdir(d)
        cmd = br.command("/x/blender", job)
        assert cmd[:3] == ["/x/blender", "--background", "--factory-startup"]
        assert cmd[-3:] == [br.SCENE_SCRIPT, "--", job]


def test_collect_reports_the_scene_script_error():
    with tempfile.TemporaryDirectory() as d:
        job = os.path.join(d, "job.json")
        Path(d, "result.json").write_text(json.dumps({"ok": False, "error": "KeyError: 'x'"}), encoding="utf-8")
        try:
            br.collect(job, 3)
        except RuntimeError as e:
            assert "KeyError" in str(e)
        else:
            raise AssertionError("expected RuntimeError")
        os.remove(os.path.join(d, "result.json"))
        Path(d, "blender.log").write_text("line\nError: EXCEPTION_ACCESS_VIOLATION\n", encoding="utf-8")
        try:
            br.collect(job, -11)
        except RuntimeError as e:
            assert "EXCEPTION_ACCESS_VIOLATION" in str(e) and "-11" in str(e)
        else:
            raise AssertionError("expected RuntimeError")


# --- boundaries -----------------------------------------------------------------------

_BLENDER_SHIPS = {"bpy", "bmesh", "mathutils", "numpy"}


def test_scene_script_imports_only_what_blender_ships():
    tree = ast.parse(Path(br.SCENE_SCRIPT).read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module.split(".")[0])
    extra = mods - _BLENDER_SHIPS - set(sys.stdlib_module_names) - {"_cycles"}
    assert not extra, f"blender_scene.py imports modules Blender does not ship: {extra}"
    assert "ankusdrive" not in mods, "the scene script must stay standalone inside Blender"


def test_no_dependency_on_blenders_mcp_server():
    """Pairing with Blender's MCP is documentation + a .blend file only (#335)."""
    for path in (REPO / "ankusdrive").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|from)\s+blender_mcp", text, re.M), path


def test_installers_pin_the_same_blender():
    sh = (REPO / "scripts" / "install-renderers.sh").read_text(encoding="utf-8")
    ps = (REPO / "scripts" / "install-solvers.ps1").read_text(encoding="utf-8")
    sh_ver = re.search(r'^BLENDER_VERSION="([\d.]+)"', sh, re.M).group(1)
    ps_ver = re.search(r"^\$BLENDER_VER = '([\d.]+)'", ps, re.M).group(1)
    assert sh_ver == ps_ver, f"install-renderers.sh pins {sh_ver}, install-solvers.ps1 {ps_ver}"
    for sha in re.findall(r'BLENDER_SHA_\w+="(\w+)"', sh) + re.findall(r"\$BLENDER_SHA_\w+ = '(\w+)'", ps):
        assert re.fullmatch(r"[0-9a-f]{64}", sha), sha
    assert re.search(r"^\s+blender\) want_blender=1", sh, re.M), "install-renderers.sh lost its blender target"
    assert re.search(r"'blender'\s*\{", ps), "install-solvers.ps1 lost its blender target"
    # the discovery globs cover where the installers put Blender
    globs = solvers._SOLVERS["blender"]["dir_globs"]
    assert "/opt/blender-*" in globs["Linux"] and "~/.local/opt/blender-*" in globs["Linux"]
    assert "AnkusDrive\\solvers" in ps  # portable zip lands in the provisioner dir


def test_mcp_tools_expose_the_blender_arguments():
    tree = ast.parse((REPO / "ankusdrive" / "mcp_server.py").read_text(encoding="utf-8"))
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in ("render_photoreal", "render_photoreal_submit"):
        args = fns[name].args
        names = [a.arg for a in args.args]
        for want in ("handle", "parts", "appearances", "scene", "quality", "device", "output_dir"):
            assert want in names, f"{name} lacks {want}"
        defaults = dict(zip(names[-len(args.defaults):], args.defaults))
        assert defaults["renderer"].value == "auto", f"{name} default renderer is not 'auto'"
    doc = ast.get_docstring(fns["render_photoreal"])
    for needle in ("render_capabilities", "blend_path", "suggestion", "Blender's own MCP"):
        assert needle in doc, f"render_photoreal docstring lacks {needle!r}"


# --- runner -------------------------------------------------------------------------

def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failures = []
    t0 = time.time()
    for name, fn in tests:
        t = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:55s} ({time.time() - t:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:55s} ({time.time() - t:.2f}s)")
    print()
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed ({time.time() - t0:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed ({time.time() - t0:.1f}s) ==")


if __name__ == "__main__":
    main()
