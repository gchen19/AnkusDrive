"""Solver decks (#437, item 2) — a replay that drifts says which input file moved.

A result names its `case_dir`, but the solver writes its outputs into that same
directory, so by the time anyone looks the deck is buried. Each job now records a
manifest of its case at the moment its first solver step launches, the result carries
it as `deck`, and a transcript compares it with `s.deck(...)` ahead of the checks — so
a number that drifted arrives with "~ case.sif" printed right above it, or with "deck
matches", which puts the blame on the solver or the environment instead.

Measured before building it: two independent runs of the same Elmer slab and OpenFOAM
pipe produce byte-identical inputs, and every file that differs is an output. The one
exception is FreeCAD's CalculiX writer, which stamps the wall clock into every .inp —
that line is dropped before hashing, and a test pins it.

Three tiers:

  * **static** (stdlib, fast lane) — the manifest (sorted, POSIX paths, full-strength
    aggregate, CRLF == LF, the volatile .inp line, `only=`, the caps, symlinks), the
    once-per-job snapshot and its attach, the jobs hook, the exporter and
    `Session.deck`, and an AST sweep: every function in the worker that launches a
    solver records the deck, or is on a named exemption list with its reason.
  * **worker** (FreeCAD) — a real CalculiX FEM solve: `fem_run` and `fem_run_submit`
    on the same analysis seconds apart carry the same deck, a load change names
    `Mesh.inp`, and the previous run's .frd in the workdir is not part of it.
  * **solver** (FreeCAD + Elmer) — independent Elmer solves agree, a changed h_conv
    names `case.sif`, and a recorded transcript replayed with that change prints the
    deck difference before the check that fails.

Run:  .venv/bin/python3 tests/test_case_deck.py
"""
import ast
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "ankusdrive"
sys.path.insert(0, str(REPO))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _load("replay_for_deck", PKG / "replay.py")
from ankusdrive import cases as C  # noqa: E402  (the same module the worker and jobs import)


class _Deck:
    """A scratch case directory, torn down after."""

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="deck_")
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)

    def write(self, rel, text, newline="\n"):
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="") as fh:
            fh.write(text.replace("\n", newline))
        return full


# --- the manifest --------------------------------------------------------------------

def test_manifest_is_deterministic_and_posix():
    with _Deck() as d:
        d.write("system/controlDict", "endTime 5;\n")
        d.write("0/U", "internalField uniform (0.5 0 0);\n")
        d.write("case.sif", "Header\nEnd\n")
        m = C.manifest(d.path)
        assert m == C.manifest(d.path), "the same directory hashed twice disagreed"
        assert list(m["files"]) == ["0/U", "case.sif", "system/controlDict"], m["files"]
        assert all(len(h) == 16 for h in m["files"].values()), m["files"]
        assert m["digest"].startswith("sha256:") and len(m["digest"]) == 7 + 64, m
        assert m["count"] == 3 and m["bytes"] > 0, m


def test_the_aggregate_is_full_strength_not_the_short_table():
    """The per-file table is shortened to keep solve results small; the claim a report
    cites — the digest — is built from the full hashes."""
    with _Deck() as d:
        d.write("a.txt", "one\n")
        m = C.manifest(d.path)
        full = hashlib.sha256(b"one\n").hexdigest()
        agg = hashlib.sha256(b"a.txt\0" + full.encode() + b"\n").hexdigest()
        assert m["digest"] == "sha256:" + agg, m
        assert m["files"]["a.txt"] == full[:16]


def test_crlf_and_lf_are_the_same_deck():
    """A deck written on Windows (text mode writes CRLF) is the same deck to a solver,
    so a transcript recorded on one OS and replayed on another must not call every
    file changed."""
    with _Deck() as a, _Deck() as b:
        a.write("case.sif", "Header\n  Mesh DB \".\"\nEnd\n")
        b.write("case.sif", "Header\n  Mesh DB \".\"\nEnd\n", newline="\r\n")
        assert C.manifest(a.path)["digest"] == C.manifest(b.path)["digest"]
    # a CRLF split across the 1 MiB read boundary still normalises
    with _Deck() as a, _Deck() as b:
        body = "x" * ((1 << 20) - 1) + "\nend\n"
        a.write("big.txt", body)
        b.write("big.txt", body, newline="\r\n")
        assert C.manifest(a.path)["digest"] == C.manifest(b.path)["digest"]


