"""Replay (#408, epic #309) — a session journal becomes a script that regenerates it.

``ankusdrive/replay.py`` has the exporter (journal entries -> Python) and ``Session``
(what that Python runs). Two tiers, each skipping (and saying so) when its
prerequisite is missing:

  * **static** (stdlib, fast lane) — the exporter on synthetic journals: handles become
    variables (and rebind after a restart), echoes bind nothing, read-only calls are
    pruned unless used or checkable, job polls collapse to ``s.wait`` (and an unfetched
    job is still waited for), aborted transactions prune on request, defaults are
    omitted, paths carry no machine layout, failures and ``run_script`` are stated, the
    empty journal is a valid script, and every script compiles. ``Session`` against a
    stub server: only known tools, refusals raise, ``wait`` / ``check``.
  * **worker** (needs ``mcp`` + FreeCAD) — record a real session through the MCP tool
    layer, export it, run the script in a fresh interpreter, and compare what it saved.

Run:  .venv/bin/python3 tests/test_replay.py
"""
import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "ankusdrive"
sys.path.insert(0, str(REPO))


def _replay():
    spec = importlib.util.spec_from_file_location("replay_under_test", PKG / "replay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _replay()


def E(seq, tool, args=None, result=None, ok=True, pid=10, error=None, txn=0):
    e = {"seq": seq, "tool": tool, "args": args or {}, "ok": ok, "workspace": "default",
         "worker_pid": pid, "txn_depth": txn, "elapsed_s": 0.0}
    if ok:
        e["result"] = result if result is not None else {}
    else:
        e["error"] = error or "RuntimeError: boom"
    return e


def _body(out):
    compile(out["script"], "<transcript>", "exec")
    return out["script"].split("with Session() as s:", 1)[1]


# --- static: exporter ----------------------------------------------------------------

def test_handles_become_variables():
    out = R.export([
        E(1, "add_primitive", {"kind": "box", "w": 40.0}, {"handle": "box_1", "name": "Box"}),
        E(2, "add_primitive", {"kind": "cylinder"}, {"handle": "cyl_1"}),
        E(3, "boolean_op", {"op": "cut", "a": "box_1", "b": "cyl_1"}, {"handle": "cut_1"}),
    ])
    body = _body(out)
    assert "box_1 = r1['handle']" in body and "cyl_1 = r2['handle']" in body, body
    assert "a=box_1" in body and "b=cyl_1" in body and "'box_1'" not in body, body
    assert "cut_1 =" not in body, "an unused handle should not get a variable"


def test_handles_rebind_after_restart():
    body = _body(R.export([
        E(1, "add_primitive", {"kind": "box"}, {"handle": "box_1"}),
        E(2, "fillet_edges", {"handle": "box_1", "edges": ["Edge1"], "radius": 2.0}, {"handle": "fillet_1"}),
        E(3, "restart_worker", {}, {"restarted": True}, pid=11),
        E(4, "add_primitive", {"kind": "cylinder"}, {"handle": "box_1"}, pid=11),
        E(5, "fillet_edges", {"handle": "box_1", "edges": ["Edge2"], "radius": 2.0}, pid=11),
    ]))
    assert body.count("box_1 = r") == 2, body
    assert body.index("s.restart_worker()") < body.index("box_1 = r4"), body
    assert "s.restart_worker()  # " not in body and "was replaced here" not in body, \
        "an explicit restart_worker must not be doubled"


def test_echo_binds_nothing():
    body = _body(R.export([
        E(1, "new_document", {"name": "part"}, {"doc": "part"}),
        E(2, "set_active_document", {"doc": "part"}, {"doc": "part"}),
    ]))
    assert "doc_part" not in body and "doc='part'" in body, body


def test_list_handles_and_registered():
    body = _body(R.export([
        E(1, "linear_pattern", {"handle": "pad_1"}, {"registered": [{"handle": "pattern_1"}],
                                                   "child_handles": ["x_1", "x_2"]}),
        E(2, "mass_properties", {"handle": "x_2"}, {"volume": 1.0}),
        E(3, "fillet_edges", {"handle": "pattern_1"}),
    ]))
    assert "pattern_1 = r1['registered'][0]['handle']" in body, body
    assert "x_2 = r1['child_handles'][1]" in body and "handle=x_2" in body, body


def test_read_only_pruned_unless_used_or_checkable():
    out = R.export([
        E(1, "add_primitive", {"kind": "box"}, {"handle": "box_1"}),
        E(2, "list_faces", {"handle": "box_1"}, [{"name": "Face1"}]),
        E(3, "render_view", {"handle": "box_1"}, {"png_base64": {"__elided__": "str"}}),
        E(4, "mass_properties", {"handle": "box_1"}, {"volume_mm3": 1000.0, "surface_area_mm2": 600.0,
                                                      "center_of_mass_mm": [0, 0, 0], "density_kg_per_mm3": 1.0}),
        E(5, "get_object", {"name": "Box"}, {"handle": "box_7"}),
        E(6, "set_visibility", {"handle": "box_7", "visible": False}),
    ])
    body = _body(out)
    reasons = {s["tool"]: s["reason"] for s in out["skipped"]}
    assert reasons.get("list_faces") == "read-only" and reasons.get("render_view") == "read-only", out["skipped"]
    assert "s.check(r4, 'volume_mm3', 1000.0)" in body and "s.check(r4, 'surface_area_mm2', 600.0)" in body, body
    assert "density" not in body.split("r4 =")[1], "only measured quantities become checks"
    assert "box_7 = r5['handle']" in body, "a read-only call whose value is used must be kept"
    everything = _body(R.export([E(1, "list_faces", {"handle": "h_1"}, [])], include_read_only=True))
    assert "s.list_faces(" in everything
    no_checks = R.export([E(1, "mass_properties", {"handle": "h_1"}, {"volume": 1.0})], checkpoints=False)
    assert "s.mass_properties" not in _body(no_checks)


def test_job_polls_collapse():
    out = R.export([
        E(1, "fem_run_submit", {"analysis": "analysis_1"}, {"job_id": "job_1", "status": "running"}),
        E(2, "job_status", {"job_id": "job_1"}, {"status": "running"}),
        E(3, "job_result", {"job_id": "job_1"}, {"job_id": "job_1", "status": "running"}),
        E(4, "job_list", {}, {"count": 1}),
        E(5, "job_result", {"job_id": "job_1"}, {"job_id": "job_1", "status": "done",
                                                 "result": {"max_von_mises": 21.5, "nodes": 900}}),
        E(6, "job_result", {"job_id": "job_1"}, {"job_id": "job_1", "status": "done"}),
        E(7, "render_photoreal_submit", {"handle": "box_1"}, {"job_id": "render_job_1"}),
    ])
    body = _body(out)
    assert body.count("s.wait(job_1)") == 1 and "r5 = s.wait(job_1)" in body, body
    assert "s.check(r5, ('result', 'max_von_mises'), 21.5, rel=0.001)" in body, body
    assert "job_status" not in body and "job_list" not in body and "job_result" not in body, body
    assert "s.wait(render_job_1)  # submitted but never fetched" in body, body
    assert out["exported"] == 3, out


def test_solver_reads_get_solver_tolerance():
    body = _body(R.export([
        E(1, "fem_results", {"job_id": "job_1"}, {"max_vonmises_mpa": 3.25, "max_displacement_mm": 0.01}),
        E(2, "fem_result_probe", {"analysis": "analysis_1"}, {"max_vonmises_mpa": 1.5}),
        E(3, "mass_properties", {"handle": "box_1"}, {"volume_mm3": 8.0}),
    ]))
    assert "s.check(r1, 'max_vonmises_mpa', 3.25, rel=0.001)" in body, body
    assert "s.check(r2, 'max_vonmises_mpa', 1.5, rel=0.001)" in body, body
    assert "s.check(r3, 'volume_mm3', 8.0)" in body, "geometry keeps the tight tolerance"


def test_prune_aborted():
    entries = [
        E(1, "add_primitive", {"kind": "box"}, {"handle": "box_1"}),
        E(2, "transaction_open", {"label": "try"}),
        E(3, "fillet_edges", {"handle": "box_1", "radius": 9.0}, {"handle": "fillet_1"}, txn=1),
        E(4, "transaction_abort", {}, txn=1),
        E(5, "transaction_open", {"label": "keep"}),
        E(6, "chamfer_edges", {"handle": "box_1"}, {"handle": "chamfer_1"}, txn=1),
        E(7, "transaction_commit", {}, txn=1),
    ]
    faithful = _body(R.export(entries))
    assert "radius=9.0" in faithful and "s.transaction_abort()" in faithful
    pruned = R.export(entries, prune_aborted=True)
    body = _body(pruned)
    assert "radius=9.0" not in body and "transaction_abort" not in body and "label='try'" not in body, body
    assert "s.chamfer_edges(" in body and "s.transaction_commit()" in body, body
    assert {s["seq"] for s in pruned["skipped"] if s["reason"] == "inside an aborted transaction"} == {2, 3, 4}


def test_defaults_omitted_in_signature_order():
    d = R.tool_defaults()
    assert "add_primitive" in d and d["save_document"]["defaults"]["visibility_hygiene"] is True, d.get("save_document")
    body = _body(R.export([E(1, "save_document", {"visibility_hygiene": True, "path": "/w/p/a.FCStd"})]))
    assert "visibility_hygiene" not in body, body
    body = _body(R.export([E(1, "save_document", {"visibility_hygiene": False, "path": "/w/p/a.FCStd"})]))
    assert body.index("path=") < body.index("visibility_hygiene=False"), body


def test_every_tool_has_a_signature():
    src = (PKG / "mcp_server.py").read_text(encoding="utf-8")
    tools = {n.name for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and any(
        isinstance(x, ast.Call) and getattr(x.func, "attr", None) == "tool" for x in n.decorator_list)}
    assert tools and tools == set(R.tool_defaults()), set(tools) ^ set(R.tool_defaults())


def test_paths_redacted():
    home = "/Users/you"
    out = R.export([
        E(1, "open_document", {"path": f"{home}/proj/in/base.FCStd"}, {"doc": "base"}),
        E(2, "export_shape", {"path": f"{home}/proj/out/base.step"}),
        E(3, "save_document", {"path": f"{home}/proj/out/base.FCStd"}),
        E(4, "release_package", {"item": "P-1", "registry": f"{home}/proj/items.json",
                                 "out_dir": f"{home}/proj/out"}),
    ])
    body = _body(out)
    assert "/Users" not in out["script"], out["script"]
    assert "str(WORKDIR / 'out' / 'base.step')" in body and "out_dir=str(WORKDIR / 'out')" in body, body
    assert out["prerequisites"] == ["in/base.FCStd", "items.json"], out["prerequisites"]
    assert "(WORKDIR / 'out').mkdir(" in out["script"]


def test_a_prepared_solver_case_is_a_prerequisite():
    """A `case_dir` / `sif` / `stl_path` is a deck the replay needs BEFORE it runs, not
    somewhere the call writes. Treating it as an output pointed the script at a
    directory that does not exist and said nothing about it (#437)."""
    out = R.export([
        E(1, "thermal_transient_submit", {"case_dir": "/Users/you/decks/slab",
                                          "sif": "/Users/you/decks/slab/case.sif"},
          {"job_id": "job_1", "status": "running"}),
        E(2, "slice_gcode_submit", {"stl_path": "/Users/you/decks/part.stl"},
          {"job_id": "job_2", "status": "running"}),
    ])
    assert sorted(out["prerequisites"]) == ["part.stl", "slab", "slab/case.sif"], \
        out["prerequisites"]
    assert "/Users" not in out["script"], out["script"]
    assert not any("re-point" in w for w in out["warnings"]), out["warnings"]
    # an output directory is still an output
    out = R.export([E(1, "release_package", {"item": "P-1", "out_dir": "/Users/you/p/out"})])
    assert out["prerequisites"] == [], out["prerequisites"]


def test_a_case_dir_the_session_produced_is_a_warning_not_a_prerequisite():
    """The coupled hand-off (#116): a fill solve reports where it worked and a warpage
    solve is pointed at it. Nothing supplies that directory — a replay regenerates the
    case in a fresh temp dir — so it is not a file to copy in, and the script says so
    instead of listing a prerequisite nobody can satisfy."""
    out = R.export([
        E(1, "molding_fill_submit", {"body": "pad_1", "stages": "fill_pack"},
          {"job_id": "job_1", "status": "running"}),
        E(2, "job_result", {"job_id": "job_1"},
          {"job_id": "job_1", "status": "done",
           "result": {"ok": True, "solver": "openfoam", "case_dir": "/tmp/foam_mold_abc"}}),
        E(3, "molding_warpage_submit", {"body": "pad_1", "cooling_case_dir": "/tmp/foam_mold_abc"},
          {"job_id": "job_2", "status": "running"}),
    ])
    assert out["prerequisites"] == [], out["prerequisites"]
    warned = [w for w in out["warnings"] if "re-point" in w]
    assert len(warned) == 1 and "step 3" in warned[0] and "step 2 produced" in warned[0], \
        out["warnings"]
    assert warned[0] in out["script"], "the warning belongs in the script's header too"


def test_a_path_the_script_itself_writes_is_neither():
    """Save to a path, reopen it: the script writes it before it reads it, so it is not
    a prerequisite — and not the #437 hand-off warning either, even though an earlier
    result echoed the path back."""
    out = R.export([
        E(1, "add_primitive", {"kind": "box"}, {"handle": "box_1"}),
        E(2, "save_document", {"path": "/Users/you/p/out/a.FCStd"},
          {"saved": "/Users/you/p/out/a.FCStd"}),
        E(3, "open_document", {"path": "/Users/you/p/out/a.FCStd"}, {"doc": "a"}),
    ])
    assert out["prerequisites"] == [], out["prerequisites"]
    assert not any("re-point" in w for w in out["warnings"]), out["warnings"]


def test_paths_without_common_root_keep_distinct_dirs():
    out = R.export([
        E(1, "export_shape", {"path": "/tmp/a/part.step"}),
        E(2, "export_shape", {"path": "/Users/user/b/part.step"}),
    ])
    assert "/Users" not in out["script"] and "/tmp" not in out["script"], out["script"]
    body = _body(out)
    assert "WORKDIR / 'dir1' / 'part.step'" in body and "WORKDIR / 'dir2' / 'part.step'" in body, body
    win = R.export([E(1, "export_shape", {"path": r"C:\Users\username\proj\x.step"})])
    assert "Users" not in win["script"] and "WORKDIR / 'x.step'" in _body(win), win["script"]
    given = R.export([E(1, "export_shape", {"path": "/srv/job/out/x.step"})], workdir="/srv/job")
    assert "WORKDIR / 'out' / 'x.step'" in _body(given)


def test_path_mapping_is_host_independent():
    win = R.export([
        E(1, "export_shape", {"path": r"C:\Users\username\proj\out\a.step"}),
        E(2, "save_document", {"path": r"c:\users\username\proj\a.FCStd"}),
    ])
    body = _body(win)
    assert "WORKDIR / 'out' / 'a.step'" in body and "str(WORKDIR / 'a.FCStd')" in body, body
    home = _body(R.export([E(1, "export_shape", {"path": "~/proj/out/a.step"}),
                           E(2, "export_shape", {"path": "~/proj/b.step"})]))
    assert "WORKDIR / 'out' / 'a.step'" in home and "WORKDIR / 'b.step'" in home, home
    src = (PKG / "replay.py").read_text(encoding="utf-8")
    cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == "_Paths")
    used = [ast.unparse(n) for n in ast.walk(cls) if isinstance(n, ast.Attribute) and ast.unparse(n).startswith("os.")]
    assert not used, f"_Paths uses {used}: the exporting host's os.path would reinterpret recorded paths"


