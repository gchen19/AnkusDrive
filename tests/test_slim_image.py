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
import os
import re
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "docker" / "heavy-solvers" / "Dockerfile"
WORKFLOW = REPO / ".github" / "workflows" / "heavy-image.yml"
DEPS_DIR = REPO / "docker" / "heavy-solvers" / "runtime-deps"
PREFIXES = ("yade", "openems", "fsi", "oims", "elmer", "bempp")
SOLVERS = ("openfoam", *PREFIXES)


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
    image is paying for a build it never does. The packages now live in the
    runtime-deps data files, so check those as well as the stage."""
    # comments explain WHY a -dev package is absent, so scan the package lines only
    lines = [ln for ln in _stage("runtime").splitlines() if not ln.lstrip().startswith("#")]
    for f in DEPS_DIR.glob("*.txt"):
        lines += [ln for ln in f.read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.lstrip().startswith("#")]
    body = "\n".join(lines)
    for pkg in ("build-essential", "cmake", "gfortran ", "g++", "python3-dev",
                "openfoam2512-dev", "flex", "bison", "ccache"):
        assert pkg not in body, f"{pkg!r} is a BUILD dependency; the slim image runs prebuilt solvers"
    dev_pkgs = [p for p in re.findall(r"^lib\S+-dev\b", body, re.M)]
    assert not dev_pkgs, f"a -dev package is in the runtime set: {dev_pkgs}"


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
        assert re.search(rf"^FROM\s+src-{p}-\$\{{SOLVER_SRC\}}\s+AS\s+src-{p}-on\s*$",
                         text, re.M), \
            f"src-{p}-on does not select on SOLVER_SRC"


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
    # the imports ldd cannot see — now recorded in yade's runtime-deps file
    yade_deps = (DEPS_DIR / "yade.txt").read_text(encoding="utf-8")
    for pkg in ("python3-numpy", "python3-mpmath", "ipython3"):
        assert re.search(rf"^{pkg}\b", yade_deps, re.M), \
            f"{pkg} is a YADE import, invisible to ldd — it must be listed by hand"


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


# --- selectable solvers (#422 part B) ---------------------------------------------

def _run(*argv, **kw):
    import subprocess
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                          cwd=str(REPO), **kw)


def test_every_solver_has_a_runtime_deps_file():
    """A selection installs the union of the selected solvers' files. A solver with no
    file would build an image that carries its binaries and none of its libraries."""
    for s in SOLVERS:
        f = DEPS_DIR / f"{s}.txt"
        assert f.is_file(), f"no runtime-deps file for {s}"
        pkgs = [ln.split()[0] for ln in f.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")]
        assert pkgs, f"{f.name} lists no packages"
        for ln in f.read_text(encoding="utf-8").splitlines():
            if not ln.strip() or ln.startswith("#"):
                continue
            parts = ln.split()
            assert len(parts) <= 2, f"{f.name}: '{ln}' — expected '<package> [arch]'"
            if len(parts) == 2:
                assert parts[1] in ("amd64", "arm64"), f"{f.name}: unknown arch {parts[1]!r}"


def test_the_selector_applies_closure_and_arch():
    sel = REPO / "tools" / "select_runtime_deps.sh"
    # fsi links libOpenFOAM: selecting it without openfoam would build an image whose
    # adapter cannot load, so the closure is applied for you
    assert "openfoam" in _run("bash", sel, "--closure", "fsi").stdout.split()
    assert set(_run("bash", sel, "--closure", "all").stdout.split()) == set(SOLVERS)
    amd = set(_run("bash", sel, "amd64", "all").stdout.split())
    arm = set(_run("bash", sel, "arm64", "all").stdout.split())
    # x86's __float128 runtime has no arm64 package; arm64's Elmer is a source build
    assert "libquadmath0" in amd and "libquadmath0" not in arm
    assert "elmerfem-csc" in amd and "elmerfem-csc" not in arm
    assert "libblas3" in arm and "libblas3" not in amd
    # a subset installs strictly less
    few = set(_run("bash", sel, "amd64", "openfoam").stdout.split())
    assert few < amd, "selecting one solver must not install everything"
    assert _run("bash", sel, "amd64", "nosuch").returncode != 0, "unknown solver accepted"
    assert _run("bash", sel, "sparc", "all").returncode != 0, "unknown arch accepted"


def test_the_build_wrapper_maps_a_selection_onto_the_build_args():
    out = _run("bash", REPO / "tools" / "build_solver_image.sh",
               "--solvers", "openfoam fsi", "--dry-run")
    assert out.returncode == 0, out.stderr
    assert "--build-arg WITH_OPENFOAM=on" in out.stdout
    assert "--build-arg WITH_FSI=on" in out.stdout
    for off in ("YADE", "OPENEMS", "OIMS", "BEMPP", "ELMER"):
        assert f"--build-arg WITH_{off}=off" in out.stdout, off
    assert "--target slim" in out.stdout
    bad = _run("bash", REPO / "tools" / "build_solver_image.sh", "--solvers", "nosuch",
               "--dry-run")
    assert bad.returncode != 0, "the wrapper accepted an unknown solver"


def test_selection_reaches_every_step_that_must_respect_it():
    """An ARG that half the stage ignores is worse than no selection: the image would
    install a solver's packages and not its files, or check a solver it does not have."""
    text = _text()
    for s in PREFIXES:
        assert re.search(rf"^FROM\s+src-{s}-\$\{{WITH_{s.upper()}\}}\s+AS\s+src-{s}\s*$",
                         text, re.M), f"src-{s} does not select on WITH_{s.upper()}"
        assert re.search(rf"^FROM\s+empty-prefixes\s+AS\s+src-{s}-off\s*$", text, re.M), \
            f"no empty stage for an unselected {s}"
    runtime, slim = _stage("runtime"), _stage("slim")
    for s in SOLVERS:
        arg = f"WITH_{s.upper()}"
        assert arg in runtime, f"{arg} does not reach the apt selection"
    for guard in ("WITH_YADE", "WITH_ELMER", "WITH_OPENEMS", "WITH_BEMPP",
                  "WITH_FSI", "WITH_OPENFOAM", "WITH_OIMS"):
        assert guard in slim, f"{guard} does not guard its check in the slim stage"
    assert 'echo "no solver selected' in runtime, \
        "an empty selection must fail the build, not produce a solverless image"


