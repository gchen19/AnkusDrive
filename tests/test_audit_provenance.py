"""Auditing a simulation (#433, epic #309) — the transcript says what computed a number.

``session_transcript`` already exported a session as a script that regenerates the
model. For a *simulation* it did not yet answer the question an audit asks: which
tools produced the figure, what did each one report, and which solver — which binary,
which version, reached how — actually ran. Three tiers, each skipping (and saying so)
when its prerequisite is missing:

  * **static** (stdlib, fast lane)
      - ``provenance``: the environment header, FreeCAD's raw version list flattened,
        ``$HOME`` collapsed out of solver paths, the per-session solver record built
        from journal entries (including a solver that MOVED mid-session), and ``drift``
        reporting a different version / a solver that is gone;
      - the exporter: an analysis call is never pruned, its numbers all become
        ``s.check`` and its verdict fields ``s.expect``, a solve's job result is
        checked whole, an in-flight ``status`` is not asserted, ``run_script`` carries
        its SHA-256, and the script carries a ``PROVENANCE`` record it re-checks;
      - ``Session.expect`` / ``Session.provenance``.
  * **server** (needs ``mcp``) — ``session_transcript`` returns the provenance record
    and stays read-only with it.
  * **worker + solver** (needs FreeCAD and Elmer) — record a real transient-thermal
    solve, export it, and find the solved temperatures and the solver's identity in
    the script.

Run:  .venv/bin/python3 tests/test_audit_provenance.py
"""
import ast
import importlib.util
import os
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "ankusdrive"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))   # the shared _mcp_python helper
from _mcp_python import HOST_DEPS, ensure_mcp_interpreter, mcp_skip_reason  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _load("replay_under_audit", PKG / "replay.py")


def E(seq, tool, args=None, result=None, ok=True, pid=10, **extra):
    e = {"seq": seq, "tool": tool, "args": args or {}, "ok": ok, "workspace": "default",
         "worker_pid": pid, "txn_depth": 0, "elapsed_s": 0.0}
    if ok:
        e["result"] = result if result is not None else {}
    else:
        e["error"] = "ValueError: no"
    e.update(extra)
    return e


# --- provenance ---------------------------------------------------------------------

def test_header_and_freecad_version():
    from ankusdrive import provenance as prov
    head = prov.header()
    assert head["ankusdrive"] and head["python"], head
    assert head["platform"]["system"] and "run_script_allowed" in head, head
    assert "freecad" not in head, "FreeCAD must not be guessed when nobody supplied it"
    # FreeCAD reports App.Version() as a list; it is flattened, hash kept and shortened
    fc = prov.header(freecad={"freecad": ["1", "1", "0", "20260325 (Git shallow)", "Unknown",
                                          "2026/03/25", "(HEAD detached at 34a9716)",
                                          "34a9716668b1ddeb55b914f1c5be644826bdbbbf"],
                              "python": "3.11.14"})["freecad"]
    assert fc["version"] == "1.1.0" and fc["python"] == "3.11.14", fc
    assert fc["build"] == "34a9716668b1", fc
    assert prov.header(freecad={"freecad": "1.1.1"})["freecad"]["version"] == "1.1.1"


def test_home_is_collapsed_out_of_solver_paths():
    from ankusdrive import provenance as prov
    home = os.path.expanduser("~")
    assert prov.tilde(os.path.join(home, "opt", "yade", "bin", "yade")) == \
        os.path.join("~", "opt", "yade", "bin", "yade").replace("\\", os.sep)
    assert prov.tilde("/usr/bin/ElmerSolver") == "/usr/bin/ElmerSolver"
    assert prov.tilde(None) is None
    # the collapse reaches the record an export publishes
    rec = prov._redact({"name": "yade", "path": os.path.join(home, "bin", "yade"), "mtime": 7})
    assert rec["path"].startswith("~") and "mtime" not in rec, rec


def test_from_entries_identifies_each_solver_and_notices_a_move():
    from ankusdrive import provenance as prov
    real = prov.identify
    prov.identify = lambda name, resolved=None: {**(resolved or {"name": name}),
                                                 "version": "26.2",
                                                 "version_source": "stub"}
    try:
        entries = [
            E(1, "h_estimate", result={"h_total_w_m2k": 8.4}),
            E(2, "job_result", result={"result": {"solver": "elmer"}},
              solvers=[{"name": "elmer", "available": True, "kind": "binary",
                        "via": "multipass", "path": "/usr/bin/ElmerSolver"}]),
            E(3, "job_result", result={"result": {"solver": "elmer"}},
              solvers=[{"name": "elmer", "available": True, "kind": "binary",
                        "via": "container", "path": "/usr/bin/ElmerSolver"}]),
        ]
        rec = prov.from_entries(entries, freecad={"freecad": "1.1.1"})
    finally:
        prov.identify = real
    assert set(rec["solvers"]) == {"elmer"}, rec
    elmer = rec["solvers"]["elmer"]
    assert elmer["version"] == "26.2" and elmer["via"] == "container", elmer
    # the substrate moved mid-session: both answers are on the record (#433)
    assert elmer["moved"] == [{"available": True, "via": "multipass",
                               "path": "/usr/bin/ElmerSolver"}], elmer
    assert rec["env"]["freecad"]["version"] == "1.1.1", rec["env"]
    assert prov.from_entries([])["solvers"] == {}