def test_failures_and_run_script_are_stated():
    out = R.export([
        E(1, "add_primitive", {"kind": "box"}, {"handle": "box_1"}),
        E(2, "fillet_edges", {"handle": "box_1", "edges": ["Edge99"]}, ok=False, error="RuntimeError: no Edge99"),
        E(3, "run_script", {"code": "x = '''a'''\n__result__ = 1", "auto_register": True}, {"result": 1}),
    ], truncated=True)
    body = _body(out)
    assert "# FAILED in the session, not replayed: RuntimeError: no Edge99" in body, body
    assert "# s.fillet_edges(handle=box_1, edges=['Edge99'])" in body, body
    assert "s.run_script(code=\"x = '''a'''\\n__result__ = 1\")" in body, body
    w = " | ".join(out["warnings"])
    assert "failed in the session" in w and "ANKUSDRIVE_ALLOW_RUN_SCRIPT" in w and "entry cap" in w, w


def test_multiline_error_stays_in_its_comment():
    err = "RuntimeError: first line\nimport os; os.remove('x')\n" + "y" * 1000
    body = _body(R.export([E(1, "fillet_edges", {"handle": "h_1", "note": "a\nb"}, ok=False, error=err)]))
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    assert all(ln.startswith("#") or ln.startswith("s.step") for ln in lines), lines
    assert any(ln.endswith("…") for ln in lines), "a long error should be shortened"