def test_the_ccx_timestamp_line_is_not_part_of_the_deck():
    """FreeCAD's CalculiX writer stamps the wall clock into every .inp. Two identical
    FEM runs a few seconds apart differed in exactly that line and nothing else."""
    head = "** written by FreeCAD inp file writer for CalculiX,Abaqus meshes\n"
    with _Deck() as a, _Deck() as b:
        a.write("Mesh.inp", head + "**   written on    --> Mon Sep 21 00:15:05 2026\n*NODE\n1, 0, 0, 0\n")
        b.write("Mesh.inp", head + "**   written on    --> Mon Sep 21 00:15:08 2026\n*NODE\n1, 0, 0, 0\n")
        assert C.manifest(a.path)["digest"] == C.manifest(b.path)["digest"]
        b.write("Mesh.inp", head + "**   written on    --> Mon Sep 21 00:15:08 2026\n*NODE\n1, 0, 0, 1\n")
        assert C.manifest(a.path)["digest"] != C.manifest(b.path)["digest"], \
            "stripping the timestamp must not hide a real change"
    # only for .inp: the same text in another file type is content
    with _Deck() as a, _Deck() as b:
        a.write("notes.txt", "**   written on    --> Mon Sep 21 00:15:05 2026\n")
        b.write("notes.txt", "**   written on    --> Mon Sep 21 00:15:08 2026\n")
        assert C.manifest(a.path)["digest"] != C.manifest(b.path)["digest"]


def test_only_restricts_the_deck_to_the_named_files():
    """CalculiX's whole input is its .inp; the FEM workdir also holds the last run's
    .frd and .dat, which must not make a re-run look like a new deck."""
    with _Deck() as d:
        d.write("Mesh.inp", "*NODE\n")
        before = C.manifest(d.path, only=["Mesh.inp"])
        d.write("Mesh.frd", "results of the previous run\n")
        d.write("Mesh.dat", "more results\n")
        after = C.manifest(d.path, only=["Mesh.inp"])
        assert before == after and list(after["files"]) == ["Mesh.inp"], after
        assert C.manifest(d.path)["count"] == 3


def test_caps_and_symlinks():
    with _Deck() as d:
        for i in range(5):
            d.write(f"f{i}.txt", str(i))
        old_cap, old_max = C.DECK_FILE_CAP, C.DECK_MAX_FILES
        try:
            C.DECK_FILE_CAP = 3
            m = C.manifest(d.path)
            assert "files" not in m and m["digest"] and m["count"] == 5, m
            C.DECK_MAX_FILES = 3
            m = C.manifest(d.path)
            assert m["digest"] is None and "files" in m["truncated"], m
        finally:
            C.DECK_FILE_CAP, C.DECK_MAX_FILES = old_cap, old_max
        if hasattr(os, "symlink"):
            try:
                os.symlink(os.path.join(d.path, "f0.txt"), os.path.join(d.path, "link.txt"))
            except OSError:
                return                                  # no symlink privilege (Windows)
            assert "link.txt" not in C.manifest(d.path)["files"]


def test_diff_names_the_file():
    with _Deck() as d:
        d.write("case.sif", "h 8\n")
        d.write("mesh.nodes", "1\n")
        a = C.manifest(d.path)
        d.write("case.sif", "h 12\n")
        d.write("extra.txt", "x")
        os.remove(os.path.join(d.path, "mesh.nodes"))
        lines = C.diff(a, C.manifest(d.path))
        assert lines == ["+ extra.txt  (new, not in the recording)",
                         "~ case.sif",
                         "- mesh.nodes  (recorded, missing now)"] or \
            sorted(lines) == sorted(["+ extra.txt  (new, not in the recording)", "~ case.sif",
                                     "- mesh.nodes  (recorded, missing now)"]), lines
        assert C.diff(a, a) == []
        assert C.diff(a, None) == ["no deck on this run"]
        big = dict(a, files=None)
        assert "too many to list" in C.diff({**a, "digest": "sha256:x", "files": None},
                                            {**a, "digest": "sha256:y", "files": None})[0]
        assert big