# --- the image states its contents (#422 part C) ----------------------------------

def test_the_image_writes_a_manifest_of_what_it_contains():
    assert "ankusdrive-write-manifest" in _stage("slim"), \
        "the slim image must record its contents for the host to read"
    gen = (REPO / "tools" / "write_solver_manifest.sh").read_text(encoding="utf-8")
    for s in SOLVERS:
        assert re.search(rf"^{s}\|/", gen, re.M), f"the manifest generator omits {s}"


def test_the_manifest_paths_match_what_the_image_publishes():
    """A manifest path that disagrees with the ENV is a lie the host would act on."""
    gen = (REPO / "tools" / "write_solver_manifest.sh").read_text(encoding="utf-8")
    env = _stage("slim")
    for line in gen.splitlines():
        m = re.match(r"^(\w+)\|(/\S+)\|", line)
        if not m:
            continue
        name, path = m.groups()
        if name in ("openfoam", "elmer", "yade", "openems", "bempp", "oims"):
            assert path in env, f"the manifest's {name} path {path} is not what slim publishes"


def test_the_manifest_generator_round_trips():
    import json
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "solvers.json"
        r = _run("bash", REPO / "tools" / "write_solver_manifest.sh", "amd64",
                 "openfoam", "yade", env={**os.environ, "MANIFEST_PATH": str(out)})
        assert r.returncode == 0, r.stderr
        data = json.loads(out.read_text(encoding="utf-8"))
        assert sorted(data["solvers"]) == ["openfoam", "yade"], data["solvers"]
        assert set(data["excluded"]) == set(SOLVERS) - {"openfoam", "yade"}
        assert data["architecture"] == "amd64"
        for name, row in data["solvers"].items():
            assert row["path"].startswith("/"), (name, row)


# --- supply chain: who built this image (#423) ------------------------------------

