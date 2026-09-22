"""The durable journal (#433): the session journal on disk, opt-in, bounded, exportable.

``ankusdrive/journal_store.py`` appends every tool call the in-memory journal records
(#407) to one JSONL file per session when ``ANKUSDRIVE_JOURNAL_DIR`` is set, and
``journal_export`` / ``ankusdrive journal export`` turn such a file back into the
record ``session_transcript`` gives. Three tiers, each skipping (and saying so) when
its prerequisite is missing:

  * **static** (stdlib, fast lane) — the journal against stub tools: off by default
    writes nothing anywhere; on, a header then one line per call in ``seq`` order with a
    digest of the *full* result; ``run_script`` code verbatim plus its SHA-256 on disk;
    an unwritable directory never breaks (or changes) a tool call and is logged;
    retention caps reap oldest-first but never the live file, a file in its grace
    window, or a foreign file; the per-session size cap keeps arguments and digests;
    redaction hashes paths and names but never ``run_script`` code or handles; the
    config file's ``journal_dir`` / ``[journal] dir`` spellings; and a journal file
    exports to exactly the script the in-memory journal would have produced — also
    through the CLI.
  * **server** (needs ``mcp``) — ``journal_export`` is registered READ_ONLY in ``core``
    with no write-target parameter; calls made through FastMCP land on disk; and for
    the live session, ``journal_export`` returns the same script as
    ``session_transcript``.

Run:  .venv/bin/python3 tests/test_journal_durable.py
"""
import ast
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "ankusdrive"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))   # the shared _mcp_python helper
from _mcp_python import HOST_DEPS, ensure_mcp_interpreter, mcp_skip_reason  # noqa: E402

from ankusdrive import journal, journal_store, replay  # noqa: E402

_VARS = ("ANKUSDRIVE_JOURNAL_DIR", "ANKUSDRIVE_JOURNAL_REDACT", "ANKUSDRIVE_JOURNAL_KEEP",
         "ANKUSDRIVE_JOURNAL_MAX_MB", "ANKUSDRIVE_JOURNAL_FILE_MAX_MB",
         "ANKUSDRIVE_JOURNAL_GRACE_S", "ANKUSDRIVE_CONFIG")


class _Env:
    """A scratch directory, a private (absent) config file, the journal vars cleared,
    and fresh journal state. ``dir=True`` also turns the durable journal on there."""

    def __init__(self, on=True, **env):
        self.on, self.env = on, env

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="journal_durable_")
        self.dir = os.path.join(self.tmp, "journal")
        self.prev = {v: os.environ.get(v) for v in _VARS}
        for v in _VARS:
            os.environ.pop(v, None)
        os.environ["ANKUSDRIVE_CONFIG"] = os.path.join(self.tmp, "config.toml")
        if self.on:
            os.environ["ANKUSDRIVE_JOURNAL_DIR"] = self.dir
        for k, v in self.env.items():
            os.environ[k] = str(v)
        journal.clear()
        journal_store._reset_for_tests()
        return self

    def __exit__(self, *exc):
        for v, val in self.prev.items():
            if val is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = val
        journal.clear()
        journal_store._reset_for_tests()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lines(self):
        files = [f for f in os.listdir(self.dir) if f.startswith("session-")]
        assert len(files) == 1, files
        with open(os.path.join(self.dir, files[0]), encoding="utf-8") as fh:
            return [json.loads(x) for x in fh if x.strip()]

    def file(self):
        return journal_store.status()["file"]


class _Stub:
    """A FastMCP stand-in: ``_tool_manager._tools`` of objects with ``fn``."""

    def __init__(self, **fns):
        self._tool_manager = SimpleNamespace(
            _tools={n: SimpleNamespace(fn=f) for n, f in fns.items()})
        self.ws, self.pid = "default", 100

    def apply(self):
        return journal.apply(self, workspace_of=lambda: self.ws,
                             worker_pid_of=lambda _ws: self.pid,
                             freecad_of=lambda _ws: ["1", "1", "0", "R1", "main"])

    def call(self, _tool, **kw):
        return self._tool_manager._tools[_tool].fn(**kw)