# --- once per job, attached at the end ------------------------------------------------

def test_snapshot_is_once_per_job_and_fresh_for_the_next():
    """A job's later steps see its own outputs — ElmerGrid then ElmerSolver, fill then
    pack — so only its first launch is the deck. A NEW job on the same directory (the
    molding pack continuing a fill) is handed those outputs, so it gets a fresh one."""
    with _Deck() as d:
        d.write("case.sif", "v1\n")
        got = {}

        def job(tag, mutate):
            C.forget()
            C.snapshot(d.path)
            if mutate:
                d.write("log.solver", "output of this job\n")
                C.snapshot(d.path)                  # a later step of the same job
            got[tag] = C.attach({"case_dir": d.path})["deck"]
            C.forget()

        t = threading.Thread(target=job, args=("first", True)); t.start(); t.join()
        t = threading.Thread(target=job, args=("second", False)); t.start(); t.join()
        assert list(got["first"]["files"]) == ["case.sif"], got["first"]
        assert sorted(got["second"]["files"]) == ["case.sif", "log.solver"], got["second"]


def test_attach_places_decks_beside_their_directories():
    with _Deck() as a, _Deck() as b:
        a.write("case.sif", "x")
        b.write("Mesh.inp", "y")
        C.forget()
        C.snapshot(a.path)
        C.snapshot(b.path, only=["Mesh.inp"])
        out = C.attach({"ok": True, "levels": [{"case_dir": a.path}, {"label": "no dir"}],
                        "fem": {"workdir": b.path}, "other": {"case_dir": "/not/snapshotted"}})
        assert "deck" in out["levels"][0] and "deck" not in out["levels"][1], out
        assert list(out["fem"]["deck"]["files"]) == ["Mesh.inp"], out["fem"]
        assert "deck" not in out["other"], "a directory this job never launched in got a deck"
        assert C.attach("not a dict") == "not a dict"
        C.forget()
        assert "deck" not in C.attach({"case_dir": a.path}), "forget() did not clear the job"


def test_a_job_result_carries_its_deck_and_never_fails_for_it():
    jobs = _load("jobs_for_deck", PKG / "jobs.py")
    with _Deck() as d:
        d.write("case.sif", "x")

        def solve():
            C.snapshot(d.path)
            return {"ok": True, "case_dir": d.path}

        sub = jobs.submit("deck_test", solve)
        for _ in range(200):
            if jobs.status(sub["job_id"])["status"] != "running":
                break
            time.sleep(0.01)
        res = jobs.result(sub["job_id"])["result"]
        assert res["deck"]["files"] == C.manifest(d.path)["files"], res
    # a result attach cannot walk is returned untouched, and the job is still done
    sub = jobs.submit("deck_odd", lambda: 42)
    for _ in range(200):
        if jobs.status(sub["job_id"])["status"] != "running":
            break
        time.sleep(0.01)
    assert jobs.result(sub["job_id"]) ["result"] == 42


# --- the transcript ---------------------------------------------------------------------

def E(seq, tool, args=None, result=None):
    return {"seq": seq, "tool": tool, "args": args or {}, "ok": True, "workspace": "default",
            "worker_pid": 10, "txn_depth": 0, "elapsed_s": 0.0, "result": result or {}}


DECK = {"digest": "sha256:" + "a" * 64, "count": 2, "bytes": 90,
        "files": {"case.sif": "0123456789abcdef", "slab/mesh.nodes": "fedcba9876543210"}}


def _solve_journal(deck=DECK, extra=None):
    res = {"ok": True, "solver": "elmer", "case_dir": "/tmp/x", "t_center_c": 45.49, "deck": deck}
    res.update(extra or {})
    return [E(1, "thermal_transient_submit", {"half_thickness_mm": 1.5},
              {"job_id": "job_1", "status": "running"}),
            E(2, "job_result", {"job_id": "job_1"},
              {"job_id": "job_1", "status": "done", "result": res})]