def test_drift_names_every_difference():
    from ankusdrive import provenance as prov
    recorded = {"env": {"ankusdrive": "0.5.0", "python": "3.12.3", "substrate": "multipass",
                        "freecad": {"version": "1.0.0"}},
                "solvers": {"elmer": {"name": "elmer", "available": True, "version": "9.0",
                                      "via": "multipass", "path": "/usr/bin/ElmerSolver"},
                            "yade": {"name": "yade", "available": True, "version": "1"}}}
    current = {"env": {"ankusdrive": "0.5.5", "python": "3.12.3", "substrate": "container",
                       "freecad": {"version": "1.1.0"}},
               "solvers": {"elmer": {"name": "elmer", "available": True, "version": "26.2",
                                     "via": "container", "path": "/usr/bin/ElmerSolver"},
                           "yade": {"name": "yade", "available": False}}}
    lines = prov.drift(recorded, current)
    blob = "\n".join(lines)
    assert "ankusdrive: recorded 0.5.0, now 0.5.5" in blob, lines
    assert "substrate: recorded multipass, now container" in blob, lines
    assert "freecad: recorded 1.0.0, now 1.1.0" in blob, lines
    assert "solver elmer version: recorded 9.0, now 26.2" in blob, lines
    assert "solver elmer via: recorded multipass, now container" in blob, lines
    assert "NOT AVAILABLE here" in blob and "yade" in blob, lines
    assert prov.drift(recorded, recorded) == [], "an unchanged environment has no drift"


# --- the exporter, as an audit record ------------------------------------------------

H_ESTIMATE = {"geometry": "vertical_plate", "mode": "natural",
              "correlation": "churchill_chu_vertical_plate", "h_total_w_m2k": 8.4111,
              "rayleigh": 5171268.12, "fidelity": "correlation", "band_pct": 20.0,
              "valid_range_ok": True, "warnings": [], "escalate_to": "cht_channel_submit"}


def test_analysis_calls_are_never_pruned():
    """The load-bearing half of a thermal session is read-only tools. Dropping them
    leaves a transcript of the CAD around the analysis with the analysis removed."""
    analysis = R.analysis_tools()
    for tool in ("h_estimate", "thermal_transient_1d", "beam_modal", "fem_results",
                 "cfd_pipe_flow", "thermal_transient_submit"):
        assert tool in analysis, f"{tool} is not treated as a derivation step"
    assert "list_faces" not in analysis and "bounding_box" not in analysis
    out = R.export([E(1, "h_estimate", args={"geometry": "vertical_plate"}, result=H_ESTIMATE),
                    E(2, "list_faces", args={"handle": "s1"}, result={"faces": []})])
    assert "s.h_estimate(" in out["script"], out["script"]
    assert "s.list_faces(" not in out["script"], out["script"]
    assert any(s["tool"] == "list_faces" and s["reason"] == "read-only"
               for s in out["skipped"]), out["skipped"]


def test_an_analysis_result_is_checked_whole():
    out = R.export([E(1, "h_estimate", args={"geometry": "vertical_plate"}, result=H_ESTIMATE)])
    script = out["script"]
    for key, value in (("h_total_w_m2k", 8.4111), ("rayleigh", 5171268.12), ("band_pct", 20.0)):
        assert f"s.check(r1, {key!r}, {value!r}" in script, script
    # the verdict fields say what the analysis WAS, and are compared exactly
    assert "s.expect(r1, 'fidelity', 'correlation')" in script, script
    assert "s.expect(r1, 'correlation', 'churchill_chu_vertical_plate')" in script, script
    assert "s.expect(r1, 'mode', 'natural')" in script, script
    # a solver's band is not geometry: 1e-3, not 1e-6
    assert "rel=0.001" in script, script
    compile(script, "t.py", "exec")