def test_both_images_are_signed_with_provenance_and_an_sbom():
    """A published image nobody can attribute is one users have to take on faith.
    Both manifests get Sigstore provenance AND an SBOM, pushed to the registry so a
    digest carries its own evidence."""
    wf = WORKFLOW.read_text(encoding="utf-8")
    for action in ("actions/attest-build-provenance", "actions/attest-sbom",
                   "anchore/sbom-action"):
        assert wf.count(action) == 2, \
            f"{action} must run for BOTH images (found {wf.count(action)})"
    # the subject is the multi-arch manifest digest — what a tag resolves to, and so
    # what anyone actually pulls
    assert wf.count("subject-digest: ${{ steps.pin.outputs.digest }}") == 4, wf.count(
        "subject-digest: ${{ steps.pin.outputs.digest }}")
    assert wf.count("push-to-registry: true") == 4
    # CycloneDX, because an SPDX SBOM of these images runs past the 16 MiB predicate
    # cap on per-file entries — the first signing run failed on exactly that
    assert wf.count("format: cyclonedx-json") == 2, "SBOMs must be CycloneDX"
    assert "spdx-json" not in wf


def test_an_oversized_sbom_cannot_sink_a_publish():
    """Size follows the image's contents, so it can cross the cap again as solvers are
    added. The signed image is what users need and it is pushed before this point;
    failing the job there would unpublish it over a file nobody had yet read."""
    wf = WORKFLOW.read_text(encoding="utf-8")
    assert wf.count("id: sbomfit") == 2
    assert wf.count("if: steps.sbomfit.outputs.fits == 'true'") == 2, \
        "the attestation must be conditional on the SBOM fitting"
    assert wf.count("16 * 1024 * 1024") == 2, "the cap must be checked, not assumed"
    assert wf.count("name: sbom-") == 2, \
        "an SBOM that does not fit must still be kept as an artifact"


def test_the_signing_jobs_can_mint_an_identity():
    """Sigstore signs with a short-lived OIDC token; without id-token the attestation
    steps fail at the end of a two-hour build."""
    import re as _re
    wf = WORKFLOW.read_text(encoding="utf-8")
    for job in ("manifest", "slim-manifest"):
        start = wf.index(f"\n  {job}:")
        nxt = _re.search(r"\n  [a-z-]+:\n", wf[start + 3:])
        body = wf[start:start + 3 + (nxt.start() if nxt else len(wf))]
        for perm in ("id-token: write", "attestations: write"):
            assert perm in body, f"{job} lacks {perm}"


def test_every_pinned_action_is_a_commit_sha():
    """A tag is mutable: `@v4` today is whatever that repo tags tomorrow, and these
    jobs hold an OIDC token. The repo pins by SHA everywhere; keep it that way."""
    import re as _re
    for wf in (WORKFLOW, REPO / ".github" / "workflows" / "heavy-solves.yml"):
        for m in _re.finditer(r"uses:\s+([\w.-]+/[\w.-]+(?:/[\w.-]+)*)@(\S+)", wf.read_text(encoding="utf-8")):
            action, ref = m.groups()
            assert _re.fullmatch(r"[0-9a-f]{40}", ref), \
                f"{wf.name}: {action} is pinned to {ref!r}, not a commit SHA"


def test_the_verify_script_pins_the_signer_workflow():
    """Verifying only that SOMETHING signed it is close to verifying nothing: any
    workflow in any repo can produce an attestation. The identity must name this
    repository AND the workflow file that builds images."""
    sh = (REPO / "scripts" / "verify-container-image.sh").read_text(encoding="utf-8")
    assert "--signer-workflow" in sh, "the verify script must pin the signing workflow"
    assert "heavy-image.yml" in sh
    assert "--repo" in sh
    # exit codes are the contract for anyone scripting it
    assert "exit 1" in sh and "exit 2" in sh


def test_the_lanes_check_provenance_before_they_trust_a_pin():
    wf = (REPO / ".github" / "workflows" / "heavy-solves.yml").read_text(encoding="utf-8")
    assert wf.count("gh attestation verify") == 2, \
        "both lanes that pull the pinned image must check who built it"
    assert "REQUIRE_ATTESTATION" in wf
    # the gate must precede the pull it guards, in both lanes
    for pull in ("- name: Pull solver image", "- name: Start the solver container"):
        assert wf.index("Verify the pinned image's provenance") < wf.index(pull) or \
            wf.rindex("Verify the pinned image's provenance") < wf.index(pull), \
            f"the provenance check must run before {pull!r}"