def test_the_transcript_compares_the_deck_before_the_numbers():
    script = R.export(_solve_journal())["script"]
    lines = [ln.strip() for ln in script.splitlines()]
    at_deck = next(i for i, ln in enumerate(lines) if ln.startswith("s.deck("))
    at_check = next(i for i, ln in enumerate(lines) if ln.startswith("s.check("))
    assert at_deck < at_check, "the deck must be compared before the first check can fail"
    assert "('result', 'deck')" in lines[at_deck], lines[at_deck]
    compile(script, "t.py", "exec")
    # the literal IS the manifest
    tree = ast.parse(script)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and getattr(n.func, "attr", "") == "deck")
    assert ast.literal_eval(call.args[2]) == DECK
    # a deck's own numbers are not re-checked as if they were results
    assert "'count'" not in "".join(ln for ln in lines if ln.startswith("s.check("))


def test_every_deck_in_a_ladder_is_compared_and_none_without_checkpoints():
    res = {"ok": True, "levels": [{"case_dir": "/tmp/a", "deck": DECK},
                                  {"case_dir": "/tmp/b", "deck": dict(DECK, digest="sha256:" + "b" * 64)}]}
    journal = [E(1, "cfd_mesh_independence_submit", {}, {"job_id": "job_1", "status": "running"}),
               E(2, "job_result", {"job_id": "job_1"}, {"job_id": "job_1", "status": "done", "result": res})]
    script = R.export(journal)["script"]
    assert "('result', 'levels', 0, 'deck')" in script and "('result', 'levels', 1, 'deck')" in script
    assert "s.deck(" not in R.export(_solve_journal(), checkpoints=False)["script"]


class _Stub:
    def _cleanup(self):
        pass


def test_session_deck_reports_and_only_raises_when_asked():
    s = R.Session(server=_Stub())
    now = {"result": {"deck": dict(DECK)}}
    assert s.deck(now, ("result", "deck"), DECK) == []
    moved = {"result": {"deck": dict(DECK, digest="sha256:" + "c" * 64,
                                     files=dict(DECK["files"], **{"case.sif": "ffffffffffffffff"}))}}
    assert s.deck(moved, ("result", "deck"), DECK) == ["~ case.sif"]
    try:
        s.deck(moved, ("result", "deck"), DECK, strict=True)
    except R.ReplayError as e:
        assert "case.sif" in str(e)
    else:
        raise AssertionError("strict deck comparison accepted a changed deck")
    assert s.deck({"result": {}}, ("result", "deck"), DECK) == ["no deck on this run"]


# --- the wiring ---------------------------------------------------------------------------

# Functions that launch a solver but hand it no case directory, and why each is exempt.
EXEMPT = {
    "_run_optics_gpl": "KrakenOS: the input is a JSON problem on stdin, built from recorded args",
    "_run_em_fullwave_gpl": "openEMS: a JSON problem on stdin; the runner makes its own sim dir",
    "_run_dem_gpl": "YADE: a JSON problem on stdin",
    "_run_bempp": "Bempp: a JSON problem on stdin",
    "_yade_exec": "resolves and ping-probes the yade executable; not a solve",
}
_LAUNCH = {("subprocess", "run"), ("subprocess", "Popen"), ("solvers", "run_argvs")}


def _own_calls(fn):
    nested = [n for n in ast.walk(fn) if isinstance(n, ast.FunctionDef) and n is not fn]
    skip = {id(x) for n in nested for x in ast.walk(n)}
    return {(n.func.value.id, n.func.attr) for n in ast.walk(fn)
            if id(n) not in skip and isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)}


