"""The DriftPin -> AnkusDrive rename (#295), after 0.6 removed its shims (#519).

The rename changed four things a user's machine already holds, none of which
this package can reach in to edit:

  * ``DRIFTPIN_*`` environment variables, in shell profiles / CI secrets / an
    MCP host's ``env`` block,
  * the config file at ``~/.config/driftpin/config.toml``,
  * solver binaries provisioned under a ``DriftPin`` application directory,
  * ``DP_*`` properties stamped into every ``.FCStd`` the user has saved.

0.6 stopped reading the first three. Each of those removals fails silently on
an unmigrated box (an unset override auto-discovers the wrong solver), so what
is pinned here is that the old spelling is truly ignored AND that
``ankusdrive doctor`` still names it with the fix.

The fourth stays. ``DP_*`` properties live inside the user's documents, and
dropping the fallback would make an annotated part read back as a bare solid
with no error. It goes only with an explicit document migration, so the
property tests below still pin read-either / update-in-place / new-wins.

Pure stdlib, no FreeCAD. Run:  python3 tests/test_compat_rename.py
"""
import contextlib
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import config, props   # noqa: E402


class _Stub:
    """Stands in for a FreeCAD DocumentObject — attribute get/set is all the
    property helpers use, so a plain object exercises them faithfully."""


# The config directory is rooted at a DIFFERENT env var per platform
# (config._config_base): %APPDATA% on Windows, $XDG_CONFIG_HOME elsewhere.
# Pinning only the POSIX one silently does nothing on Windows, and the test then
# reads the runner's REAL config dir — which is how this suite first failed CI.
_CONFIG_ROOT_ENV = "APPDATA" if os.name == "nt" else "XDG_CONFIG_HOME"


@contextlib.contextmanager
def _config_root():
    """Point the config layer at a throwaway root, on every platform."""
    saved = {k: os.environ.get(k) for k in (_CONFIG_ROOT_ENV, "ANKUSDRIVE_CONFIG")}
    with tempfile.TemporaryDirectory() as root:
        try:
            os.environ.pop("ANKUSDRIVE_CONFIG", None)
            os.environ[_CONFIG_ROOT_ENV] = root
            config._cache.clear()
            yield Path(root)
        finally:
            config._cache.clear()
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


# --- environment (removed in 0.6) ------------------------------------------

def test_legacy_env_var_is_not_promoted():
    """0.6 dropped the import-time promotion: a DRIFTPIN_* var is just a var."""
    from ankusdrive import config as cfg
    assert not hasattr(cfg, "adopt_legacy_env"), "the env shim is still there"
    saved = {k: os.environ.get(k) for k in ("DRIFTPIN_SU2_PATH", "ANKUSDRIVE_SU2_PATH")}
    try:
        os.environ.pop("ANKUSDRIVE_SU2_PATH", None)
        os.environ["DRIFTPIN_SU2_PATH"] = "/opt/su2/SU2_CFD"
        import importlib

        import ankusdrive
        importlib.reload(ankusdrive)
        assert "ANKUSDRIVE_SU2_PATH" not in os.environ, "import promoted a legacy var"
        assert config.lookup("ANKUSDRIVE_SU2_PATH")[0] is None
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


# --- config file (removed in 0.6) ------------------------------------------

def test_config_path_is_the_new_location():
    with _config_root() as root:
        expected = str(root / "ankusdrive" / "config.toml")
        assert config.config_path() == expected, config.config_path()
        assert config._current_config_path() == expected


def test_pre_rename_config_file_is_not_read():
    """An unmigrated file is ignored, not silently honoured — doctor names it."""
    with _config_root() as root:
        legacy = root / "driftpin" / "config.toml"
        legacy.parent.mkdir(parents=True)
        legacy.write_text('freecadcmd = "/legacy/freecadcmd"\n', encoding="utf-8")
        assert config.config_path() == str(root / "ankusdrive" / "config.toml")
        config._cache.clear()
        assert config.load().get("freecadcmd") is None, "read the pre-rename file"


# --- stamped .FCStd properties ---------------------------------------------

def test_legacy_property_is_read():
    """The whole point: a part saved by <=0.4.x still reports its intent."""
    obj = _Stub()
    obj.DP_Intent = '{"invariants": []}'
    assert props.prop_has(obj, "AD_Intent")
    assert props.prop_get(obj, "AD_Intent") == '{"invariants": []}'
    assert props.prop_name(obj, "AD_Intent") == "DP_Intent"


def test_legacy_property_is_updated_in_place():
    """Writing must not leave a second, shadowing property behind."""
    obj = _Stub()
    obj.DP_Interfaces = "{}"
    props.prop_set(obj, "AD_Interfaces", '{"lid_seat": {}}')
    assert obj.DP_Interfaces == '{"lid_seat": {}}'
    assert not hasattr(obj, "AD_Interfaces"), "created a shadow property"


