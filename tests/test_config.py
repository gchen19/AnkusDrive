"""Persistent-config layer (issue #199) — env override -> config file -> auto.

Pure-Python, no FreeCAD. The config file is the resolution layer that survives an
MCP host's minimal launch env: a path set once in
``~/.config/ankusdrive/config.toml`` (``%APPDATA%`` on Windows) resolves FreeCAD
and solver binaries with NO environment variables, while a ``ANKUSDRIVE_*`` env
var still wins when present (CI / one-off runs unchanged).

Run:  python3 tests/test_config.py
"""
import os
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankusdrive import config, solvers  # noqa: E402


@contextmanager
def _env(**vars):
    """Temporarily set (value) or remove (None) environment variables."""
    saved = {k: os.environ.get(k) for k in vars}
    try:
        for k, v in vars.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _write_config(text: str) -> str:
    fd, path = tempfile.mkstemp(prefix="ankusdrive_cfg_", suffix=".toml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def test_config_path_override_and_platform_default():
    with _env(ANKUSDRIVE_CONFIG=r"C:\somewhere\my.toml"):
        assert config.config_path() == r"C:\somewhere\my.toml"
    with _env(ANKUSDRIVE_CONFIG=None):
        p = config.config_path()
        assert p.endswith(os.path.join("ankusdrive", "config.toml")), p
        if os.name == "nt":
            assert os.environ.get("APPDATA", "~") in p or "ankusdrive" in p, p
        else:
            assert ".config" in p or os.environ.get("XDG_CONFIG_HOME", "") in p, p


def test_load_missing_and_malformed_are_empty():
    with _env(ANKUSDRIVE_CONFIG=os.path.join(tempfile.gettempdir(), "nope_ankusdrive.toml")):
        assert config.load() == {}
    bad = _write_config("this is [ not toml = = =")
    try:
        with _env(ANKUSDRIVE_CONFIG=bad):
            assert config.load() == {}          # malformed -> optional, never raises
    finally:
        os.unlink(bad)


def test_lookup_precedence_env_beats_config():
    cfg = _write_config('freecadcmd = "/from/config/freecadcmd"\n'
                        "[solvers]\n"
                        'su2_path = "/from/config/SU2_CFD"\n')
    try:
        with _env(ANKUSDRIVE_CONFIG=cfg, ANKUSDRIVE_FREECADCMD=None, ANKUSDRIVE_SU2_PATH=None):
            assert config.lookup("ANKUSDRIVE_FREECADCMD") == ("/from/config/freecadcmd", "config")
            assert config.lookup("ANKUSDRIVE_SU2_PATH") == ("/from/config/SU2_CFD", "config")
            assert config.lookup("ANKUSDRIVE_ELMER_PATH") == (None, None)
        with _env(ANKUSDRIVE_CONFIG=cfg, ANKUSDRIVE_SU2_PATH="/from/env/SU2_CFD"):
            assert config.lookup("ANKUSDRIVE_SU2_PATH") == ("/from/env/SU2_CFD", "env")
    finally:
        os.unlink(cfg)


def test_edit_is_picked_up_without_restart():
    cfg = _write_config("[solvers]\nsu2_path = '/v1'\n")
    try:
        with _env(ANKUSDRIVE_CONFIG=cfg, ANKUSDRIVE_SU2_PATH=None):
            assert config.get("ANKUSDRIVE_SU2_PATH") == "/v1"
            time.sleep(0.01)
            with open(cfg, "w", encoding="utf-8") as f:
                f.write("[solvers]\nsu2_path = '/v2'\n")
            now = time.time()
            os.utime(cfg, (now + 2, now + 2))   # force a distinct mtime
            assert config.get("ANKUSDRIVE_SU2_PATH") == "/v2"
    finally:
        os.unlink(cfg)


def test_minimal_parser_matches_schema():
    # the 3.10 fallback covers exactly the documented flat schema
    parsed = config._parse_minimal(
        '# comment\n'
        'freecadcmd = "/apps/freecadcmd"   # trailing comment\n'
        "\n"
        "[solvers]\n"
        "elmer_path = '/opt/elmer/ElmerSolver'\n")
    assert parsed == {"freecadcmd": "/apps/freecadcmd",
                      "solvers": {"elmer_path": "/opt/elmer/ElmerSolver"}}


def test_solver_discovery_reads_config_layer():
    # a solver path set ONLY in the config file resolves through find_solver —
    # no env var anywhere (the MCP-host minimal-env scenario).
    with tempfile.NamedTemporaryFile(prefix="fake-su2-", delete=False) as tf:
        fake_bin = tf.name
    cfg = _write_config(
        f'[solvers]\nsu2_path = "{fake_bin.replace(os.sep, "/")}"\n')
    try:
        with _env(ANKUSDRIVE_CONFIG=cfg, ANKUSDRIVE_SU2_PATH=None):
            info = solvers.find_solver("su2")
            assert info["available"] is True, info
            assert os.path.normpath(info["path"]) == os.path.normpath(fake_bin), info
    finally:
        os.unlink(cfg)
        os.unlink(fake_bin)


def test_client_resolves_freecadcmd_from_config():
    from ankusdrive import client
    fake = _write_config("")                     # any existing file path will do
    cfg = _write_config(f'freecadcmd = "{fake.replace(os.sep, "/")}"\n')
    try:
        with _env(ANKUSDRIVE_CONFIG=cfg, ANKUSDRIVE_FREECADCMD=None):
            got = client._resolve_freecadcmd()
            assert os.path.normpath(got) == os.path.normpath(fake), (got, fake)
    finally:
        os.unlink(cfg)
        os.unlink(fake)


def test_doctor_reports_config_layer():
    from ankusdrive import doctor
    cfg = _write_config('freecadcmd = "/x/freecadcmd"\n[solvers]\nsu2_path = "/x/su2"\n')
    try:
        with _env(ANKUSDRIVE_CONFIG=cfg):
            rep = doctor.config_report()
            assert rep["path"] == cfg and rep["present"] is True, rep
            assert rep["keys"] == ["freecadcmd", "solvers.su2_path"], rep
        with _env(ANKUSDRIVE_CONFIG=cfg + ".absent"):
            rep = doctor.config_report()
            assert rep["present"] is False and rep["keys"] == [], rep
    finally:
        os.unlink(cfg)


def test_write_merges_and_persists():
    # ankusdrive setup's persistence step (issue #200): write() creates the file
    # (and directory), later writes MERGE rather than clobber, and values
    # round-trip through the normal lookup.
    with tempfile.TemporaryDirectory() as d:
        with _env(ANKUSDRIVE_CONFIG=os.path.join(d, "sub", "config.toml"),
                  ANKUSDRIVE_FREECADCMD=None, ANKUSDRIVE_SU2_PATH=None):
            path = config.write(
                {"ANKUSDRIVE_FREECADCMD": os.sep.join(["C:", "fc", "freecadcmd.exe"])})
            assert os.path.isfile(path), path
            assert config.get("ANKUSDRIVE_FREECADCMD") == "C:/fc/freecadcmd.exe"
            config.write({"ANKUSDRIVE_SU2_PATH": "/opt/su2/SU2_CFD"})
            assert config.get("ANKUSDRIVE_FREECADCMD") == "C:/fc/freecadcmd.exe"
            assert config.get("ANKUSDRIVE_SU2_PATH") == "/opt/su2/SU2_CFD"
            body = open(path, encoding="utf-8").read()
            assert "[solvers]" in body and 'su2_path = "/opt/su2/SU2_CFD"' in body, body


# --- runner -------------------------------------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


def main():
    tests = _discover()
    failures = []
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, e, traceback.format_exc()))
            print(f"  FAIL {name:48s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:48s} ({time.time() - t0:.2f}s)")

    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, _, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()