def test_a_solve_is_checked_whole_and_names_its_solver():
    job = {"job_id": "job_1", "kind": "thermal_transient", "status": "done", "elapsed_s": 0.7,
           "result": {"ok": True, "returncode": 0, "solver": "elmer", "t_center_c": 45.49,
                      "t_surface_c": 44.89, "stdout_tail": "…",
                      "gate": {"pass": True, "score": 0.9, "band_pct": 10.0}}}
    out = R.export([
        E(1, "thermal_transient_submit", args={"half_thickness_mm": 1.5},
          result={"job_id": "job_1", "status": "running", "cache_hit": False}),
        E(2, "job_status", args={"job_id": "job_1"}, result={"status": "running"}),
        E(3, "job_result", args={"job_id": "job_1"}, result=job,
          solvers=[{"name": "elmer", "available": True, "via": "host",
                    "path": "/usr/bin/ElmerSolver"}]),
    ])
    script = out["script"]
    assert "s.check(r3, ('result', 't_center_c'), 45.49, rel=0.001)" in script, script
    assert "s.check(r3, ('result', 'gate', 'score'), 0.9, rel=0.001)" in script, script
    assert "s.expect(r3, ('result', 'solver'), 'elmer')" in script, script
    assert "s.expect(r3, ('result', 'gate', 'pass'), True)" in script, script
    assert "s.expect(r3, 'status', 'done')" in script, script
    # wall-clock is not a claim about the part, and an in-flight status is not a verdict
    assert "elapsed_s" not in script, script
    assert "'running'" not in script, script
    compile(script, "t.py", "exec")


def test_run_script_carries_its_content_hash():
    code = "result = {'tau_s': 273.0}\n"
    digest = "sha256:" + __import__("hashlib").sha256(code.encode()).hexdigest()
    out = R.export([E(1, "run_script", args={"code": code}, result={"result": None},
                      code_sha256=digest)])
    assert f"# code {digest}" in out["script"], out["script"]
    assert code.strip() in out["script"].replace("\\n", "\n"), out["script"]


def test_the_script_carries_and_rechecks_its_environment():
    rec = {"env": {"ankusdrive": "0.5.5", "python": "3.12.3", "substrate": "container",
                   "platform": {"system": "Linux", "machine": "x86_64"},
                   "freecad": {"version": "1.1.0"}},
           "solvers": {"elmer": {"name": "elmer", "available": True, "version": "26.2",
                                 "via": "container", "path": "/usr/bin/ElmerSolver",
                                 "moved": [{"via": "multipass", "path": "/usr/bin/ElmerSolver"}]}}}
    out = R.export([E(1, "h_estimate", args={"geometry": "vertical_plate"}, result=H_ESTIMATE)],
                   provenance=rec)
    script = out["script"]
    assert "ankusdrive 0.5.5" in script and "FreeCAD 1.1.0" in script, script
    assert "elmer 26.2 — /usr/bin/ElmerSolver (via container)" in script, script
    assert "earlier in the session" in script, script
    assert "PROVENANCE = {" in script and "s.provenance(PROVENANCE)" in script, script
    compile(script, "t.py", "exec")
    # the literal in the script IS the record, not a prose summary of it
    node = next(b for b in ast.parse(script).body
                if isinstance(b, ast.Assign) and getattr(b.targets[0], "id", "") == "PROVENANCE")
    assert ast.literal_eval(node.value) == rec, ast.literal_eval(node.value)
    empty = R.export([], provenance=rec)["script"]          # even an empty session states it
    assert [ln.strip() for ln in empty.splitlines()].count("s.provenance(PROVENANCE)") == 1, empty


def test_provenance_is_optional():
    out = R.export([E(1, "h_estimate", args={"geometry": "vertical_plate"}, result=H_ESTIMATE)])
    assert "PROVENANCE" not in out["script"], out["script"]
    empty = R.export([], provenance={"env": {"ankusdrive": "0.5.5"}, "solvers": {}})
    assert "no calls to replay" in empty["script"], empty["script"]
    compile(empty["script"], "t.py", "exec")


# --- Session.expect / Session.provenance ---------------------------------------------

class _StubServer:
    def _cleanup(self):
        pass


def test_session_expect_and_provenance():
    s = R.Session(server=_StubServer())
    r = {"fidelity": "correlation", "gate": {"pass": True}}
    assert s.expect(r, "fidelity", "correlation") == "correlation"
    assert s.expect(r, ("gate", "pass"), True) is True
    for bad in (lambda: s.expect(r, "fidelity", "solve"),
                lambda: s.expect(r, ("gate", "pass"), False),
                lambda: s.expect(r, "nope", 1)):
        try:
            bad()
        except R.ReplayError:
            pass
        else:
            raise AssertionError("expect() accepted a value that drifted")
    from ankusdrive import provenance as prov
    real, rec = prov.drift, {"env": {}, "solvers": {}}
    prov.drift = lambda recorded, current=None: ["solver elmer version: recorded 9, now 26"]
    try:
        assert s.provenance(rec) == ["solver elmer version: recorded 9, now 26"]
        try:
            s.provenance(rec, strict=True)
        except R.ReplayError:
            pass
        else:
            raise AssertionError("strict provenance accepted a changed environment")
        prov.drift = lambda recorded, current=None: []
        assert s.provenance(rec) == []
    finally:
        prov.drift = real


