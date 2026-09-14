"""Reviewer scenarios (#368) — the three example prompts in docs/REVIEWER_GUIDE.md, proven.

Anthropic's Software Directory Policy asks for "at least three working examples of
prompts or use cases demonstrating core functionality". The guide gives reviewers
natural-language prompts; Claude chooses the tools. This file drives the tool sequence
a correct run uses, over real stdio against the real server and FreeCAD, in exactly
the configuration a reviewer gets from the Claude Desktop extension — the bundle's
default families (``core,drawings,fem``) and ``run_script`` off — and asserts the
numbers the guide quotes. If a tool changes shape or a result drifts, this fails
before the guide becomes wrong.

  1. **Plate** — 60 × 40 × 10 mm block, four Ø5.5 mm (M5 clearance) through-holes 5 mm
     in from each corner: one watertight solid, volume 23,049.7 mm³, 62.2 g in aluminum
     (2.70 g/cm³).
  2. **Cantilever FEM** — 100 × 10 × 10 mm steel beam (E = 210 GPa) fixed at one end,
     100 N transverse tip load, 2nd-order tets: max displacement and von Mises stress
     within a few percent of Euler–Bernoulli beam theory (0.1905 mm, 60 MPa).
  3. **Drawing** — front/top/right views of the plate with overall dimensions and a
     title block, exported to DXF; the manufacturability gate reports that the holes'
     size and position are not yet dimensioned (it must NOT pass a drawing that cannot
     make the part).

Needs the ``mcp`` package and a resolvable FreeCAD; SKIPs, saying which, otherwise.

Run:  .venv/bin/python3 tests/test_reviewer_scenarios.py
"""
import asyncio
import importlib.util
import json
import math
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# The numbers docs/REVIEWER_GUIDE.md quotes. Change them together.
PLATE_VOLUME_MM3 = 60 * 40 * 10 - 4 * math.pi * 2.75 ** 2 * 10          # 23,049.67
ALUMINUM_KG_PER_MM3 = 2.70e-6
PLATE_MASS_G = PLATE_VOLUME_MM3 * ALUMINUM_KG_PER_MM3 * 1000              # 62.23
E_MPA, L_MM, B_MM, H_MM, F_N = 210_000.0, 100.0, 10.0, 10.0, 100.0
I_MM4 = B_MM * H_MM ** 3 / 12
BEAM_DELTA_MM = F_N * L_MM ** 3 / (3 * E_MPA * I_MM4)                     # 0.1905
BEAM_SIGMA_MPA = F_N * L_MM * (H_MM / 2) / I_MM4                          # 60.0

BUNDLE_ENV = {"ANKUSDRIVE_TOOLSETS": "core,drawings,fem", "ANKUSDRIVE_ALLOW_RUN_SCRIPT": "false"}


class Session:
    """A tiny MCP client over stdio that parses every content block of a result."""

    def __init__(self, s):
        self.s = s

    async def call(self, _tool_name, **args):
        # Not `tool`: boolean_op takes a `tool` argument of its own.
        res = await self.s.call_tool(_tool_name, args)
        parsed = []
        for c in res.content:
            try:
                parsed.append(json.loads(c.text))
            except Exception:
                parsed.append(c.text)
        out = parsed[0] if len(parsed) == 1 else parsed
        if res.isError:
            raise AssertionError(f"{_tool_name} returned an error: {str(out)[:300]}")
        return out


async def _with_server(body):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable, args=["-m", "ankusdrive", "mcp"],
                                   env={**os.environ, **BUNDLE_ENV}, cwd=str(REPO))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            assert "run_script" not in names, "reviewer configuration must not serve run_script"
            return await body(Session(s), names)


async def _build_plate(c):
    await c.call("new_document", name="MountingPlate")
    cur = (await c.call("add_primitive", kind="box", w=60, d=40, h=10, name="Plate"))["handle"]
    for i, (x, y) in enumerate([(5, 5), (55, 5), (5, 35), (55, 35)]):
        cyl = await c.call("add_primitive", kind="cylinder", r=2.75, h=10, placement=[x, y, 0], name=f"Hole{i + 1}")
        cur = (await c.call("boolean_op", op="cut", base=cur, tool=cyl["handle"]))["handle"]
    return cur


def test_1_plate_volume_and_mass():
    async def body(c, names):
        plate = await _build_plate(c)
        shape = await c.call("check_shape", handle=plate)
        assert shape["watertight_solid"] is True and shape["solids"] == 1, shape
        mp = await c.call("mass_properties", handle=plate, density=ALUMINUM_KG_PER_MM3)
        assert abs(mp["volume_mm3"] - PLATE_VOLUME_MM3) < 0.5, (mp["volume_mm3"], PLATE_VOLUME_MM3)
        assert abs(mp["mass_kg"] * 1000 - PLATE_MASS_G) < 0.05, (mp["mass_kg"], PLATE_MASS_G)
        print(f"    plate: {mp['volume_mm3']:.1f} mm3, {mp['mass_kg'] * 1000:.1f} g, watertight")
    asyncio.run(_with_server(body))


