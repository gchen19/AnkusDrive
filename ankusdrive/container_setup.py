"""``ankusdrive container setup`` — one command from "nothing" to the container
substrate: the Linux-only solvers (OpenFOAM, preCICE FSI, openInjMoldSim, YADE,
Elmer, openEMS, Bempp) running from the prebuilt solver image.

Written for Windows first, where it replaces a page of hand-typed WSL + Docker steps
whose quoting differs between PowerShell and bash. Every command here is an argv
list, never a shell string. On Windows the engine lives in the WSL2 distro (Docker
Engine installed there, or Docker Desktop's WSL integration) and is reached through
``solvers.container_relay``; on Linux/macOS it is the host's own engine.

Steps, each checked before the next, each skipped when already done:

  1. an engine is reachable (Windows: a WSL distro that has ``docker``). With
     ``install_engine`` and no engine, installs Docker Engine into the distro as
     root (apt ``docker.io``, systemd, the default user in the ``docker`` group).
     That is a system change, so it is never done without the flag.
  2. the image is pulled.
  3. the solver container exists and is running, created with the exact
     ``solvers.container_run_argv`` the substrate's hints print.
  4. the config file records ``substrate = "container"`` and the IN-CONTAINER solver
     paths the image publishes in its environment, so MCP hosts (which do not see a
     shell's exported vars) resolve them too.

Returns a report; the CLI prints it. Nothing here runs a solver.
"""
from __future__ import annotations

import os
import platform
import subprocess
import tempfile

from . import config as _config
from . import solvers

# The image's published in-container paths (`docker exec <c> env`) worth persisting.
# Only ANKUSDRIVE_* names; anything else in the container's env is not ours.
_ENV_PREFIX = "ANKUSDRIVE_"


def _run(argv, timeout=120, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, **kw)


def _text(proc) -> str:
    return solvers.clean_wsl_text((proc.stdout or "") + (proc.stderr or "")).strip()


def _in_distro(script: str, root: bool = False) -> list:
    """``sh -c script`` inside the WSL distro the substrate relays into."""
    d = solvers.wsl_distro()
    return ["wsl", *(["-d", d] if d else []), *(["-u", "root"] if root else []),
            "-e", "sh", "-c", script]


# Docker Engine from the distro's own archive: no third-party apt source to trust,
# and the version the distro supports. systemd must be PID 1 for dockerd to start
# with the distro; a distro without it gets `[boot] systemd=true` and a restart.
_INSTALL_ENGINE = (
    "set -e; export DEBIAN_FRONTEND=noninteractive; "
    "command -v apt-get >/dev/null || { echo 'not an apt distro' >&2; exit 3; }; "
    "apt-get update -qq; apt-get install -y -qq docker.io >/dev/null; "
    "u=$(getent passwd 1000 | cut -d: -f1); [ -n \"$u\" ] && usermod -aG docker \"$u\"; "
    "if [ \"$(ps -p 1 -o comm=)\" = systemd ]; then systemctl enable --now docker; "
    "else echo NEEDS_SYSTEMD; fi")

_ENABLE_SYSTEMD = (
    "set -e; f=/etc/wsl.conf; touch $f; "
    "if grep -q '^systemd=true' $f; then exit 0; fi; "
    "if grep -q '^\\[boot\\]' $f; then sed -i '/^\\[boot\\]/a systemd=true' $f; "
    "else printf '\\n[boot]\\nsystemd=true\\n' >> $f; fi")


def _engine_step(install_engine: bool, rep: dict) -> bool:
    eng = solvers.container_engine()
    if platform.system() != "Windows":
        ok = _run([eng, "info", "--format", "{{.ServerVersion}}"], timeout=30)
        if ok.returncode == 0:
            rep["steps"].append(f"engine: {eng} {ok.stdout.strip()}")
            return True
        rep["error"] = (f"`{eng} info` failed — install and start {eng} first "
                        f"({_text(ok)[:200]})")
        return False
    if not solvers.wsl_available():
        rep["error"] = ("no WSL distro — in an elevated PowerShell run "
                        "`wsl --install -d Ubuntu`, reboot, open Ubuntu once to create "
                        "your user, then re-run `ankusdrive container setup`")
        return False
    rep["steps"].append(f"WSL distro: {solvers.wsl_distro()}")
    probe = _run(_in_distro(f"command -v {eng}"), timeout=120)
    if probe.returncode != 0:
        if eng != "docker" or not install_engine:
            rep["error"] = (
                f"no `{eng}` inside WSL distro {solvers.wsl_distro()!r}. Either re-run "
                "with --install-engine (installs Docker Engine in the distro via apt, "
                "as root), or install Docker Desktop and enable its WSL integration "
                "for this distro")
            return False
        inst = _run(_in_distro(_INSTALL_ENGINE, root=True), timeout=900)
        if inst.returncode != 0:
            rep["error"] = f"installing Docker Engine in WSL failed: {_text(inst)[-400:]}"
            return False
        if "NEEDS_SYSTEMD" in inst.stdout:
            en = _run(_in_distro(_ENABLE_SYSTEMD, root=True))
            if en.returncode != 0:
                rep["error"] = f"could not enable systemd in /etc/wsl.conf: {_text(en)}"
                return False
            d = solvers.wsl_distro()
            _run(["wsl", "--terminate", d] if d else ["wsl", "--shutdown"])
            rep["steps"].append("enabled systemd in /etc/wsl.conf and restarted the distro")
            _run(_in_distro("true"), timeout=120)      # boot it again, systemd as PID 1
            _run(_in_distro("systemctl enable --now docker", root=True), timeout=120)
        rep["steps"].append("installed Docker Engine (docker.io) in WSL")
    info = _run([*solvers.container_relay(), eng, "info", "--format", "{{.ServerVersion}}"],
                timeout=120)
    if info.returncode != 0:
        # a fresh `docker` group membership only applies to new logins — root works now
        rep["error"] = (f"`{solvers.container_cli()} info` failed: {_text(info)[:300]}. "
                        "If Docker was just installed, run `wsl --shutdown` once so "
                        "your user picks up the docker group, then re-run")
        return False
    rep["steps"].append(f"engine: {eng} {info.stdout.strip()} (in WSL)")
    return True


