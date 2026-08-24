"""The DriftPin -> AnkusDrive compatibility shims (issue #295).

The rename changed four things a user's machine already holds, none of which
this package can reach in to edit:

  * ``DRIFTPIN_*`` environment variables, in shell profiles / CI secrets / an
    MCP host's ``env`` block,
  * the config file at ``~/.config/driftpin/config.toml``,
  * solver binaries provisioned under a ``DriftPin`` application directory,
  * ``DP_*`` properties stamped into every ``.FCStd`` the user has saved.

Each has the same failure mode if dropped, and it is the bad one: nothing
raises. An unset override falls through to auto-discovery and resolves the
wrong solver; an unread property makes an annotated part read back as a bare
solid. So each shim is asserted here rather than assumed, including the part
that is easy to get backwards — that the NEW spelling always wins, so the shim
cannot shadow an explicit choice.

These tests pin behaviour that is scheduled for deletion in 0.6. When it goes,
delete this file with it; ``tests/test_naming.py`` will then flag the allowlist
entries that outlived their reason.

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


# --- environment ------------------------------------------------------------

def test_legacy_env_var_is_promoted():
    env = {"DRIFTPIN_SU2_PATH": "/opt/su2/SU2_CFD"}
    promoted = config.adopt_legacy_env(env, warn=False)
    assert promoted == ["DRIFTPIN_SU2_PATH"], promoted
    assert env["ANKUSDRIVE_SU2_PATH"] == "/opt/su2/SU2_CFD", env


def test_explicit_new_env_var_wins():
    """A user who has migrated must not be overridden by a stale old var."""
    env = {"DRIFTPIN_FREECADCMD": "/old/freecadcmd",
           "ANKUSDRIVE_FREECADCMD": "/new/freecadcmd"}
    promoted = config.adopt_legacy_env(env, warn=False)
    assert promoted == [], promoted
    assert env["ANKUSDRIVE_FREECADCMD"] == "/new/freecadcmd", env


def test_promotion_is_idempotent():
    env = {"DRIFTPIN_YADE_PATH": "/opt/yade"}
    config.adopt_legacy_env(env, warn=False)
    assert config.adopt_legacy_env(env, warn=False) == [], "second pass re-promoted"
    assert env["ANKUSDRIVE_YADE_PATH"] == "/opt/yade"


def test_unrelated_env_is_untouched():
    env = {"PATH": "/usr/bin", "DRIFT": "x", "MYDRIFTPIN_X": "y"}
    before = dict(env)
    assert config.adopt_legacy_env(env, warn=False) == []
    assert env == before, env


# --- config file ------------------------------------------------------------

def test_config_path_prefers_the_new_location():
    """With neither file present, the new path is what we report and write."""
    with _config_root() as root:
        expected = str(root / "ankusdrive" / "config.toml")
        assert config.config_path() == expected, config.config_path()
        assert config._current_config_path() == expected


def test_config_path_falls_back_to_the_pre_rename_file():
    """An unmigrated user's settings must still be found — but writes migrate
    forward, so `ankusdrive setup` moves them without a manual step."""
    with _config_root() as root:
        legacy = root / "driftpin" / "config.toml"
        legacy.parent.mkdir(parents=True)
        legacy.write_text('freecadcmd = "/legacy/freecadcmd"\n', encoding="utf-8")
        assert config.config_path() == str(legacy), config.config_path()
        assert config._current_config_path() != str(legacy), "write would rewrite it"
        assert config._current_config_path() == str(root / "ankusdrive" / "config.toml")
        # and the contents actually resolve through the normal reader
        config._cache.clear()
        assert config.load().get("freecadcmd") == "/legacy/freecadcmd"


def test_new_config_file_wins_over_the_legacy_one():
    with _config_root() as root:
        for name in ("driftpin", "ankusdrive"):
            f = root / name / "config.toml"
            f.parent.mkdir(parents=True)
            f.write_text(f'freecadcmd = "/{name}/freecadcmd"\n', encoding="utf-8")
        assert config.config_path() == str(root / "ankusdrive" / "config.toml"), \
            config.config_path()
        config._cache.clear()
        assert config.load().get("freecadcmd") == "/ankusdrive/freecadcmd"


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


# --- the deprecated console script -----------------------------------------

def test_doctor_reports_what_is_still_on_a_shim():
    """The shims are temporary, so `doctor` has to name what has not migrated —
    a startup warning alone is scrolled past."""
    from ankusdrive import doctor
    saved = os.environ.get("DRIFTPIN_SU2_PATH")
    try:
        os.environ["DRIFTPIN_SU2_PATH"] = "/opt/su2/SU2_CFD"
        config.adopt_legacy_env(warn=False)
        report = doctor.config_report()
        assert "DRIFTPIN_SU2_PATH" in report["legacy_env"], report
        assert "legacy_config" in report, report
        rendered = doctor.render({"platform": {"system": "T", "machine": "t"},
                                  "freecad": {"available": False, "path": None,
                                              "fix": "install FreeCAD"},
                                  "config": report,
                                  "solvers": {"families": {}}})
        assert "DEPRECATED" in rendered and "DRIFTPIN_SU2_PATH" in rendered, rendered
    finally:
        os.environ.pop("DRIFTPIN_SU2_PATH", None)
        os.environ.pop("ANKUSDRIVE_SU2_PATH", None)
        if saved is not None:
            os.environ["DRIFTPIN_SU2_PATH"] = saved
        config.adopt_legacy_env(warn=False)


def test_legacy_cli_shim_delegates():
    import ankusdrive._legacy_cli as shim
    src = Path(shim.__file__).read_text(encoding="utf-8")
    assert "from .cli import main" in src, "shim no longer delegates to the real CLI"
    assert "0.6" in src, "shim does not say when it goes away"
    import tomllib
    with open(REPO / "pyproject.toml", "rb") as f:
        scripts = tomllib.load(f)["project"]["scripts"]
    assert scripts["ankusdrive"] == "ankusdrive.cli:main", scripts
    assert scripts["driftpin"] == "ankusdrive._legacy_cli:main", scripts


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