def test_2_cantilever_matches_beam_theory():
    async def body(c, names):
        await c.call("new_document", name="Cantilever")
        beam = (await c.call("add_primitive", kind="box", w=L_MM, d=B_MM, h=H_MM, name="Beam"))["handle"]
        root = await c.call("query_faces", handle=beam, predicate={"type": "planar", "normal_dir": [-1, 0, 0]})
        tip = await c.call("query_faces", handle=beam, predicate={"type": "planar", "normal_dir": [1, 0, 0]})
        root, tip = (root[0] if isinstance(root, list) else root), (tip[0] if isinstance(tip, list) else tip)
        edges = await c.call("list_edges", handle=beam)
        vertical = [e for e in edges if e["kind"] == "line" and abs(e["length"] - H_MM) < 1e-6
                    and abs(e["centroid"][0] - L_MM) < 1e-6 and abs(e["centroid"][2] - H_MM / 2) < 1e-6]
        assert vertical, "no vertical edge at the tip to give the load a direction"
        an = (await c.call("fem_new_analysis", name="CantileverAnalysis"))["handle"]
        await c.call("fem_set_solver", analysis=an, kind="ccx")
        await c.call("fem_set_material", analysis=an, body=beam, material={
            "YoungsModulus": f"{E_MPA:.0f} MPa", "PoissonRatio": "0.30", "Density": "7850 kg/m^3", "Name": "Steel"})
        await c.call("fem_add_constraint", analysis=an, kind="fixed", refs=[{"handle": beam, "tag": root["tag"]}])
        await c.call("fem_add_constraint", analysis=an, kind="force", refs=[{"handle": beam, "tag": tip["tag"]}],
                     force=F_N, direction={"handle": beam, "edge": vertical[0]["tag"]})
        await c.call("fem_mesh", analysis=an, body=beam, char_length=5, element_order="2nd")
        run = await c.call("fem_run", analysis=an)
        assert run["status"] == "ok", run
        res = await c.call("fem_results", analysis=an)
        d, s = res["max_displacement_mm"], res["max_vonmises_mpa"]
        print(f"    cantilever: {d:.4f} mm (theory {BEAM_DELTA_MM:.4f}), {s:.1f} MPa (theory {BEAM_SIGMA_MPA:.1f})")
        assert abs(d - BEAM_DELTA_MM) / BEAM_DELTA_MM < 0.02, (d, BEAM_DELTA_MM)
        assert abs(s - BEAM_SIGMA_MPA) / BEAM_SIGMA_MPA < 0.05, (s, BEAM_SIGMA_MPA)
    asyncio.run(_with_server(body))


def test_3_drawing_exports_and_the_gate_is_honest():
    async def body(c, names):
        plate = await _build_plate(c)
        page = (await c.call("make_drawing_page", name="PlateDrawing"))["handle"]
        group = await c.call("add_projection_group", page=page, body=plate, views=["Front", "Top", "Right"])
        assert group["views"] == ["Front", "Top", "Right"], group
        dims = await c.call("add_dimension", page=page, auto=True)
        assert len(dims["dimensions"]) >= 3, dims
        await c.call("set_title_block", page=page, part="Mounting plate", material="Aluminum 6061", rev="A")
        out = Path(tempfile.mkdtemp(prefix="ankusdrive_reviewer_")) / "mounting_plate.dxf"
        ex = await c.call("export_drawing", page=page, path=str(out))
        assert ex["format"] == "dxf" and ex["views"] == 3 and out.is_file() and out.stat().st_size > 1000, ex
        gate = await c.call("drawing_gate", page=page)
        missing = [v for v in gate["violations"] if v["code"] == "under"]
        assert gate["ok"] is False and missing, "the gate passed a drawing whose holes are undimensioned"
        assert any(v.get("axis") == "dia" for v in missing), "the gate did not name the missing hole size"
        print(f"    drawing: DXF {out.stat().st_size} B, 3 views; gate lists {len(missing)} missing dimension(s)")
    asyncio.run(_with_server(body))


def _freecad_available() -> bool:
    try:
        from ankusdrive import client
        return Path(client._resolve_freecadcmd()).exists()
    except Exception:
        return False


def main():
    if importlib.util.find_spec("mcp") is None:
        print(f"  SKIP test_reviewer_scenarios — `mcp` not importable in {sys.executable}")
        return
    if not _freecad_available():
        print("  SKIP test_reviewer_scenarios — freecadcmd not resolvable")
        return
    g = globals()
    tests = [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]
    failures = []
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:48s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:48s} ({time.time() - t0:.2f}s)")
    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()
