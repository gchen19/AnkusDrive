"""
Manifest schema formalization (RFC §11.7) — free, no LLM, no key. The manifest
gains a version stamp ("driftpin.manifest/1"), a load-time validator that catches
the cross-reference errors a JSON shape can't, and a content hash recorded in the
lockfile so "built against a stale contract" is detectable as such (not only
inferable from interface hashes). Proves: validate_manifest discriminates;
merge_assembly + assembly_lock reject a malformed contract at the door; and
assembly_lock_check flags a changed manifest that per-component hashes alone miss.

Run: .venv/bin/python3 tests/test_manifest_schema.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402

_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def _has(label, problems, substr):
    _check(label, any(substr in pp for pp in problems), True)


def _block(path, name):
    with Worker() as w:
        w.call("new_document", name=name)
        w.call("add_primitive", kind="box", w=20, d=20, h=20, name=name)
        w.call("save_document", path=str(path))


def _valid_manifest(tmp):
    _block(tmp / "a.FCStd", "a")
    _block(tmp / "b.FCStd", "b")
    return {"schema": "driftpin.manifest/1", "name": "ok", "root": "ok.FCStd",
            "components": {"a": {"file": "a.FCStd"}, "b": {"file": "b.FCStd"}},
            "instances": [{"component": "a", "name": "a", "placement": [0, 0, 0]},
                          {"component": "b", "name": "b", "placement": [40, 0, 0]}]}


def _validate(tmp, man):
    (tmp / "m.json").write_text(json.dumps(man), encoding="utf-8")
    with Worker() as w:
        return w.call("validate_manifest", manifest=str(tmp / "m.json"))


# --- tests -------------------------------------------------------------------

def test_valid_manifest_passes():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        r = _validate(tmp, _valid_manifest(tmp))
        _check("valid manifest -> ok", r["ok"], True)
        _check("no problems", r["problems"], [])
        _check("schema reported", r["schema"], "driftpin.manifest/1")
        _check("manifest_hash present", bool(r["manifest_hash"]), True)


def test_absent_schema_is_backcompat():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        man = _valid_manifest(tmp)
        del man["schema"]
        r = _validate(tmp, man)
        _check("absent schema accepted (unversioned)", r["ok"], True)


def test_cross_reference_errors_caught():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # component with no source
        m = _valid_manifest(tmp)
        m["components"]["c"] = {"object": "X"}
        _has("component with no source flagged",
             _validate(tmp, m)["problems"], "exactly one of file/manifest/library")
        # component with two sources
        m = _valid_manifest(tmp)
        m["components"]["a"] = {"file": "a.FCStd", "manifest": "sub/m.json"}
        _has("component with two sources flagged",
             _validate(tmp, m)["problems"], "exactly one")
        # instance referencing unknown component
        m = _valid_manifest(tmp)
        m["instances"].append({"component": "ghost", "placement": [0, 0, 0]})
        _has("instance -> unknown component flagged",
             _validate(tmp, m)["problems"], "unknown component 'ghost'")
        # bad schema version
        m = _valid_manifest(tmp)
        m["schema"] = "driftpin.manifest/99"
        _has("bad schema version flagged", _validate(tmp, m)["problems"], "unknown schema")
        # library without a tool
        m = _valid_manifest(tmp)
        m["components"]["bolt"] = {"library": {"spec": {"x": 1}}}
        m["instances"].append({"component": "bolt", "placement": [0, 0, 0]})
        _has("library without tool flagged",
             _validate(tmp, m)["problems"], "library needs a 'tool'")
        # check referencing an unknown instance
        m = _valid_manifest(tmp)
        m["checks"] = [{"kind": "gear_mesh", "a": "a", "b": "ghost",
                        "module_mm": 2, "center_distance_mm": 48}]
        _has("check -> unknown instance flagged",
             _validate(tmp, m)["problems"], "unknown instance 'ghost'")


def test_merge_rejects_invalid_manifest():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        m = _valid_manifest(tmp)
        m["instances"].append({"component": "ghost", "placement": [0, 0, 0]})
        (tmp / "m.json").write_text(json.dumps(m), encoding="utf-8")
        raised = False
        try:
            with Worker() as w:
                w.call("merge_assembly", manifest=str(tmp / "m.json"))
        except Exception as e:
            raised = "invalid manifest" in str(e)
        _check("merge_assembly raises on an invalid manifest at the door", raised, True)


def test_lockfile_records_and_detects_contract_drift():
    """The §11.7 payoff: a manifest edit that changes NO component file (so the
    per-component hashes are unchanged) is still detectable as a contract change."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        man = _valid_manifest(tmp)
        mp = tmp / "m.json"
        mp.write_text(json.dumps(man), encoding="utf-8")
        with Worker() as w:
            w.call("merge_assembly", manifest=str(mp))
            lock = w.call("assembly_lock", manifest=str(mp))
            clean = w.call("assembly_lock_check", manifest=str(mp))
        _check("lock records schema", lock["schema"], "driftpin.manifest/1")
        _check("lock records a manifest_hash", bool(lock["manifest_hash"]), True)
        _check("unchanged -> manifest_changed False", clean["manifest_changed"], False)
        _check("unchanged -> ok", clean["ok"], True)
        # edit the CONTRACT only (move an instance) — component files untouched
        man["instances"][1]["placement"] = [50, 0, 0]
        mp.write_text(json.dumps(man), encoding="utf-8")
        with Worker() as w:
            chk = w.call("assembly_lock_check", manifest=str(mp))
        _check("contract edit -> manifest_changed True", chk["manifest_changed"], True)
        _check("but no component file is `modified` (hashes unchanged)",
               chk["modified"], [])


def test_stamping_schema_is_not_a_contract_change():
    """Adding the schema stamp to a previously-unversioned manifest must NOT read
    as drift — the hash excludes the schema field."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        man = _valid_manifest(tmp)
        del man["schema"]
        mp = tmp / "m.json"
        mp.write_text(json.dumps(man), encoding="utf-8")
        with Worker() as w:
            w.call("merge_assembly", manifest=str(mp))
            w.call("assembly_lock", manifest=str(mp))
        man["schema"] = "driftpin.manifest/1"   # stamp it
        mp.write_text(json.dumps(man), encoding="utf-8")
        with Worker() as w:
            chk = w.call("assembly_lock_check", manifest=str(mp))
        _check("stamping schema is not a contract change",
               chk["manifest_changed"], False)


def main():
    print("== manifest schema — validation + contract-drift detection (no API) ==")
    for t in (test_valid_manifest_passes, test_absent_schema_is_backcompat,
              test_cross_reference_errors_caught, test_merge_rejects_invalid_manifest,
              test_lockfile_records_and_detects_contract_drift,
              test_stamping_schema_is_not_a_contract_change):
        try:
            t()
        except Exception as e:
            global _FAIL
            _FAIL += 1
            import traceback
            print(f"  FAIL {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n== {_PASS}/{_PASS + _FAIL} checks passed "
          f"({'OK' if not _FAIL else str(_FAIL) + ' FAILED'}) ==")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