# --- server tier ---------------------------------------------------------------------

def server_test_transcript_returns_provenance():
    os.environ.setdefault("ANKUSDRIVE_TOOLSETS", "all")
    from ankusdrive import journal, mcp_server
    journal.clear()
    try:
        out = mcp_server.mcp._tool_manager._tools["session_transcript"].fn()
        assert "provenance" in out, sorted(out)
        assert out["provenance"]["env"]["ankusdrive"], out["provenance"]
        assert out["provenance"]["solvers"] == {}, out["provenance"]
        off = mcp_server.mcp._tool_manager._tools["session_transcript"].fn(provenance=False)
        assert "provenance" not in off, sorted(off)
        assert "PROVENANCE" not in off["script"], off["script"]
    finally:
        mcp_server._cleanup()
        journal.clear()


# --- worker + solver tier -------------------------------------------------------------

def solver_test_real_solve_is_auditable():
    """The end the issue asks for: a real Elmer solve, exported, with the numbers it
    produced and the binary that produced them both in the script."""
    import asyncio
    os.environ.setdefault("ANKUSDRIVE_TOOLSETS", "all")
    from ankusdrive import journal, mcp_server
    call = mcp_server.mcp._tool_manager.call_tool

    def run(_tool, **args):
        return asyncio.run(call(_tool, args))

    journal.clear()
    try:
        run("h_estimate", geometry="vertical_plate", characteristic_mm=100.0, t_surface_c=200.0)
        sub = run("thermal_transient_submit", half_thickness_mm=1.5, h_conv=8.0,
                  duration_s=600.0, k=0.2, rho=1040.0, cp=1400.0, t_initial_c=200.0)
        sub = _payload(sub)
        assert sub.get("job_id"), sub
        for _ in range(300):
            if _payload(run("job_status", job_id=sub["job_id"]))["status"] != "running":
                break
            time.sleep(1.0)
        res = _payload(run("job_result", job_id=sub["job_id"]))
        assert res["status"] == "done", res
        t_center = res["result"]["t_center_c"]
        out = mcp_server.mcp._tool_manager._tools["session_transcript"].fn()
    finally:
        mcp_server._cleanup()
        journal.clear()
    script = out["script"]
    assert "s.h_estimate(" in script, script
    assert f"('result', 't_center_c'), {t_center!r}, rel=0.001)" in script, script
    assert "('result', 'solver'), 'elmer')" in script, script
    elmer = out["provenance"]["solvers"]["elmer"]
    assert elmer["available"] and elmer["path"], elmer
    assert elmer["version"], f"the solver's version was not probed: {elmer}"
    assert f"elmer {elmer['version']}" in script, script
    compile(script, "t.py", "exec")


def _payload(result):
    """FastMCP's call_tool returns (content, structured) on recent versions."""
    if isinstance(result, tuple):
        result = result[-1]
    if isinstance(result, dict) and set(result) == {"result"}:
        return result["result"]
    return result


# --- runner ----------------------------------------------------------------------------

def _freecad_available() -> bool:
    try:
        from ankusdrive import client
        path = client._resolve_freecadcmd()
        # resolution returns the path it WOULD use, present or not — the file has to
        # exist or the worker tier runs and dies on a hosted runner with no FreeCAD
        return bool(path and Path(path).exists())
    except Exception:
        return False


def _elmer_available() -> bool:
    try:
        from ankusdrive import solvers
        return bool(solvers.find_solver("elmer")["available"])
    except Exception:
        return False


def _tests():
    g = globals()
    out = [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]
    if importlib.util.find_spec("mcp") is None:
        print(f"  SKIP server tier — {mcp_skip_reason()}")
        return out
    out.append(("server_test_transcript_returns_provenance", server_test_transcript_returns_provenance))
    if not _freecad_available():
        print("  SKIP solver tier — freecadcmd not resolvable")
    elif not _elmer_available():
        print("  SKIP solver tier — ElmerSolver not installed")
    else:
        out.append(("solver_test_real_solve_is_auditable", solver_test_real_solve_is_auditable))
    return out


def main():
    # An interpreter with `mcp` (and the rest of the host deps), found by probing —
    # not assumed to be the one run_all.sh started (#449). Re-execs, or returns.
    ensure_mcp_interpreter(__file__, needs=HOST_DEPS)
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