def _user() -> str:
    """The uid:gid the container runs as — the host user (#362), or on Windows the
    distro's default user, whose files on /mnt/<drive> are the Windows user's."""
    if platform.system() == "Windows":
        r = _run(_in_distro('echo "$(id -u):$(id -g)"'))
        out = _text(r)
        return out if r.returncode == 0 and ":" in out else "1000:1000"
    return f"{os.getuid()}:{os.getgid()}"


def setup(install_engine: bool = False, image: str | None = None,
          recreate: bool = False, write_config: bool = True) -> dict:
    """Provision the container substrate end to end. ``{ok, steps, error?,
    config?, container_cli, families?}``."""
    rep: dict = {"ok": False, "steps": []}
    os.environ.setdefault("ANKUSDRIVE_SUBSTRATE", "container")  # for this process
    if solvers.substrate() != "container":
        rep["error"] = ("ANKUSDRIVE_SUBSTRATE is set to "
                        f"{solvers.substrate()!r}; unset it or set it to container")
        return rep
    rep["container_cli"] = solvers.container_cli()
    if not _engine_step(install_engine, rep):
        return rep
    relay, eng = solvers.container_relay(), solvers.container_engine()
    name, image = solvers.container_name(), image or solvers._SOLVER_IMAGE

    pull = _run([*relay, eng, "pull", "-q", image], timeout=3600)
    if pull.returncode != 0:
        rep["error"] = f"pulling {image} failed: {_text(pull)[-400:]}"
        return rep
    rep["steps"].append(f"image: {image}")

    state = _run([*relay, eng, "container", "inspect", "--format", "{{.State.Running}}",
                  name])
    exists = state.returncode == 0
    if exists and recreate:
        _run([*relay, eng, "rm", "-f", name])
        exists = False
    if not exists:
        scratch = solvers.container_path(tempfile.gettempdir())
        argv = solvers.container_run_argv(_user(), scratch, image)
        made = _run(argv, timeout=300)
        if made.returncode != 0:
            rep["error"] = f"creating container {name!r} failed: {_text(made)[-400:]}"
            return rep
        rep["steps"].append(f"created container {name!r} (scratch {scratch})")
    elif state.stdout.strip() != "true":
        st = _run([*relay, eng, "start", name])
        if st.returncode != 0:
            rep["error"] = f"starting container {name!r} failed: {_text(st)}"
            return rep
        rep["steps"].append(f"started container {name!r}")
    else:
        rep["steps"].append(f"container {name!r} already running "
                            "(--recreate to rebuild it from the image)")

    env = _run([*relay, eng, "exec", name, "env"])
    published = dict(line.split("=", 1) for line in (env.stdout or "").splitlines()
                     if line.startswith(_ENV_PREFIX) and "=" in line)
    values = {"ANKUSDRIVE_SUBSTRATE": "container", **published}
    if write_config:
        rep["config"] = _config.write(values)
        rep["steps"].append(f"wrote substrate + {len(published)} in-container paths to "
                            f"{rep['config']}")
    rep["values"] = values
    # An exported env var beats config.toml, so a host path left in the environment
    # (a native Windows Elmer, say) would still win over the in-container one.
    rep["shadowed"] = sorted(k for k, v in values.items()
                             if k != "ANKUSDRIVE_SUBSTRATE"
                             and os.environ.get(k) not in (None, v))
    for k, v in values.items():                  # this process, for the report below
        os.environ[k] = v
    solvers._ctr_info_cache.clear()
    solvers._ctr_probe_cache.clear()
    caps = solvers.capabilities()
    fams = caps.get("families") or {}
    rep["families"] = {f: bool(v.get("available")) for f, v in fams.items()
                       if isinstance(v, dict) and "available" in v}
    rep["ok"] = True
    return rep


def render(rep: dict) -> str:
    out = [f"  - {s}" for s in rep["steps"]]
    if rep.get("error"):
        out.append(f"FAILED: {rep['error']}")
        return "\n".join(out)
    fams = rep.get("families") or {}
    if fams:
        ready = sorted(f for f, ok in fams.items() if ok)
        out.append("solver families ready: " + ", ".join(ready))
    for k in rep.get("shadowed") or []:
        out.append(f"WARNING: {k} is set in your environment and overrides the config "
                   f"file — remove it (Windows: `[Environment]::SetEnvironmentVariable("
                   f"'{k}', $null, 'User')`), or the container's path is not used")
    out.append(f"engine command: {rep['container_cli']}")
    out.append("Restart your MCP host (e.g. Claude Desktop) so its server re-reads the "
               "config; `ankusdrive doctor` shows every family.")
    return "\n".join(out)