def test_every_solver_launch_records_its_deck():
    """A new family that launches a solver without recording what it handed it fails
    here, rather than shipping results with no deck. Adding to EXEMPT needs a reason."""
    tree = ast.parse((PKG / "worker.py").read_text(encoding="utf-8"))
    missing, seen = [], set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        own = _own_calls(fn)
        if not own & _LAUNCH:
            continue
        seen.add(fn.name)
        if ("_cases", "snapshot") not in own and fn.name not in EXEMPT:
            missing.append(f"{fn.name} (line {fn.lineno})")
    assert not missing, f"solver launches that record no deck: {missing}"
    assert not set(EXEMPT) - seen, f"stale exemptions (no longer launch): {set(EXEMPT) - seen}"
    for chokepoint in ("_run_solver", "_run_foam", "_fem_ccx_exec"):
        assert chokepoint in seen, f"{chokepoint} no longer launches — sweep is stale"
    fsi = ast.parse((PKG / "analysis" / "fsi_case.py").read_text(encoding="utf-8"))
    run = next(n for n in ast.walk(fsi) if isinstance(n, ast.FunctionDef) and n.name == "run_coupled_fsi")
    assert ("_cases", "snapshot") in _own_calls(run), "the FSI solve records no deck"


def test_the_worker_gives_synchronous_requests_a_job_boundary():
    src = (PKG / "worker.py").read_text(encoding="utf-8")
    assert "_cases.forget()" in src and "_cases.attach(HANDLERS[method](params))" in src
    assert "only=[os.path.basename(inp)]" in src, "FEM must hash its .inp, not the workdir"
    assert "r = _attach_decks(r)" in (PKG / "jobs.py").read_text(encoding="utf-8")


# --- worker tier: CalculiX FEM ----------------------------------------------------------------

_STEEL = {"Name": "Steel", "YoungsModulus": "210000 MPa", "PoissonRatio": "0.30",
          "Density": "7900 kg/m^3"}


def _fem_analysis(w, pressure=1.0):
    w.call("new_document", name="deck")
    box = w.call("add_primitive", kind="box", w=120, d=12, h=6)
    fixed = w.call("query_faces", handle=box["handle"],
                   predicate={"type": "planar", "normal_dir": [-1, 0, 0]})
    top = w.call("query_faces", handle=box["handle"],
                 predicate={"type": "planar", "normal_dir": [0, 0, 1]})
    an = w.call("fem_new_analysis", name="A")["handle"]
    w.call("fem_set_solver", analysis=an, kind="ccx", tunables={"GeometricalNonlinearity": "linear"})
    w.call("fem_set_material", analysis=an, body=box["handle"], material=dict(_STEEL))
    w.call("fem_add_constraint", analysis=an, kind="fixed",
           refs=[{"handle": box["handle"], "tag": fixed[0]["tag"]}])
    w.call("fem_add_constraint", analysis=an, kind="pressure",
           refs=[{"handle": box["handle"], "tag": top[0]["tag"]}], pressure=pressure)
    w.call("fem_mesh", analysis=an, body=box["handle"], char_length=6.0)
    return an


def worker_test_fem_decks():
    from ankusdrive import Worker
    with Worker() as w:
        an = _fem_analysis(w)
        first = w.call("fem_run", analysis=an)
        time.sleep(1.2)                                 # a different second on the .inp's clock
        sub = w.call("fem_run_submit", analysis=an)
        while w.call("job_status", job_id=sub["job_id"])["status"] == "running":
            time.sleep(0.2)
        second = w.call("job_result", job_id=sub["job_id"])["result"]
    assert list(first["deck"]["files"]) == ["Mesh.inp"], first["deck"]
    assert first["deck"]["digest"] == second["deck"]["digest"], \
        "the same analysis, run sync then async seconds apart, reported different decks"
    with Worker() as w:
        other = w.call("fem_run", analysis=_fem_analysis(w, pressure=2.0))
    assert C.diff(first["deck"], other["deck"]) == ["~ Mesh.inp"]


# --- solver tier: Elmer, and the transcript --------------------------------------------------

_SLAB = dict(half_thickness_mm=1.5, h_conv=8.0, duration_s=60.0, k=0.2, rho=1040.0,
             cp=1400.0, t_initial_c=200.0, n_steps=10)


