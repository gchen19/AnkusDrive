"""Regeneration round trip (#410, epic #309) — a recorded session, replayed, is the same part.

The epic's promise, tested end to end rather than per piece. Tolerances apply, not byte
identity. A session is recorded through the MCP tool layer exactly as an agent drives
it, exported with ``session_transcript``, run in a **fresh interpreter** (a new server
process and a new worker), and the result compared with the original.

  test_cad_fem_roundtrip
      Box, a failed fillet, an aborted transaction, a drilled hole, mass properties, an
      FEM analysis on the cut solid (material, fixed + pressure, mesh), fem_run_submit
      with job polling, fem_results by job id, save + STEP export. Exported with
      prune_aborted, so the replay allocates ``cylinder_1`` where the recording
      had ``cylinder_2``: that handle drift only replays correctly if handles are
      linked by value. Compared: volume, area and centre of mass of the saved solid
      within 1e-6, its face tags (the geometry fingerprint) exactly, and the FEM maxima
      through the script's own ``s.check`` lines. The script must contain no recording
      path, must collapse the polls to one ``s.wait``, and must drop the pruned
      read-only calls.
  test_tampered_checkpoint_fails
      Two-sided: the same script with the recorded max von Mises moved by 5 % must fail
      at that step with ReplayError. Without this, passing checks could mean they never run.
  test_run_script_honest_under_switch
      A session that used run_script exports a warning. Replayed with
      ANKUSDRIVE_ALLOW_RUN_SCRIPT=false it stops at that step naming the switch; with it on
      it regenerates.

The FEM half needs CalculiX. Without it the session is recorded CAD-only and the FEM
assertions SKIP, saying so.

Run:  .venv/bin/python3 tests/test_regeneration_roundtrip.py
"""
import asyncio
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))   # the shared _mcp_python helper
from _mcp_python import HOST_DEPS, ensure_mcp_interpreter, mcp_skip_reason  # noqa: E402

_STEEL = {"Name": "Steel", "YoungsModulus": "210000 MPa",
          "PoissonRatio": "0.30", "Density": "7900 kg/m^3"}
_REPLAY_TIMEOUT_S = 900
_state: dict = {}


def _server():
    os.environ.setdefault("ANKUSDRIVE_TOOLSETS", "all")
    from ankusdrive import journal, mcp_server
    return journal, mcp_server


def _tool(mcp_server):
    call = mcp_server.mcp._tool_manager.call_tool

    def run(_name, **args):
        return asyncio.run(call(_name, args))
    return run


def _replay(script_text, workdir, extra_env=None):
    """Write the script and run it in a new interpreter. Returns CompletedProcess."""
    script = Path(workdir) / "transcript.py"
    script.write_text(script_text, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(REPO), "ANKUSDRIVE_TOOLSETS": "all", **(extra_env or {})}
    return subprocess.run([sys.executable, str(script), str(Path(workdir) / "regen")], env=env,
                          cwd=workdir, capture_output=True, text=True, timeout=_REPLAY_TIMEOUT_S)


def _fingerprint(fcstd):
    """Volume, area, centre of mass and sorted face tags of the Cut in a saved file."""
    from ankusdrive import Worker
    with Worker() as w:
        w.call("open_document", path=str(fcstd))
        cut = next(o["name"] for o in w.call("list_objects") if o["type"] == "Part::Cut")
        h = w.call("register_handle", object=cut)["handle"]
        mp = w.call("mass_properties", handle=h)
        tags = sorted(f["tag"] for f in w.call("list_faces", handle=h))
    return {"volume": mp["volume_mm3"], "area": mp["surface_area_mm2"],
            "com": mp["center_of_mass_mm"], "tags": tags}


