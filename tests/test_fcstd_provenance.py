"""Provenance attached to a saved .FCStd (#462).

``save_document(..., attach_provenance=True)`` embeds the ``journal_export`` record
(script + environment + ledger digests) for the calls that built the saved document in
the document's ``Meta`` map; ``ankusdrive journal export file.FCStd`` reads it back.
Tiers, each skipping (and saying so) when its prerequisite is missing:

  * **static** (stdlib) — which calls belong to the model: from the ``new_document`` /
    ``open_document`` that produced it, only while it was active, only the saving
    workspace and its current worker, never a failed call; the fallback when no call
    produced it; redaction honoured (paths and names hashed, ``run_script`` code kept,
    digests of the unredacted results); every in-memory entry carries the full
    result's digest; :func:`fcstd_provenance.read` and the CLI read a record from a
    zip without FreeCAD, and say so when a file has none.
  * **worker** (needs ``mcp`` and FreeCAD) — a real session through the MCP tools:
    saved with the flag, the file carries a record of that document's calls only (not
    another document's, not another workspace's, not a failed call); saved without
    it, none — and re-saving an attached file without it removes the record; FreeCAD
    reopening and re-saving the file keeps it; ``ANKUSDRIVE_JOURNAL_REDACT`` hashes the
    paths in it; a STEP path with the flag fails loudly.

Run:  .venv/bin/python3 tests/test_fcstd_provenance.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
import zipfile
from pathlib import Path
from types import SimpleNamespace
from xml.sax.saxutils import quoteattr

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))   # the shared _mcp_python helper
from _mcp_python import HOST_DEPS, ensure_mcp_interpreter, mcp_skip_reason  # noqa: E402

from ankusdrive import fcstd_provenance as fp, journal, journal_store  # noqa: E402

CODE = "__result__ = {'k': 2.5}  # the load-bearing step\n"
SECRET = "/srv/alice/client_x/bracket.FCStd"


class _Stub:
    """Tools shaped like the real ones, journaled exactly as the server journals them."""

    def __init__(self):
        self.ws, self.pid, self.active, self.n = "default", 100, None, 0

        def new_document(name="part"):
            self.active = name
            return {"doc": name}

        def open_document(path):
            self.active = os.path.splitext(os.path.basename(path))[0]
            return {"doc": self.active, "objects": []}

        def set_active_document(name):
            self.active = name
            return {"active": name}

        def add_primitive(kind="box", **kw):
            self.n += 1
            return {"handle": f"h{self.n}", "volume": 1.0 * self.n}

        def run_script(code, timeout=60.0):
            return {"result": 2.5, "registered": []}

        def boom(**kw):
            raise RuntimeError("no such handle")

        def use_workspace(name):
            self.ws = name
            return {"workspace": name}

        fns = dict(new_document=new_document, open_document=open_document,
                   set_active_document=set_active_document, add_primitive=add_primitive,
                   run_script=run_script, fillet_edges=boom, use_workspace=use_workspace)
        self._tool_manager = SimpleNamespace(
            _tools={n: SimpleNamespace(fn=f) for n, f in fns.items()})
        journal.apply(self, workspace_of=lambda: self.ws, worker_pid_of=lambda _ws: self.pid)

    def call(self, _tool, **kw):
        try:
            return self._tool_manager._tools[_tool].fn(**kw)
        except RuntimeError:
            return None


def _session():
    journal.clear()
    s = _Stub()
    s.call("add_primitive", kind="box", w=1)              # before any document: earlier
    s.call("new_document", name="A")                      # anchor
    s.call("add_primitive", kind="box", w=40)
    s.call("fillet_edges", handle="h9", radius=1)         # failed
    s.call("new_document", name="B")                      # another document
    s.call("add_primitive", kind="cylinder", r=7)
    s.call("set_active_document", name="A")
    s.call("run_script", code=CODE)
    s.call("use_workspace", name="w2")                    # another workspace
    s.call("add_primitive", kind="sphere", r=3)
    s.call("use_workspace", name="default")
    s.call("add_primitive", kind="cone", r=2)
    return s


def _entries():
    return journal.snapshot()["entries"]


# --- static -------------------------------------------------------------------------

def test_selection_is_this_documents_calls():
    _session()
    rec = fp.build(_entries(), doc="A", workspace="default", worker_pid=100, redact=False)
    tools = [(r["seq"], r["tool"]) for r in rec["ledger"]]
    assert tools == [(2, "new_document"), (3, "add_primitive"), (7, "set_active_document"),
                     (8, "run_script"), (12, "add_primitive")], tools
    sc = rec["scope"]
    assert sc["anchor"] == {"seq": 2, "tool": "new_document"}, sc
    assert sc["excluded_failed"] == 1 and sc["excluded_other_document"] == 3, sc
    assert "r=7" not in rec["script"] and "r=3" not in rec["script"], rec["script"]
    assert "w=40" in rec["script"] and CODE.splitlines()[0] in rec["script"]
    assert "fillet_edges" not in rec["script"], "a failed call is not in the script"
    assert rec["format"] == fp.FORMAT and rec["document"] == "A" and not rec["redacted"]
    compile(rec["script"], "<fcstd provenance>", "exec")
    # every kept call pins its full result
    assert all(r["result_digest"].startswith("sha256:") for r in rec["ledger"]), rec["ledger"]
    assert rec["ledger"][3]["code_sha256"].startswith("sha256:")
    # and the other document gets its own calls only
    b = fp.build(_entries(), doc="B", workspace="default", worker_pid=100, redact=False)
    assert [r["tool"] for r in b["ledger"]] == ["new_document", "add_primitive"], b["ledger"]


def test_worker_restart_and_fallback():
    s = _session()
    s.pid = 200                                           # a fresh worker
    s.call("add_primitive", kind="box", w=77)
    rec = fp.build(_entries(), doc="A", workspace="default", worker_pid=200, redact=False)
    assert [r["tool"] for r in rec["ledger"]] == ["add_primitive"], rec["ledger"]
    assert rec["scope"]["anchor"] is None and "w=77" in rec["script"]
    assert "w=40" not in rec["script"], "nothing from before the restart"


def test_memory_entries_carry_the_full_digest():
    _session()
    e = _entries()[2]
    assert e["result_digest"] == journal_store.digest({"handle": "h2", "volume": 2.0}), e
    assert "result_digest" not in _entries()[3], "a failed call has no result to pin"


def test_redaction_honoured():
    s = _session()
    s.call("open_document", path=SECRET)
    prev = os.environ.get("ANKUSDRIVE_JOURNAL_REDACT")
    os.environ["ANKUSDRIVE_JOURNAL_REDACT"] = "1"
    try:
        rec = fp.build(_entries(), doc="bracket", workspace="default", worker_pid=100)
    finally:
        if prev is None:
            os.environ.pop("ANKUSDRIVE_JOURNAL_REDACT", None)
        else:
            os.environ["ANKUSDRIVE_JOURNAL_REDACT"] = prev
    text = fp.dumps(rec)
    assert rec["redacted"] and "alice" not in text and "bracket" not in text, text[:400]
    assert rec["document"].startswith("redacted:sha256:")
    assert any("not a runnable replay" in w for w in rec["warnings"])
    assert rec["ledger"][0]["tool"] == "open_document"
    # the digest still pins the unredacted result
    assert rec["ledger"][0]["result_digest"] == journal_store.digest(
        {"doc": "bracket", "objects": []})


def _fake_fcstd(path, record):
    value = "" if record is None else fp.dumps(record)
    items = "" if record is None else \
        f'<Item key="{fp.META_KEY}" value={quoteattr(value)}/>'
    xml = ("<?xml version='1.0' encoding='utf-8'?>\n<Document SchemaVersion=\"4\">"
           "<Properties Count=\"2\"><Property name=\"Label\" type=\"App::PropertyString\">"
           "<String value=\"A\"/></Property><Property name=\"Meta\" "
           f"type=\"App::PropertyMap\"><Map count=\"1\">{items}</Map></Property>"
           "</Properties><Objects Count=\"0\"/></Document>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Document.xml", xml)


def test_read_and_cli_without_freecad():
    from ankusdrive import cli
    _session()
    rec = fp.build(_entries(), doc="A", workspace="default", worker_pid=100, redact=False)
    tmp = tempfile.mkdtemp(prefix="fcstd_prov_")
    try:
        f = os.path.join(tmp, "a.FCStd")
        _fake_fcstd(f, rec)
        assert fp.read(f) == json.loads(fp.dumps(rec))
        out_py = os.path.join(tmp, "t.py")
        cli.main(["journal", "export", f, "-o", out_py])
        assert Path(out_py).read_text(encoding="utf-8") == rec["script"]
        out_json = os.path.join(tmp, "t.json")
        cli.main(["journal", "export", f, "--json", "-o", out_json])
        assert json.loads(Path(out_json).read_text(encoding="utf-8"))["ledger"] == rec["ledger"]
        bare = os.path.join(tmp, "bare.FCStd")
        _fake_fcstd(bare, None)
        assert fp.read(bare) is None
        try:
            cli.main(["journal", "export", bare])
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("a file with no record must fail the CLI")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        journal.clear()


# --- worker (needs mcp + FreeCAD) ---------------------------------------------------

def worker_test_real_save_and_reopen():
    import asyncio
    os.environ.setdefault("ANKUSDRIVE_TOOLSETS", "all")
    os.environ.pop("ANKUSDRIVE_JOURNAL_REDACT", None)
    from ankusdrive import mcp_server
    call = mcp_server.mcp._tool_manager.call_tool

    def run(_tool, **args):
        return asyncio.run(call(_tool, args))
    tmp = tempfile.mkdtemp(prefix="fcstd_prov_")
    journal.clear()
    try:
        run("new_document", name="ProvA")
        box = run("add_primitive", kind="box", w=31, d=11, h=4)
        try:
            run("fillet_edges", handle=box["handle"], edges=["Edge999"], radius=1)
        except Exception:
            pass
        run("new_document", name="ProvB")
        run("add_primitive", kind="cylinder", r=6.5, h=3)
        run("use_workspace", name="prov_w2")
        run("new_document", name="ProvA")                     # same name, other workspace
        run("add_primitive", kind="sphere", r=2.25)
        run("use_workspace", name="default")
        run("set_active_document", name="ProvA")
        run("add_primitive", kind="box", w=13, d=3, h=5)
        attached = os.path.join(tmp, "attached.FCStd")
        out = run("save_document", path=attached, attach_provenance=True)
        assert out.get("provenance_attached") and out["provenance"]["calls"] >= 4, out
        plain = os.path.join(tmp, "plain.FCStd")
        run("save_document", path=plain)
        try:
            run("save_document", path=os.path.join(tmp, "x.step"), attach_provenance=True)
        except Exception as e:
            assert "FCStd" in str(e), e
        else:
            raise AssertionError("attach_provenance to a STEP path must fail")

        rec = fp.read(attached)
        assert rec is not None and fp.read(plain) is None
        s = rec["script"]
        compile(s, "<fcstd provenance>", "exec")
        assert "w=31" in s and "w=13" in s, s
        assert "r=6.5" not in s, "another document's calls leaked in"
        assert "r=2.25" not in s, "another workspace's calls leaked in"
        assert "fillet_edges" not in s, "a failed call leaked in"
        assert rec["provenance"]["env"]["freecad"], rec["provenance"]["env"]
        assert rec["scope"]["anchor"]["tool"] == "new_document"

        # FreeCAD reopening and re-saving keeps it; our save without the flag drops it
        run("open_document", path=attached)
        again = os.path.join(tmp, "resaved.FCStd")
        run("run_script", code=f"import FreeCAD as App\nApp.ActiveDocument.saveAs({again!r})")
        assert fp.read(again) == rec, "FreeCAD's own save lost the record"
        stripped = os.path.join(tmp, "stripped.FCStd")
        run("save_document", path=stripped)
        assert fp.read(stripped) is None, "a plain save must not keep a stale record"

        # redaction
        os.environ["ANKUSDRIVE_JOURNAL_REDACT"] = "1"
        red = os.path.join(tmp, "redacted.FCStd")
        run("save_document", path=red, attach_provenance=True)
        rrec = fp.read(red)
        text = fp.dumps(rrec)
        assert rrec["redacted"] and rrec["scope"]["anchor"]["tool"] == "open_document"
        for name in ("attached.FCStd", "stripped.FCStd"):       # open/save paths: hashed
            assert name not in text, text[max(0, text.find(name) - 300):][:600]
        assert "resaved.FCStd" in text, "run_script code is the analysis: never redacted"
    finally:
        os.environ.pop("ANKUSDRIVE_JOURNAL_REDACT", None)
        mcp_server._cleanup()
        journal.clear()
        shutil.rmtree(tmp, ignore_errors=True)


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
        print(f"  SKIP worker tier — {mcp_skip_reason()}")
        return out
    if not _freecad_available():
        print("  SKIP worker tier — freecadcmd not resolvable")
        return out
    out.append(("worker_test_real_save_and_reopen", worker_test_real_save_and_reopen))
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
            print(f"  FAIL {name:52s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:52s} ({time.time() - t0:.2f}s)")
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
