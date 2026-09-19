"""The slim solver image's contract (#422) — what `ankusdrive-solvers` must stay.

`ankusdrive-heavy` is the CI image: the whole suite runs inside it, so it carries
FreeCAD, the driver venv and every -dev package. `ankusdrive-solvers` is what a user
pulls for ANKUSDRIVE_SUBSTRATE=container, where AnkusDrive and FreeCAD run on the
HOST and only solvers cross `<engine> exec`. The difference is most of the download.

Nothing here builds an image — the CI `slim` job does that, and the stage's own RUN
steps are the real gate (every DT_NEEDED resolves, every solver runs). These are the
static promises that a well-meaning edit would otherwise erode without failing a
build: the host-side payloads staying out, the trim actually reclaiming its bytes,
both source modes wired for every prefix, and the checks that caught the two bugs
this image shipped with in development.

Stdlib only; reads files; no Docker.

Run:  python3 tests/test_slim_image.py
"""
import re
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "docker" / "heavy-solvers" / "Dockerfile"
WORKFLOW = REPO / ".github" / "workflows" / "heavy-image.yml"
PREFIXES = ("yade", "openems", "fsi", "oims", "elmer", "bempp")


def _text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _stage(name: str) -> str:
    """The Dockerfile lines belonging to stage ``name``, up to the next FROM."""
    text = _text()
    m = re.search(rf"^FROM\s+\S+\s+AS\s+{re.escape(name)}\s*$", text, re.M)
    assert m, f"no stage named {name!r}"
    rest = text[m.end():]
    nxt = re.search(r"^FROM\s", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def test_slim_builds_on_the_runtime_base_not_the_ci_image():
    """`slim` must descend from `runtime` (apt runtime packages only). Basing it on
    `system` or `final` would silently drag the toolchain — or FreeCAD — back in."""
    text = _text()
    assert re.search(r"^FROM\s+runtime\s+AS\s+slim\s*$", text, re.M), \
        "the slim stage must be FROM runtime"
    assert re.search(r"^FROM\s+runtime\s+AS\s+assemble\s*$", text, re.M), \
        "the assemble stage must be FROM runtime"


def test_host_side_payloads_never_enter_the_slim_image():
    """FreeCAD, the driver venv, Blender, CalculiX and PrusaSlicer all resolve on the
    HOST under this substrate. Copying any of them back in is the whole regression
    this image exists to prevent (~2.5 GB of a ~4.5 GB download, measured on arm64)."""
    body = _stage("assemble") + _stage("slim") + _stage("runtime")
    for forbidden in ("/opt/freecad", "/opt/venv ", "install-freecad-appimage.sh",
                      "install-renderers.sh", "calculix-ccx", "prusa-slicer",
                      "blender"):
        assert forbidden not in body, \
            f"{forbidden!r} is host-side under the container substrate — keep it out of the slim image"


def test_the_runtime_base_carries_no_build_toolchain():
    """Runtime packages only: no compiler, no headers. A -dev package here means the
    image is paying for a build it never does."""
    body = _stage("runtime")
    installs = " ".join(re.findall(r"apt-get install[^&]*", body))
    for pkg in ("build-essential", "cmake", "gfortran ", "g++", "python3-dev",
                "openfoam2512-dev", "flex", "bison", "ccache"):
        assert pkg not in installs, f"{pkg!r} is a BUILD dependency; the slim image runs prebuilt solvers"
    dev_pkgs = re.findall(r"\blib\S+-dev\b", installs)
    assert not dev_pkgs, f"a -dev package is installed in the runtime base: {dev_pkgs}"


def test_the_trim_happens_where_it_reclaims_bytes():
    """Deleting a file in a layer ABOVE the one that added it reclaims nothing — the
    data still ships. The OF-7 trim must therefore run in `assemble`, whose /opt the
    `slim` stage then copies in one layer."""
    assert "rm -rf /opt/of7/OpenFOAM/OpenFOAM-7/platforms/*/applications" in _stage("assemble"), \
        "the OpenFOAM-7 trim must run in the assemble stage"
    assert re.search(r"COPY --from=assemble\s+/opt\s+/opt", _stage("slim")), \
        "slim must copy the assembled /opt in a single layer, or the trim reclaims nothing"
    assert "rm -rf" not in _stage("slim"), \
        "a delete in the slim stage reclaims nothing — trim in assemble instead"


def test_both_source_modes_are_wired_for_every_prefix():
    """SOLVER_SRC=stages (CI, from the same recipe) and =published (a local build in
    minutes). A prefix missing from either alias set breaks that mode only, which is
    exactly the kind of gap nobody notices until they try it."""
    text = _text()
    assert re.search(r"^ARG SOLVER_SRC=stages\s*$", text, re.M), \
        "SOLVER_SRC must default to `stages`, so CI builds from the recipe"
    for p in PREFIXES:
        for mode in ("stages", "published"):
            assert re.search(rf"^FROM\s+\S+\s+AS\s+src-{p}-{mode}\s*$", text, re.M), \
                f"no src-{p}-{mode} alias stage"
        assert re.search(rf"^FROM\s+src-{p}-\$\{{SOLVER_SRC\}}\s+AS\s+src-{p}\s*$", text, re.M), \
            f"src-{p} does not select on SOLVER_SRC"


def test_bempp_is_its_own_stage_so_both_images_can_copy_it():
    """It used to be built inside `final`, which a COPY --from cannot name."""
    text = _text()
    assert re.search(r"^FROM\s+system\s+AS\s+bempp\s*$", text, re.M), "bempp must be its own stage"
    assert "COPY --from=bempp /opt/venv-bempp /opt/venv-bempp" in text, \
        "the heavy image must copy the bempp venv from that stage, not rebuild it"


def test_the_image_proves_each_solver_RUNS_not_merely_exists():
    """Both bugs this image shipped with in development passed a weaker check: YADE
    was missing python3-mpmath (then IPython) and `yade --version` still exited 0 on
    an image where every DEM solve died. So the gate runs a SCRIPT through yade, and
    imports the dedicated venvs' modules."""
    body = _stage("slim")
    assert "--check" in body and "ankusdrive-runtime-deps" in body, \
        "the slim stage must assert every DT_NEEDED resolves on this architecture"
    assert "yade -x -n /tmp/yade_probe.py" in body, \
        "a `yade --version` check passes on an image where running a script fails — run one"
    for probe in ('import openEMS, CSXCAD', 'import bempp_cl', 'ELMER SOLVER', 'simpleFoam -help'):
        assert probe in body, f"the slim stage does not prove {probe!r} works"
    # the imports ldd cannot see
    for pkg in ("python3-numpy", "python3-mpmath", "ipython3"):
        assert pkg in _stage("runtime"), f"{pkg} is a YADE import, invisible to ldd"


def test_an_unset_target_arch_fails_the_build():
    """Without TARGETARCH the per-arch packages (amd64's Elmer, arm64's BLAS) are
    skipped silently and the image publishes without a solver."""
    assert re.search(r"TARGETARCH.*amd64\|arm64", _stage("runtime")), \
        "the runtime stage must reject an unset/unknown TARGETARCH"


def test_the_workflow_publishes_the_slim_image_for_both_arches():
    wf = WORKFLOW.read_text(encoding="utf-8")
    assert "ankusdrive-solvers" in wf, "the workflow must publish the slim image"
    for job in ("slim:", "slim-manifest:"):
        assert re.search(rf"^  {re.escape(job)}\s*$", wf, re.M), f"no {job} job"
    slim_job = wf[wf.index("\n  slim:"):wf.index("\n  slim-manifest:")]
    assert "target: slim" in slim_job
    for arch in ("amd64", "arm64"):
        assert arch in slim_job, f"the slim job does not build {arch}"
    assert "bempp" in wf[wf.index("target: [yade"):wf.index("target: [yade") + 120], \
        "the bempp stage must be in the stage matrix, or the slim build compiles it"


def test_the_docs_send_users_to_the_slim_image():
    doc = (REPO / "docs" / "CONTAINER_SUBSTRATE.md").read_text(encoding="utf-8")
    assert "ankusdrive-solvers" in doc
    run_cmd = doc[doc.index("docker run -d --name ankusdrive-solvers"):]
    assert "ghcr.io/gchen19/ankusdrive-solvers" in run_cmd[:600], \
        "the documented `docker run` must name the slim image"
    assert "--tmpfs /tmp" in run_cmd[:600], \
        "solvers write heavily to /tmp; a full Docker disk otherwise looks like a solver bug"


def _discover():
    g = globals()
    return [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]


def main():
    failures = []
    t_suite = time.time()
    for name, fn in _discover():
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:58s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:58s} ({time.time() - t0:.2f}s)")
    print()
    total = len(_discover())
    if failures:
        print(f"== {len(failures)}/{total} failed  ({time.time() - t_suite:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        return 1
    print(f"== {total}/{total} passed  ({time.time() - t_suite:.1f}s) ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