def test_new_property_wins_over_legacy():
    obj = _Stub()
    obj.DP_Performance = "old"
    obj.AD_Performance = "new"
    assert props.prop_get(obj, "AD_Performance") == "new"
    assert props.prop_name(obj, "AD_Performance") == "AD_Performance"


def test_absent_property_reports_absent():
    """The silent-default case: no property under either spelling must be
    distinguishable from an empty one."""
    obj = _Stub()
    assert not props.prop_has(obj, "AD_FaceRoles")
    assert props.prop_get(obj, "AD_FaceRoles", "SENTINEL") == "SENTINEL"
    assert props.prop_name(obj, "AD_FaceRoles") == "AD_FaceRoles", \
        "a new stamp must be created under the new name"


def test_non_prefixed_names_are_passed_through():
    """The helpers are used with FreeCAD's own property names too."""
    obj = _Stub()
    obj.Label = "bracket"
    assert props.prop_name(obj, "Label") == "Label"
    assert props.prop_get(obj, "Label") == "bracket"


# --- doctor names what the removal broke -----------------------------------

def test_doctor_names_a_legacy_env_var():
    from ankusdrive import doctor
    env = {"DRIFTPIN_SU2_PATH": "/opt/su2/SU2_CFD",
           "DRIFTPIN_FREECADCMD": "/old", "ANKUSDRIVE_FREECADCMD": "/new",
           "MYDRIFTPIN_X": "unrelated"}
    rep = doctor.legacy_report(environ=env)
    assert [v["name"] for v in rep["legacy_env"]] == [
        "DRIFTPIN_FREECADCMD", "DRIFTPIN_SU2_PATH"], rep
    by = {v["name"]: v for v in rep["legacy_env"]}
    assert by["DRIFTPIN_SU2_PATH"]["new"] == "ANKUSDRIVE_SU2_PATH"
    assert by["DRIFTPIN_SU2_PATH"]["new_set"] is False
    assert by["DRIFTPIN_FREECADCMD"]["new_set"] is True
    text = "\n".join(doctor._fmt_legacy({"path": "/c", "present": False, **rep}))
    assert "IGNORED" in text and "rename it to ANKUSDRIVE_SU2_PATH" in text, text
    assert "delete it, ANKUSDRIVE_FREECADCMD is already set" in text, text


def test_doctor_names_the_pre_rename_config_file():
    from ankusdrive import doctor
    with _config_root() as root:
        legacy = root / "driftpin" / "config.toml"
        legacy.parent.mkdir(parents=True)
        legacy.write_text('freecadcmd = "/legacy"\n', encoding="utf-8")
        report = doctor.config_report()
        assert report["legacy_config"] == str(legacy), report
        rendered = doctor.render({"platform": {"system": "T", "machine": "t"},
                                  "freecad": {"available": False, "path": None,
                                              "fix": "install FreeCAD"},
                                  "config": report,
                                  "solvers": {"families": {}}})
        assert "IGNORED" in rendered and str(legacy) in rendered, rendered
        assert "move it to " + str(root / "ankusdrive" / "config.toml") in rendered


def test_doctor_is_quiet_on_a_migrated_box():
    from ankusdrive import doctor
    with _config_root():
        rep = doctor.legacy_report(environ={"ANKUSDRIVE_SU2_PATH": "/x"}, system="Linux")
        assert rep["legacy_env"] == [] and rep["legacy_config"] is None, rep
        assert rep["legacy_solver_dirs"] == [], rep
        rep["legacy_command"] = None      # PATH is the runner's, not ours to pin
        assert doctor._fmt_legacy({"path": "/c", **rep}) == []


def test_doctor_names_a_pre_rename_solver_dir():
    from ankusdrive import doctor
    with tempfile.TemporaryDirectory() as lad:
        old = Path(lad) / "DriftPin" / "solvers"
        old.mkdir(parents=True)
        saved = os.environ.get("LOCALAPPDATA")
        try:
            os.environ["LOCALAPPDATA"] = lad
            dirs = doctor._legacy_solver_dirs("Windows")
        finally:
            os.environ.pop("LOCALAPPDATA", None)
            if saved is not None:
                os.environ["LOCALAPPDATA"] = saved
        assert dirs == [{"path": str(old),
                         "move_to": str(Path(lad) / "AnkusDrive" / "solvers")}], dirs


def test_legacy_cli_is_gone():
    assert not (REPO / "ankusdrive" / "_legacy_cli.py").exists()
    import tomllib
    with open(REPO / "pyproject.toml", "rb") as f:
        scripts = tomllib.load(f)["project"]["scripts"]
    assert scripts["ankusdrive"] == "ankusdrive.cli:main", scripts
    assert "driftpin" not in scripts, scripts


def _discover():
    g = globals()
    return [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]


def main():
    failures = []
    t_suite = time.time()
    tests = _discover()
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