def _solve(**kw):
    from ankusdrive import Worker
    with Worker() as w:                                  # fresh worker: no job-cache hit
        sub = w.call("thermal_transient_submit", **kw)
        while w.call("job_status", job_id=sub["job_id"])["status"] == "running":
            time.sleep(0.2)
        return w.call("job_result", job_id=sub["job_id"])["result"]


def solver_test_elmer_decks_agree_and_a_change_names_its_file():
    a, b = _solve(**_SLAB), _solve(**_SLAB)
    assert a["deck"]["digest"] == b["deck"]["digest"], "two identical Elmer solves disagreed"
    assert "case.sif" in a["deck"]["files"], a["deck"]
    assert not any(f.startswith("log") or f.endswith(".names") or f.endswith(".dat")
                   for f in a["deck"]["files"]), f"solver outputs in the deck: {a['deck']['files']}"
    assert C.diff(a["deck"], _solve(**dict(_SLAB, h_conv=12.0))["deck"]) == ["~ case.sif"]


def solver_test_a_drifted_replay_is_explained_first():
    import asyncio
    os.environ.setdefault("ANKUSDRIVE_TOOLSETS", "all")
    from ankusdrive import journal, mcp_server
    call = mcp_server.mcp._tool_manager.call_tool

    def run(tool, **args):
        r = asyncio.run(call(tool, args))
        r = r[-1] if isinstance(r, tuple) else r
        return r["result"] if isinstance(r, dict) and set(r) == {"result"} else r

    journal.clear()
    try:
        sub = run("thermal_transient_submit", **_SLAB)
        while run("job_status", job_id=sub["job_id"])["status"] == "running":
            time.sleep(0.2)
        run("job_result", job_id=sub["job_id"])
        script = mcp_server.mcp._tool_manager._tools["session_transcript"].fn(provenance=False)["script"]
    finally:
        mcp_server._cleanup()
        journal.clear()
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "PYTHONPATH": str(REPO), "ANKUSDRIVE_TOOLSETS": "all"}
        same = Path(tmp) / "same.py"
        same.write_text(script, encoding="utf-8")
        p = subprocess.run([sys.executable, str(same), str(Path(tmp) / "a")], env=env,
                           capture_output=True, text=True, timeout=600)
        assert p.returncode == 0 and "deck matches the recording" in p.stdout, p.stdout + p.stderr
        drift = Path(tmp) / "drift.py"
        drift.write_text(script.replace("h_conv=8.0", "h_conv=12.0"), encoding="utf-8")
        p = subprocess.run([sys.executable, str(drift), str(Path(tmp) / "b")], env=env,
                           capture_output=True, text=True, timeout=600)
    assert p.returncode != 0, "a replay with a different h_conv passed its checks"
    out = p.stdout
    assert "deck DIFFERS" in out and "~ case.sif" in out, out
    assert "t_center_c" in p.stderr, "the failure was not the temperature check"


# --- runner ------------------------------------------------------------------------------------

def _freecad_available() -> bool:
    try:
        from ankusdrive import client
        path = client._resolve_freecadcmd()
        return bool(path and Path(path).exists())
    except Exception:
        return False


def _available(name) -> bool:
    try:
        from ankusdrive import solvers
        return bool(solvers.find_solver(name)["available"])
    except Exception:
        return False


def _tests():
    g = globals()
    out = [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]
    if not _freecad_available():
        print("  SKIP worker + solver tiers — freecadcmd not resolvable")
        return out
    out.append(("worker_test_fem_decks", worker_test_fem_decks))
    if not _available("elmer"):
        print("  SKIP solver tier — ElmerSolver not installed")
    elif importlib.util.find_spec("mcp") is None:
        out.append(("solver_test_elmer_decks_agree_and_a_change_names_its_file",
                    solver_test_elmer_decks_agree_and_a_change_names_its_file))
        print(f"  SKIP transcript replay — `mcp` not importable in {sys.executable}")
    else:
        out.append(("solver_test_elmer_decks_agree_and_a_change_names_its_file",
                    solver_test_elmer_decks_agree_and_a_change_names_its_file))
        out.append(("solver_test_a_drifted_replay_is_explained_first",
                    solver_test_a_drifted_replay_is_explained_first))
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