def test_pid_change_without_restart_is_made_explicit():
    out = R.export([
        E(1, "list_workspaces", {}, {}, pid=None),
        E(2, "add_primitive", {"kind": "box"}, {"handle": "box_1"}, pid=10),
        E(3, "add_primitive", {"kind": "box"}, {"handle": "box_1"}, pid=12),
    ])
    body = _body(out)
    assert body.count("s.restart_worker()") == 1 and "was replaced here" in body, body
    assert any("without a restart_worker call" in w for w in out["warnings"]), out["warnings"]
    assert out["skipped"][0]["reason"] == "workspace management"


def test_empty_journal_is_a_valid_script():
    out = R.export([])
    body = _body(out)
    assert "pass  # the journal has no calls" in body and out["exported"] == 0, body


def test_generated_identifiers_are_safe():
    body = _body(R.export([
        E(1, "new_document", {"name": "x"}, {"doc": "list"}),
        E(2, "set_active_document", {"doc": "list"}),
        E(3, "add_primitive", {}, {"handle": "class_1"}),
        E(4, "pad", {"sketch": "class_1"}, {"handle": "1abc"}),
        E(5, "pocket", {"handle": "1abc"}),
    ]))
    assert "doc_list = r1['doc']" in body and "class_1 = r3['handle']" in body, body
    assert "handle_1abc = r4['handle']" in body, body