def _record_cad_fem(rec_dir):
    """Drive the session through the MCP tool layer. Returns (entries, facts)."""
    journal, mcp_server = _server()
    run = _tool(mcp_server)
    out = Path(rec_dir) / "out"
    out.mkdir(parents=True, exist_ok=True)
    facts = {"fem": False}
    journal.clear()
    try:
        run("new_document", name="bracket")
        box = run("add_primitive", kind="box", w=120, d=12, h=6)
        try:
            run("fillet_edges", handle=box["handle"], edges=["Edge999"], radius=1.0)
        except Exception:
            facts["failed_call"] = True
        run("transaction_open", label="try a long pin")
        run("add_primitive", kind="cylinder", r=2, h=50)
        run("transaction_abort")
        pin = run("add_primitive", kind="cylinder", r=2, h=10, placement=[100, 6, -2])
        facts["pin_handle"] = pin["handle"]
        cut = run("boolean_op", op="cut", base=box["handle"], tool=pin["handle"])
        facts["mass"] = run("mass_properties", handle=cut["handle"])
        run("list_faces", handle=cut["handle"])

        fixed = run("query_faces", handle=cut["handle"],
                    predicate={"type": "planar", "normal_dir": [-1, 0, 0]})
        top = run("query_faces", handle=cut["handle"],
                  predicate={"type": "planar", "normal_dir": [0, 0, 1]})
        an = run("fem_new_analysis", name="A")["handle"]
        run("fem_set_solver", analysis=an, kind="ccx",
            tunables={"GeometricalNonlinearity": "linear", "MatrixSolverType": "default",
                      "IterationsControlParameterTimeUse": False})
        run("fem_set_material", analysis=an, body=cut["handle"], material=dict(_STEEL))
        run("fem_add_constraint", analysis=an, kind="fixed",
            refs=[{"handle": cut["handle"], "tag": fixed[0]["tag"]}])
        run("fem_add_constraint", analysis=an, kind="pressure",
            refs=[{"handle": cut["handle"], "tag": top[0]["tag"]}], pressure=1.0)
        run("fem_mesh", analysis=an, body=cut["handle"], char_length=4.0)
        job = run("fem_run_submit", analysis=an)
        if job.get("ok") is False:
            facts["fem_skip"] = job.get("reason") or job.get("status")
        else:
            t0 = time.monotonic()
            while run("job_status", job_id=job["job_id"])["status"] == "running":
                if time.monotonic() - t0 > 600:
                    raise AssertionError("recorded FEM solve did not finish")
                time.sleep(0.2)
            done = run("job_result", job_id=job["job_id"])
            assert done["status"] == "done", done
            facts["fem_results"] = run("fem_results", job_id=job["job_id"])
            facts["fem"] = True
        run("save_document", path=str(out / "bracket.FCStd"))
        # object= explicitly: name the part the fingerprint checks rather than rely on the default
        cut_name = next(o["name"] for o in run("list_objects") if o["type"] == "Part::Cut")
        run("export_shape", path=str(out / "bracket.step"), object=cut_name)
        transcript = run("session_transcript", prune_aborted=True)
        entries = journal.snapshot("default")["entries"]
    finally:
        mcp_server._cleanup()
    return transcript, entries, facts


def _recorded():
    """Record once for the CAD+FEM tests (the solve is the slow part)."""
    if "cad_fem" not in _state:
        rec_dir = tempfile.mkdtemp(prefix="ankusdrive_roundtrip_rec_")
        transcript, entries, facts = _record_cad_fem(rec_dir)
        _state["cad_fem"] = (rec_dir, transcript, entries, facts)
    return _state["cad_fem"]


# --- tests --------------------------------------------------------------------------

def test_cad_fem_roundtrip():
    rec_dir, t, entries, facts = _recorded()
    script = t["script"]
    assert facts.get("failed_call"), "the session was meant to contain a failed call"
    assert facts["pin_handle"] == "cylinder_2", f"the aborted cylinder should have used cylinder_1: {facts}"
    assert rec_dir not in script and os.path.expanduser("~") not in script, "recording paths leaked"
    assert "# FAILED in the session, not replayed" in script
    assert "cylinder_2 = r" in script and "tool=cylinder_2" in script, "handle not linked by value"
    assert "r=2.0, h=50" not in script and "h=50.0" not in script, "the aborted cylinder was replayed"
    assert t["skipped"].get("read-only", 0) >= 1 and "s.list_faces(" not in script, t["skipped"]
    if facts["fem"]:
        assert script.count("s.wait(") == 1 and "job_status" not in script, "job polls not collapsed"
        assert "'max_vonmises_mpa'" in script and ", rel=0.001)" in script, "FEM maxima unchecked"
    else:
        print(f"    SKIP FEM assertions — CalculiX unavailable: {facts.get('fem_skip')}")

    with tempfile.TemporaryDirectory(prefix="ankusdrive_roundtrip_replay_") as work:
        p = _replay(script, work)
        assert p.returncode == 0, f"replay failed:\n{p.stdout}\n{p.stderr}\n--- script ---\n{script}"
        regen = Path(work) / "regen" / "bracket.FCStd"
        assert regen.exists() and (Path(work) / "regen" / "bracket.step").exists(), \
            sorted(str(x) for x in (Path(work) / "regen").rglob("*"))
        got, want = _fingerprint(regen), _fingerprint(Path(rec_dir) / "out" / "bracket.FCStd")
    for k in ("volume", "area"):
        assert abs(got[k] - want[k]) <= 1e-6 * abs(want[k]), (k, got[k], want[k])
    assert all(abs(a - b) <= 1e-6 for a, b in zip(got["com"], want["com"])), (got["com"], want["com"])
    assert got["tags"] == want["tags"], "face tags differ: the regenerated solid is not the same shape"
    assert abs(want["volume"] - facts["mass"]["volume_mm3"]) <= 1e-6 * want["volume"]


