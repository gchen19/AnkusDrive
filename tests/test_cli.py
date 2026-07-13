"""
Toy problems for the DriftPin CLI. Each test invokes `python3 -m driftpin <cmd>`
as a subprocess, checks exit code, stdout shape, and any on-disk artifacts.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def dp(*args, timeout=300):
    return subprocess.run(
        [sys.executable, "-m", "driftpin", *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_ping():
    r = dp("ping")
    assert r.returncode == 0, r.stderr
    assert "ping=pong" in r.stdout, r.stdout
    assert "freecad=1.1" in r.stdout, r.stdout


def test_version():
    r = dp("version")
    assert r.returncode == 0, r.stderr
    assert "freecad=1.1" in r.stdout
    assert "python=3.11" in r.stdout


def test_box():
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "b.FCStd")
        r = dp("box", "--w", "10", "--d", "20", "--h", "5", "-o", out)
        assert r.returncode == 0, r.stderr
        assert os.path.isfile(out)
        # parse "volume=..." token, tolerate float jitter
        vol = float([t.split("=", 1)[1] for t in r.stdout.split() if t.startswith("volume=")][0])
        assert abs(vol - 1000.0) < 1e-6, f"unexpected volume {vol}"


def test_cylinder():
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "c.FCStd")
        r = dp("cylinder", "--r", "5", "--h", "10", "-o", out)
        assert r.returncode == 0, r.stderr
        assert os.path.isfile(out)


def test_export_step():
    with tempfile.TemporaryDirectory() as tmp:
        fcstd = os.path.join(tmp, "b.FCStd")
        step = os.path.join(tmp, "b.step")
        assert dp("box", "--w", "10", "--d", "10", "--h", "10", "-o", fcstd).returncode == 0
        r = dp("export", fcstd, "-o", step)
        assert r.returncode == 0, r.stderr
        assert os.path.isfile(step)
        with open(step, encoding="utf-8") as f:
            head = f.read(100)
        assert "ISO-10303" in head, f"not a STEP file: {head!r}"


def test_export_stl():
    with tempfile.TemporaryDirectory() as tmp:
        fcstd = os.path.join(tmp, "b.FCStd")
        stl = os.path.join(tmp, "b.stl")
        assert dp("box", "--w", "10", "--d", "10", "--h", "10", "-o", fcstd).returncode == 0
        r = dp("export", fcstd, "-o", stl)
        assert r.returncode == 0, r.stderr
        assert os.path.getsize(stl) > 0


def test_run_script():
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "s.py")
        Path(script).write_text(
            "doc = App.newDocument('scripted')\n"
            "b = doc.addObject('Part::Box', 'B')\n"
            "b.Length = 7; b.Width = 8; b.Height = 9\n"
            "doc.recompute()\n"
            "__result__ = {'volume': b.Shape.Volume, 'types': [o.TypeId for o in doc.Objects]}\n", encoding="utf-8"
        )
        r = dp("run", script)
        assert r.returncode == 0, r.stderr
        payload = json.loads(r.stdout)
        assert payload["volume"] == 504.0
        assert payload["types"] == ["Part::Box"]


def test_run_script_error_surfaces():
    """Broken script → nonzero exit, error on stderr with traceback."""
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "bad.py")
        Path(script).write_text("raise ValueError('toy problem')\n", encoding="utf-8")
        r = dp("run", script)
        assert r.returncode == 2
        assert "ValueError" in r.stderr
        assert "toy problem" in r.stderr


def test_fem_cantilever():
    r = dp("fem", "cantilever", "--mesh-size", "500")
    assert r.returncode == 0, r.stderr
    for key in ("nodes=", "tets=", "max_disp_mm=", "max_vM_MPa="):
        assert key in r.stdout, f"missing {key}: {r.stdout}"


def test_setup_print_mcp_config():
    # issue #201: the registration block is emitted with an ABSOLUTE launcher
    # (GUI hosts don't inherit shell PATH) and parseable mcpServers JSON.
    r = dp("setup", "--print-mcp-config")
    assert r.returncode == 0, r.stderr
    start, end = r.stdout.index("{"), r.stdout.rindex("}") + 1
    snippet = json.loads(r.stdout[start:end])
    server = snippet["mcpServers"]["driftpin"]
    assert os.path.isabs(server["command"]), server
    assert os.path.isfile(server["command"]), server
    assert server["args"][-1] == "mcp", server
    assert "claude mcp add driftpin" in r.stdout, r.stdout


def test_setup_yes_writes_config():
    # issue #200 acceptance: --yes resolves FreeCAD and persists it to the
    # config file (pointed at a temp location via DRIFTPIN_CONFIG).
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.toml")
        env = dict(os.environ, DRIFTPIN_CONFIG=cfg, PYTHONUTF8="1")
        env.pop("DRIFTPIN_FREECADCMD", None)
        r = subprocess.run(
            [sys.executable, "-m", "driftpin", "setup", "--yes"],
            cwd=str(REPO), capture_output=True, text=True, timeout=300, env=env)
        assert r.returncode == 0, r.stderr + r.stdout
        assert "mcpServers" in r.stdout, r.stdout
        # on a box with FreeCAD resolvable, the path is persisted
        if "found" in r.stdout:
            assert os.path.isfile(cfg), r.stdout
            body = open(cfg, encoding="utf-8").read()
            assert "freecadcmd = " in body, body
        # unknown extras are refused cleanly, not installed
        r2 = subprocess.run(
            [sys.executable, "-m", "driftpin", "setup", "--yes",
             "--extras", "bogus"],
            cwd=str(REPO), capture_output=True, text=True, timeout=300, env=env)
        assert r2.returncode == 0, r2.stderr + r2.stdout
        assert "unknown extras skipped: bogus" in r2.stdout, r2.stdout


# --- runner -------------------------------------------------------------------

def _discover():
    return [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]


def main():
    failures = []
    t_suite = time.time()
    for name, fn in _discover():
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, e, traceback.format_exc()))
            print(f"  FAIL {name:40s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:40s} ({time.time() - t0:.2f}s)")

    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(_discover())} failed  ({total:.1f}s) ==")
        for name, _, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(_discover())}/{len(_discover())} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()