# --- static: Session -------------------------------------------------------------------

class _Server:
    def __init__(self):
        self.polls = 0
        self.cleaned = False

    def _cleanup(self):
        self.cleaned = True

    def add_primitive(self, **kw):
        return {"handle": "box_1", "volume": 1000.0 + kw.get("bump", 0)}

    def em_conduction_submit(self, **kw):
        return {"ok": False, "solver": "ccx", "status": "absent", "reason": "not installed",
                "install": "brew install calculix"}

    def run_script(self, **kw):
        return {"ok": False, "status": "disabled", "reason": "off", "enable": "set the switch"}

    def dfm_check(self, **kw):
        return {"ok": False, "problems": ["wall too thin"]}

    def job_status(self, job_id):
        self.polls += 1
        return {"job_id": job_id, "status": "running" if self.polls < 3 else "done"}

    def job_result(self, job_id, discard=False):
        return {"job_id": job_id, "status": "done", "result": {"max_von_mises": 5.0}}

    def render_job(self, job_id, discard=False):
        return {"job_id": job_id, "status": "failed", "error": "no renderer"}

    def boolean_op(self, **kw):
        raise RuntimeError("shapes do not intersect")


def test_session_calls_only_known_tools():
    srv = _Server()
    with R.Session(server=srv) as s:
        assert s.add_primitive(kind="box")["handle"] == "box_1"
        for bad in ("_cleanup", "not_a_tool", "add_primitve"):
            try:
                getattr(s, bad)
            except AttributeError as e:
                if bad == "add_primitve":
                    assert "add_primitive" in str(e), e
            else:
                raise AssertionError(f"Session exposed {bad}")
    assert srv.cleaned, "leaving the with-block must shut the workers down"