def test_doctor_reports_the_image_the_solvers_run_from():
    """"What am I running your geometry through?" should have an answer a user can
    check, not whatever the registry served that day."""
    doc = (REPO / "ankusdrive" / "doctor.py").read_text(encoding="utf-8")
    assert "container_image" in doc and "_fmt_container_image" in doc
    assert "verify-container-image.sh" in doc, \
        "doctor must print the command that checks the digest it just printed"


# --- build inputs: what the image is made OF (#423) --------------------------------

def test_no_build_input_is_fetched_over_plain_http():
    """Plain http means anyone on the path chooses what gets compiled into a solver.
    The CalculiX source became ccx_preCICE — the solid participant of every FSI solve."""
    for f in (DOCKERFILE, REPO / "scripts" / "install-solvers.sh",
              REPO / "scripts" / "add-openfoam-repo.sh",
              REPO / "tools" / "build_openinjmoldsim.sh"):
        if not f.is_file():
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            assert "http://" not in line, f"{f.name}:{i} fetches over plain http: {line.strip()}"


def test_the_calculix_source_is_checksummed():
    text = _text()
    assert "CCX_SRC_SHA256=" in text, "the CalculiX tarball must be pinned by checksum"
    assert "sha256sum -c -" in text, "…and the checksum must actually be verified"
    sh = (REPO / "scripts" / "install-solvers.sh").read_text(encoding="utf-8")
    assert "sha256sum -c -" in sh, \
        "the recipe users copy must verify it too, or only CI is protected"


def test_precice_is_pinned_by_commit_not_tag():
    """A tag can be moved; this library mediates every FSI solve."""
    import re as _re
    text = _text()
    m = _re.search(r"ARG PRECICE_SHA=([0-9a-f]{40})", text)
    assert m, "preCICE must be pinned to a full commit SHA"
    # Fetched by tag, then VERIFIED against the commit: preCICE derives its version
    # from tags, and a bare-SHA checkout builds a library the openfoam-adapter cannot
    # link. Pinning is about what you ACCEPT, not how you fetch.
    assert '[ "$got" = "$PRECICE_SHA" ]' in text, \
        "the cloned preCICE commit must be checked against the pin"


def test_the_openfoam_repo_key_is_pinned_not_piped_to_bash():
    """`curl … | bash` runs whatever that URL serves today, as root, and apt then
    trusts the key it installs — for every OpenFOAM package, on every image built
    afterwards."""
    script = REPO / "scripts" / "add-openfoam-repo.sh"
    assert script.is_file(), "no pinned repo script"
    body = script.read_text(encoding="utf-8")
    assert "PUBKEY_SHA256=" in body and "PUBKEY_FPR=" in body, \
        "pin both: a checksum catches a mangled download, a fingerprint a different key"
    assert "signed-by=" in body, \
        "scope the key to this repo — trusted.gpg.d lets it vouch for anything"
    text = _text() + (REPO / "scripts" / "install-solvers.sh").read_text(encoding="utf-8")
    assert "add-debian-repo.sh | bash" not in text
    assert "add-debian-repo.sh | sudo bash" not in text


# --- runtime privilege (#423) ------------------------------------------------------

def test_the_documented_run_command_is_least_privilege():
    import subprocess
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r)\n"
         "import os; os.environ['ANKUSDRIVE_SUBSTRATE']='container'\n"
         "from ankusdrive import solvers; print(solvers.container_run_command())"
         % str(REPO)],
        capture_output=True, text=True, cwd=str(REPO))
    cmd = out.stdout
    for flag in ("--network none", "--cap-drop ALL", "--security-opt no-new-privileges",
                 "--read-only", "--tmpfs /tmp", '--user "$(id -u):$(id -g)"'):
        assert flag in cmd, f"the documented run command lacks {flag}: {cmd}"


def test_the_docs_show_the_same_flags_as_the_code():
    """A hardened command in code and a permissive one in the docs teaches the docs."""
    doc = (REPO / "docs" / "CONTAINER_SUBSTRATE.md").read_text(encoding="utf-8")
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    for where, body in (("CONTAINER_SUBSTRATE.md", doc), ("README.md", readme)):
        run = body[body.index("docker run -d --name ankusdrive-solvers"):][:700]
        for flag in ("--network none", "--cap-drop ALL", "--read-only", "--tmpfs /tmp"):
            assert flag in run, f"{where}'s run command lacks {flag}"