def test_tampered_checkpoint_fails():
    _rec, t, _entries, facts = _recorded()
    if not facts["fem"]:
        print(f"    SKIP — CalculiX unavailable: {facts.get('fem_skip')}")
        return
    want = facts["fem_results"]["max_vonmises_mpa"]
    pattern = re.compile(r"(s\.check\((r\d+), 'max_vonmises_mpa', )([^,]+)(, rel=0\.001\))")
    m = pattern.search(t["script"])
    assert m and abs(float(m.group(3)) - want) <= 1e-12 * max(1.0, abs(want)), (m and m.group(0), want)
    tampered = pattern.sub(lambda mm: f"{mm.group(1)}{want * 1.05!r}{mm.group(4)}", t["script"], count=1)
    step = re.findall(r"s\.step = (\d+)", t["script"][:m.start()])[-1]
    with tempfile.TemporaryDirectory(prefix="ankusdrive_roundtrip_tamper_") as work:
        p = _replay(tampered, work)
    assert p.returncode != 0, "a checkpoint 5% off the recording passed — checks are not enforced"
    assert "ReplayError" in p.stderr and f"step {step}: max_vonmises_mpa" in p.stderr, p.stderr[-2000:]


def test_run_script_honest_under_switch():
    journal, mcp_server = _server()
    if "run_script" not in mcp_server.mcp._tool_manager._tools:
        print("    SKIP — run_script is disabled in this test environment, so no session can use it")
        return
    run = _tool(mcp_server)
    journal.clear()
    try:
        run("new_document", name="scripted")
        made = run("run_script", code="import Part\nobj = App.ActiveDocument.addObject('Part::Box', 'Block')\n"
                                      "obj.Length = 7\nApp.ActiveDocument.recompute()\n__result__ = obj.Name")
        handle = made["registered"][0]["handle"]
        run("mass_properties", handle=handle)
        t = run("session_transcript")
    finally:
        mcp_server._cleanup()
    assert any("ANKUSDRIVE_ALLOW_RUN_SCRIPT" in w for w in t["warnings"]), t["warnings"]
    assert "Read it before running this script" in t["script"], t["script"]
    with tempfile.TemporaryDirectory(prefix="ankusdrive_roundtrip_rs_") as work:
        off = _replay(t["script"], work, {"ANKUSDRIVE_ALLOW_RUN_SCRIPT": "false"})
        assert off.returncode != 0 and "run_script could not run in this install" in off.stderr, \
            f"{off.stdout}\n{off.stderr}"
        on = _replay(t["script"], work, {"ANKUSDRIVE_ALLOW_RUN_SCRIPT": "true"})
        assert on.returncode == 0, f"{on.stdout}\n{on.stderr}\n--- script ---\n{t['script']}"


def _prereqs_missing():
    if importlib.util.find_spec("mcp") is None:
        return f"{mcp_skip_reason()}"
    try:
        from ankusdrive import client
        path = client._resolve_freecadcmd()
    except Exception as e:
        return f"freecadcmd not resolvable ({e})"
    if not (path and Path(path).exists()):
        return "freecadcmd not resolvable"
    return None


def main():
    # An interpreter with `mcp` (and the rest of the host deps), found by probing —
    # not assumed to be the one run_all.sh started (#449). Re-execs, or returns.
    ensure_mcp_interpreter(__file__, needs=HOST_DEPS)
    missing = _prereqs_missing()
    if missing:
        print(f"  SKIP regeneration round trip — {missing}")
        print("\n== 0/0 passed (skipped) ==")
        return
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    tests.sort(key=lambda nf: nf[0] != "test_cad_fem_roundtrip")   # records first
    failures = []
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:44s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {str(e)[:300]}")
        else:
            print(f"  PASS {name:44s} ({time.time() - t0:.2f}s)")
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