def test_session_raises_on_refusals_and_errors_not_on_check_results():
    s = R.Session(server=_Server())
    s.step = 7
    for tool, needle in (("em_conduction_submit", "brew install calculix"), ("run_script", "set the switch"),
                         ("boolean_op", "shapes do not intersect")):
        try:
            getattr(s, tool)()
        except R.ReplayError as e:
            assert str(e).startswith("step 7: ") and needle in str(e), e
        else:
            raise AssertionError(f"{tool} did not raise")
    assert s.dfm_check()["ok"] is False, "a check that finds problems is a result, not a refusal"
    assert R.Session(server=_Server(), strict=False).em_conduction_submit()["status"] == "absent"


def test_session_wait_and_check():
    s = R.Session(server=_Server())
    r = s.wait({"job_id": "job_1"}, poll_s=0)
    assert r["status"] == "done" and s.check(r, ("result", "max_von_mises"), 5.0) == 5.0
    try:
        s.wait("render_job_1", poll_s=0)
    except R.ReplayError as e:
        assert "no renderer" in str(e)
    else:
        raise AssertionError("failed render did not raise")
    box = s.add_primitive(bump=0.5)
    try:
        s.check(box, "volume", 1000.0)
    except R.ReplayError as e:
        assert "recorded 1000.0" in str(e), e
    else:
        raise AssertionError("drift not caught")
    assert s.check(box, "volume", 1000.0, rel=1e-3) == 1000.5
    try:
        s.check(box, "mass", 1.0)
    except R.ReplayError as e:
        assert "no ['mass']" in str(e)
    else:
        raise AssertionError("missing key not caught")