def test_the_image_is_scanned_for_secrets_and_cves():
    wf = WORKFLOW.read_text(encoding="utf-8")
    assert "\n  scan:" in wf, "no scan job"
    job = wf[wf.index("\n  scan:"):]
    assert "--scanners secret --exit-code 1" in job, \
        "a secret in a published image must fail the build — there is no acceptable number"
    assert "--severity CRITICAL --ignore-unfixed" in job, \
        "the CVE gate is fixable CRITICALs; failing on unfixable ones wedges the pipeline"
    assert "upload-sarif" in job, "CVEs should land in code scanning, not just a log"
    assert (REPO / ".trivyignore").is_file(), "waivers need a reviewed home"


# --- the published sets, and the lane that tests what users pull (#422) ------------

def _job(wf: str, name: str) -> str:
    start = wf.index(f"\n  {name}:")
    nxt = re.search(r"\n  [a-z-]+:\n", wf[start + 3:])
    return wf[start:start + 3 + (nxt.start() if nxt else len(wf))]


def test_full_and_openfoam_are_both_published_and_signed():
    """#422 promises two prebuilt tags: `:full` (= latest) and `:openfoam`. Each needs
    its own per-arch build, its own manifest, its own signature — and the openfoam
    build must actually switch the other six solvers off."""
    wf = WORKFLOW.read_text(encoding="utf-8")
    slim, publish = _job(wf, "slim"), _job(wf, "slim-manifest")
    assert '["full","openfoam"]' in slim, "main must build both published sets"
    for solver in PREFIXES:
        assert f"WITH_{solver.upper()}=${{{{ matrix.set == 'full' && 'on' || 'off' }}}}" \
            in slim, f"the openfoam set does not switch {solver} off"
    assert "slim-digest-${{ matrix.set }}-" in slim and \
        "slim-digest-${{ matrix.set }}-*" in publish, \
        "each set must publish its OWN digests, or one manifest joins both sets"
    assert "set: [full, openfoam]" in publish
    assert "type=raw,value=${{ matrix.set }}" in publish, "no :full / :openfoam tag"
    assert "'{{is_default_branch}}'" in publish, ":latest must stay default-branch only"
    assert "Attest build provenance (slim)" in publish, "every published set is signed"
    # a subset build must never overwrite the full build's layer cache
    assert "matrix.set == 'full' && format('type=registry,ref={0}:cache-slim" in slim


def test_the_subset_is_preflighted_from_the_host():
    """The manifest is only worth something if the HOST honours it: the subset job
    runs the container preflight with every other solver expected to be excluded."""
    job = _job(WORKFLOW.read_text(encoding="utf-8"), "slim-subset")
    assert "scripts/ci-container-preflight.sh" in job
    assert "EXPECT_SOLVERS: openfoam" in job
    pre = (REPO / "scripts" / "ci-container-preflight.sh").read_text(encoding="utf-8")
    assert "does not include" in pre, "an excluded solver must be checked BY its hint"
    assert "EXPECT_SOLVERS is set but the image has no" in pre, \
        "a subset image without a manifest must fail the preflight"


def test_lane_b_runs_on_the_image_users_pull():
    """Lane B exists to test the container substrate; testing it against the CI image
    proved the relay but not the image anyone ships with it (#422)."""
    wf = (REPO / ".github" / "workflows" / "heavy-solves.yml").read_text(encoding="utf-8")
    lane = _job(wf, "heavy-solves-container")
    m = re.search(r"SOLVER_IMAGE: (\S+)", lane)
    assert m, "lane B has no SOLVER_IMAGE pin"
    assert m.group(1).startswith("ghcr.io/gchen19/ankusdrive-solvers@sha256:"), \
        f"lane B must pin the slim image by digest, not {m.group(1)}"
    assert "ankusdrive-heavy" not in lane, "lane B still references the CI image"
    assert "--tmpfs /tmp" in lane, "lane B must start the container as the docs do"


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