def _cad_stub():
    """Tools shaped like the real ones (names and results), no FreeCAD."""
    count = {"h": 0}

    def add_primitive(kind="box", w=10.0, d=10.0, h=10.0, r=5.0, placement=None, name=None):
        count["h"] += 1
        return {"handle": f"h{count['h']}", "volume": w * d * h}

    def mass_properties(handle, density=None):
        return {"handle": handle, "volume": 4000.0, "area": 2200.0}

    def run_script(code, timeout=60.0):
        return {"result": 42, "registered": []}

    def h_estimate(**kw):
        return {"h_w_m2k": 7.9, "fidelity": "correlation", "band_pct": 20.0}

    def render_view(handle, **kw):
        return {"png_base64": "A" * 5000}

    def save_document(path, visibility_hygiene=True):
        return {"saved": path}

    def boom(**kw):
        raise RuntimeError("cannot open /home/alice/client_x/secret.FCStd")

    stub = _Stub(add_primitive=add_primitive, mass_properties=mass_properties,
                 run_script=run_script, h_estimate=h_estimate, render_view=render_view,
                 save_document=save_document, open_document=boom)

    def use_workspace(name):
        stub.ws = name
        return {"workspace": name}
    stub._tool_manager._tools["use_workspace"] = SimpleNamespace(fn=use_workspace)
    stub.apply()
    return stub


CODE = "import math\n__result__ = {'t': 13.2 * math.exp(0.26)}  # the load-bearing step\n"


def _session(stub):
    box = stub.call("add_primitive", kind="box", w=40.0, d=20.0, h=5.0)
    stub.call("mass_properties", handle=box["handle"])
    stub.call("h_estimate", geometry="vertical_plate", length_m=0.1, delta_t_k=175.0)
    stub.call("run_script", code=CODE)
    stub.call("render_view", handle=box["handle"])
    stub.call("save_document", path="/home/alice/client_x/bracket.FCStd")
    try:
        stub.call("open_document", path="/home/alice/client_x/secret.FCStd")
    except RuntimeError:
        pass
    return box


# --- static -------------------------------------------------------------------------

def test_off_by_default_writes_nothing():
    with _Env(on=False) as env:
        stub = _cad_stub()
        _session(stub)
        assert journal.snapshot()["count"] == 7, "the in-memory journal still records"
        assert journal_store.journal_dir() is None
        assert os.listdir(env.tmp) == [], f"wrote {os.listdir(env.tmp)} with the journal off"
        assert journal_store.status()["session"] is None, "no session minted while off"
        assert journal_store.append({"seq": 1, "tool": "x", "ok": True}, {}) is False