def test_replay_imports_no_server_at_module_level():
    tree = ast.parse((PKG / "replay.py").read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name for n in top for a in n.names} | {getattr(n, "module", None) for n in top}
    assert not names & {"ankusdrive", "mcp", "mcp_server", "FreeCAD"}, names


# --- worker (needs mcp + FreeCAD) ------------------------------------------------------

def worker_test_record_export_replay():
    import asyncio
    os.environ.setdefault("ANKUSDRIVE_TOOLSETS", "all")
    from ankusdrive import journal, mcp_server
    call = mcp_server.mcp._tool_manager.call_tool

    def run(_tool, **args):
        return asyncio.run(call(_tool, args))

    with tempfile.TemporaryDirectory() as rec_dir, tempfile.TemporaryDirectory() as out_dir:
        journal.clear()
        try:
            run("new_document", name="replay_test")
            box = run("add_primitive", kind="box", w=40, d=20, h=5)
            try:
                run("fillet_edges", handle=box["handle"], edges=["Edge999"], radius=1.0)
            except Exception:
                pass
            run("transaction_open", label="try")
            run("add_primitive", kind="cylinder", r=2, h=50)
            run("transaction_abort")
            cyl = run("add_primitive", kind="cylinder", r=3, h=5)
            cut = run("boolean_op", op="cut", base=box["handle"], tool=cyl["handle"])
            recorded = run("mass_properties", handle=cut["handle"])
            run("list_faces", handle=cut["handle"])
            run("save_document", path=str(Path(rec_dir) / "out" / "part.FCStd"))
        finally:
            mcp_server._cleanup()
        snap = journal.snapshot("default")
        out = R.export(snap["entries"], prune_aborted=True)
        script = Path(out_dir) / "transcript.py"
        script.write_text(out["script"], encoding="utf-8")
        assert rec_dir not in out["script"], "recording paths leaked into the script"
        assert "s.check(r" in out["script"] and "'volume_mm3'" in out["script"] and "FAILED in the session" in out["script"], out["script"]

        regen = Path(out_dir) / "regen"
        env = {**os.environ, "PYTHONPATH": str(REPO), "ANKUSDRIVE_TOOLSETS": "all"}
        p = subprocess.run([sys.executable, str(script), str(regen)], env=env, cwd=out_dir,
                           capture_output=True, text=True, timeout=300)
        assert p.returncode == 0, f"replay failed:\n{p.stdout}\n{p.stderr}\n--- script ---\n{out['script']}"
        saved = regen / "part.FCStd"        # one recorded path: WORKDIR is its folder
        assert saved.exists(), f"replay did not write {saved}: {sorted(regen.rglob('*'))}"

        from ankusdrive import Worker
        with Worker() as w:
            w.call("open_document", path=str(saved))
            names = [o["name"] for o in w.call("list_objects")]
            cut_obj = next(n for n in reversed(names) if n.startswith("Cut"))
            h = w.call("register_handle", object=cut_obj)["handle"]
            vol = w.call("mass_properties", handle=h)["volume_mm3"]
        assert abs(vol - recorded["volume_mm3"]) <= 1e-6 * recorded["volume_mm3"], (vol, recorded["volume_mm3"])


def _freecad_available() -> bool:
    try:
        from ankusdrive import client
        path = client._resolve_freecadcmd()
    except Exception:
        return False
    return bool(path and Path(path).exists())


def _tests():
    g = globals()
    out = [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]
    if importlib.util.find_spec("mcp") is None:
        print(f"  SKIP worker tier — `mcp` not importable in {sys.executable}")
    elif not _freecad_available():
        print("  SKIP worker tier — freecadcmd not resolvable")
    else:
        out.append(("worker_test_record_export_replay", worker_test_record_export_replay))
    return out


def main():
    failures = []
    t_suite = time.time()
    tests = _tests()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:58s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:58s} ({time.time() - t0:.2f}s)")
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