def test_enabled_writes_header_then_calls_in_order():
    with _Env() as env:
        stub = _cad_stub()
        _session(stub)
        lines = env.lines()
        head, rest = lines[0], lines[1:]
        assert head["type"] == "header" and head["format"] == journal_store.FORMAT
        assert head["env"]["ankusdrive"] and head["env"]["python"], head["env"]
        assert "solvers" not in head["env"], "no solver probes on the tool-call path"
        sid = head["session"]
        assert os.path.basename(env.file()) == f"session-{sid}.jsonl"
        workers = [x for x in rest if x["type"] == "worker"]
        calls = [x for x in rest if x["type"] == "call"]
        assert len(workers) == 1 and workers[0]["freecad"][:3] == ["1", "1", "0"], workers
        assert [c["seq"] for c in calls] == list(range(1, 8)), [c["seq"] for c in calls]
        assert [c["tool"] for c in calls] == [e["tool"] for e in journal.snapshot()["entries"]]
        assert all(c["session"] == sid and c["ts"].endswith("Z") for c in calls)
        render = calls[4]
        assert render["result"]["png_base64"]["__elided__"] == "str", "stored result is compacted"
        full = {"png_base64": "A" * 5000}
        assert render["result_digest"] == journal_store.digest(full), \
            "the digest must be over the full, uncompacted result"
        expected = "sha256:" + hashlib.sha256(json.dumps(
            full, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert render["result_digest"] == expected
        failed = calls[6]
        assert not failed["ok"] and "result_digest" not in failed and "secret" in failed["error"]
        # the memory entry is untouched by the disk path
        assert "result_digest" not in journal.snapshot()["entries"][0]


def test_run_script_code_verbatim_and_hashed_on_disk():
    with _Env() as env:
        stub = _cad_stub()
        stub.call("run_script", code=CODE)
        call = [x for x in env.lines() if x["type"] == "call"][0]
        assert call["args"]["code"] == CODE, "run_script code must be on disk verbatim"
        assert call["code_sha256"] == "sha256:" + hashlib.sha256(CODE.encode()).hexdigest()


def test_unwritable_dir_never_breaks_a_tool_call():
    with _Env(on=False) as env:
        blocker = os.path.join(env.tmp, "a_file")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        os.environ["ANKUSDRIVE_JOURNAL_DIR"] = os.path.join(blocker, "journal")
        seen = []

        class _H(logging.Handler):
            def emit(self, rec):
                seen.append(rec.getMessage())
        h = _H()
        journal_store.log.addHandler(h)
        try:
            stub = _cad_stub()
            box = stub.call("add_primitive", kind="box", w=1.0, d=2.0, h=3.0)
            assert box == {"handle": "h1", "volume": 6.0}, box
            try:
                stub.call("open_document", path="x")
            except RuntimeError as e:
                assert "secret" in str(e), "the tool's own error must come through unchanged"
            else:
                raise AssertionError("the raising tool did not raise")
            for _ in range(3):
                stub.call("mass_properties", handle="h1")
        finally:
            journal_store.log.removeHandler(h)
        assert journal.snapshot()["count"] == 5, "the memory journal is unaffected"
        assert journal_store.status()["errors"] == 5
        assert len(seen) == 1 and "could not write" in seen[0], seen   # logged, not spammed


def _fake_sessions(where, n, *, age_s, size=100, start=0):
    os.makedirs(where, exist_ok=True)
    now = time.time()
    out = []
    for i in range(n):
        p = os.path.join(where, f"session-20260101T00000{i % 10}Z-{start + i}-abcdef.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("x" * size)
        t = now - age_s - (n - i) * 10          # later i = newer
        os.utime(p, (t, t))
        out.append(p)
    return out


def test_rotation_caps_oldest_first_never_live_or_in_grace():
    with _Env() as env:
        old = _fake_sessions(env.dir, 5, age_s=7200)
        foreign = os.path.join(env.dir, "notes.jsonl")
        with open(foreign, "w", encoding="utf-8") as fh:
            fh.write("mine")
        os.utime(foreign, (0, 0))
        r = journal_store.reap(env.dir, live=old[0], keep=2, grace_s=3600)
        left = sorted(os.listdir(env.dir))
        assert os.path.exists(old[0]), "the live file is never reaped, even when oldest"
        assert os.path.exists(old[4]) and not os.path.exists(old[3]), left
        assert r["removed"] == 3 and r["kept"] == 2, r
        assert os.path.exists(foreign), "a file the journal did not name is left alone"
        # grace: recent files survive any cap
        recent = _fake_sessions(env.dir, 3, age_s=0, start=50)
        r = journal_store.reap(env.dir, keep=1, grace_s=3600)
        assert all(os.path.exists(p) for p in recent) and r["skipped_in_grace"] >= 2, r
        # the byte cap
        shutil.rmtree(env.dir)
        big = _fake_sessions(env.dir, 4, age_s=7200, size=400_000)
        r = journal_store.reap(env.dir, keep=100, max_mb=1.0, grace_s=3600)
        assert [os.path.exists(p) for p in big] == [False, False, True, True], r


def test_rotation_runs_when_a_session_opens_and_env_tunes_it():
    with _Env(ANKUSDRIVE_JOURNAL_KEEP=3) as env:
        old = _fake_sessions(env.dir, 6, age_s=7200)
        stub = _cad_stub()
        stub.call("add_primitive")
        files = sorted(f for f in os.listdir(env.dir) if f.startswith("session-"))
        assert len(files) == 3, files
        assert os.path.basename(env.file()) in files, "the live session survives its own reap"
        assert all(not os.path.exists(p) for p in old[:4]) and os.path.exists(old[5])


def test_file_disappearing_rewrites_the_header():
    with _Env() as env:
        stub = _cad_stub()
        stub.call("add_primitive")
        os.remove(env.file())
        stub.call("add_primitive")
        lines = env.lines()
        assert lines[0]["type"] == "header" and lines[0].get("reopened") is True, lines[0]
        assert [x["seq"] for x in lines if x["type"] == "call"] == [2]


def test_per_session_cap_keeps_args_and_digest():
    with _Env(ANKUSDRIVE_JOURNAL_FILE_MAX_MB="0.001") as env:     # ~1 KB
        stub = _cad_stub()
        for _ in range(8):
            stub.call("run_script", code=CODE)
        calls = [x for x in env.lines() if x["type"] == "call"]
        elided = [c for c in calls if c.get("result_elided")]
        assert elided, "the cap never engaged"
        for c in elided:
            assert "result" not in c and c["args"]["code"] == CODE and c["result_digest"]
        out = journal_store.export(env.file().split("session-")[1][:-6], provenance=False)
        assert any("size cap" in w for w in out["warnings"]), out["warnings"]


def test_redaction_hashes_names_and_paths_but_never_code():
    with _Env(ANKUSDRIVE_JOURNAL_REDACT="1") as env:
        stub = _cad_stub()
        _session(stub)
        stub.call("add_primitive", kind="box", w=10.0, name="AcmeClientBracket")
        calls = {c["seq"]: c for c in env.lines() if c["type"] == "call"}
        assert env.lines()[0]["journal"]["redact"] is True
        rs = calls[4]
        assert rs["args"]["code"] == CODE, "run_script code is never redacted"
        assert rs["code_sha256"] == "sha256:" + hashlib.sha256(CODE.encode()).hexdigest()
        save = calls[6]
        path = "/home/alice/client_x/bracket.FCStd"
        want = "redacted:sha256:" + hashlib.sha256(path.encode()).hexdigest()
        assert save["args"]["path"] == want, save["args"]
        assert save["result"]["saved"] == want, "the same value hashes the same in a result"
        assert "alice" not in json.dumps(calls[7]), calls[7]["error"]
        assert calls[8]["args"]["name"].startswith("redacted:sha256:")
        assert calls[8]["args"]["kind"] == "box" and calls[8]["args"]["w"] == 10.0
        assert calls[2]["args"]["handle"] == "h1", "handles are replay linkage, not names"
        assert "alice" not in Path(env.file()).read_text(encoding="utf-8")
        # the digest pins the *unredacted* result
        assert save["result_digest"] == journal_store.digest({"saved": path})
        out = journal_store.export("latest", provenance=False)
        assert out["redacted"] and any("not a runnable replay" in w for w in out["warnings"])
        assert CODE.strip().splitlines()[0] in out["script"]


def test_config_file_spellings():
    with _Env(on=False) as env:
        cfg = os.environ["ANKUSDRIVE_CONFIG"]
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(f'journal_dir = "{env.dir.replace(os.sep, "/")}"\n')
        assert journal_store.journal_dir() == os.path.abspath(env.dir)
        time.sleep(0.01)
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(f'[journal]\ndir = "{env.dir.replace(os.sep, "/")}"\nredact = true\n'
                     f'keep = 7\n')
        os.utime(cfg, (time.time() + 5, time.time() + 5))     # defeat the mtime cache
        assert journal_store.journal_dir() == os.path.abspath(env.dir)
        assert journal_store.redacting() is True and journal_store.caps()["keep"] == 7
        os.environ["ANKUSDRIVE_JOURNAL_REDACT"] = "0"
        assert journal_store.redacting() is False, "env overrides the config file"
        from ankusdrive import config
        assert config._config_key("ANKUSDRIVE_JOURNAL_DIR") == ("journal_dir",)


def _expected_script(ws, provenance=False):
    """What session_transcript builds from the in-memory journal, minus the server."""
    snap = journal.snapshot(ws)
    prov = None
    if provenance:
        from ankusdrive import provenance as p
        prov = p.from_entries(snap["entries"], freecad=None)
    return replay.export(snap["entries"], workspace=ws, truncated=snap["truncated"],
                         provenance=prov)["script"]


def test_export_round_trips_to_the_transcript_script():
    with _Env() as env:
        stub = _cad_stub()
        _session(stub)
        stub.call("use_workspace", name="w2")
        stub.call("add_primitive", kind="cylinder", r=3.0, h=9.0)
        path = env.file()
        sid = os.path.basename(path)[len("session-"):-len(".jsonl")]
        out = journal_store.export(sid, provenance=False)
        assert out["workspace"] == "w2", "default: the workspace current when it ended"
        assert out["script"] == _expected_script("w2")
        out = journal_store.export(sid, workspace="default", provenance=False)
        assert out["script"] == _expected_script("default"), "round trip differs"
        assert CODE in out["script"] or CODE.splitlines()[0] in out["script"]
        assert [r["seq"] for r in out["ledger"]] == list(range(1, 10))
        assert out["ledger"][3]["code_sha256"].startswith("sha256:")
        assert all(r.get("result_digest") for r in out["ledger"] if r["ok"])
        assert out["integrity"] == {"calls": 9, "gaps": [], "bad_lines": 0, "elided": 0}
        assert out["env"]["ankusdrive"] and not out["truncated"]
        # provenance: the recorded env + this machine's solver identities
        out = journal_store.export(sid, workspace="default", provenance=True)
        assert out["provenance"]["env"]["freecad"]["version"] == "1.1.0", out["provenance"]
        # a torn last line and a missing seq are reported, not fatal
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"type": "call", "seq": 12, "tool": "ping", "ok": true, "args": {}, '
                     '"workspace": "default", "worker_pid": 100, "txn_depth": 0}\n{"type": "ca')
        out = journal_store.export(sid, workspace="default", provenance=False)
        assert out["integrity"]["bad_lines"] == 1 and out["integrity"]["gaps"] == [[10, 11]]
        assert out["truncated"] and any("missing" in w for w in out["warnings"])


def test_export_rejects_anything_but_a_session_id():
    with _Env():
        for bad in ("../etc/passwd", "/tmp/x.jsonl", "session-x", ""):
            try:
                journal_store.export(bad)
            except ValueError:
                continue
            raise AssertionError(f"export accepted {bad!r}")


def test_cli_export_matches():
    from ankusdrive import cli
    with _Env() as env:
        stub = _cad_stub()
        _session(stub)
        out_py = os.path.join(env.tmp, "t.py")
        cli.main(["journal", "export", env.file(), "-o", out_py, "--no-provenance",
                  "--workspace", "default"])
        assert Path(out_py).read_text(encoding="utf-8") == _expected_script("default")
        out_json = os.path.join(env.tmp, "t.json")
        cli.main(["journal", "export", "latest", "--json", "-o", out_json,
                  "--no-provenance"])
        rec = json.loads(Path(out_json).read_text(encoding="utf-8"))
        assert rec["ledger"] and rec["env"] and rec["script"]
        ast.parse(rec["script"])


def test_journal_py_itself_still_writes_nothing():
    tree = ast.parse((PKG / "journal.py").read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            name = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            assert name not in {"open", "write_text", "write_bytes", "dump", "mkdir"}, name


# --- server (needs mcp) -------------------------------------------------------------

def server_test_journal_export_registered_read_only_core():
    src = (PKG / "mcp_server.py").read_text(encoding="utf-8")
    fns = {n.name: n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)}
    fn = fns["journal_export"]
    for a in fn.args.args + fn.args.kwonlyargs:
        assert not any(w in a.arg for w in ("path", "file", "dir", "out")), a.arg
    from ankusdrive import tool_annotations as ta, toolsets as ts
    assert "journal_export" in ta.READ_ONLY and ta.hints_for("journal_export")["readOnlyHint"]
    assert "journal_export" in ts.FAMILIES["core"]
    assert replay.SKIP_TOOLS.get("journal_export"), "exports must not replay themselves"


def server_test_live_session_export_matches_session_transcript():
    import asyncio
    os.environ.setdefault("ANKUSDRIVE_TOOLSETS", "all")
    from ankusdrive import mcp_server
    tools = mcp_server.mcp._tool_manager._tools
    with _Env() as env:
        listing = tools["journal_export"].fn()
        assert listing["enabled"] and listing["sessions"] == [], listing
        asyncio.run(mcp_server.mcp._tool_manager.call_tool("list_workspaces", {}))
        try:
            asyncio.run(mcp_server.mcp._tool_manager.call_tool("use_workspace", {"name": ""}))
        except Exception:
            pass
        sid = journal_store.status()["session"]
        listing = tools["journal_export"].fn()
        assert [s["session"] for s in listing["sessions"]] == [sid] and \
            listing["sessions"][0]["live"] and "file" not in listing["sessions"][0]
        # unjournaled, so the transcript call itself is not one of the calls compared
        want = tools["session_transcript"].fn.__wrapped__(provenance=False)["script"]
        got = tools["journal_export"].fn(session=sid, provenance=False)
        assert got["script"] == want, "journal_export differs from session_transcript"
        assert "file" not in got, "the MCP result does not name a local path"
        tools = [x["tool"] for x in env.lines() if x["type"] == "call"]
        assert tools[:3] == ["journal_export", "list_workspaces", "use_workspace"], tools


def _tests():
    g = globals()
    out = [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]
    if importlib.util.find_spec("mcp") is None:
        print(f"  SKIP server tier — {mcp_skip_reason()}")
        return out
    out += [(n, g[n]) for n in sorted(g) if n.startswith("server_test_")]
    return out


def main():
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
            print(f"  FAIL {name:56s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:56s} ({time.time() - t0:.2f}s)")
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
