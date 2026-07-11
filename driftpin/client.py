"""
DriftPin host-side client — spawns `freecadcmd worker.py`, talks to it over
stdin/stdout using newline-delimited JSON.

Usage:
    with Worker() as w:
        w.call("new_document", name="part")
        box = w.call("add_primitive", kind="box", w=10, d=20, h=5)
        print(box["volume"])
"""
import glob
import json
import os
import platform
import shutil
import signal
import subprocess
import threading
from pathlib import Path


# The console binary is `freecadcmd` on POSIX and `freecadcmd.exe` / `FreeCADCmd.exe`
# on Windows. `shutil.which` (below) tries every name against PATH (honoring Windows
# PATHEXT), so these names are also what we look for by name; the per-OS lists here are
# the fallback fixed/globbed install locations when the binary isn't on PATH.
_FREECADCMD_NAMES = ("freecadcmd", "FreeCADCmd", "freecad.cmd")

# Per-OS default install locations, most-preferred first. Windows and Linux entries may
# contain glob wildcards (versioned dirs like ``FreeCAD 1.1``); macOS is a fixed bundle
# path. Resolution: DRIFTPIN_FREECADCMD env → PATH (any name) → these globbed candidates.
_DEFAULT_FREECADCMD_CANDIDATES = {
    "Darwin": (
        "/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd",
    ),
    "Linux": (
        "/usr/bin/freecadcmd",
        "/usr/local/bin/freecadcmd",
        "/snap/bin/freecad.cmd",
        # AppImage extractions / manual installs commonly land here.
        os.path.expanduser("~/.local/bin/freecadcmd"),
    ),
    "Windows": (
        # Program Files installer layout, version-globbed (FreeCAD 1.1, 1.0, 0.21, …).
        r"C:\Program Files\FreeCAD *\bin\freecadcmd.exe",
        r"C:\Program Files\FreeCAD*\bin\freecadcmd.exe",
        r"C:\Program Files (x86)\FreeCAD *\bin\freecadcmd.exe",
        # Per-user install (winget / "install for me only").
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\FreeCAD *\bin\freecadcmd.exe"),
    ),
}


def _freecadcmd_candidates():
    """Every default install-location candidate for this OS, glob-expanded and
    sorted newest-version-last so the highest version wins. Pure lookup — the
    caller filters for existence."""
    out = []
    for pat in _DEFAULT_FREECADCMD_CANDIDATES.get(platform.system(), ()):
        if any(ch in pat for ch in "*?["):
            out.extend(sorted(glob.glob(pat)))   # e.g. FreeCAD 1.0 < FreeCAD 1.1
        else:
            out.append(pat)
    # Overlapping globs (``FreeCAD *`` and ``FreeCAD*``) can name the same install;
    # order-preserving dedup so we don't probe/report a path twice.
    return list(dict.fromkeys(out))


def _resolve_freecadcmd():
    # env override -> config file (~/.config/driftpin/config.toml `freecadcmd`;
    # the persistent layer MCP hosts' minimal env can't strip, issue #199)
    from driftpin import config as _config
    if explicit := _config.get("DRIFTPIN_FREECADCMD"):
        return explicit
    for name in _FREECADCMD_NAMES:
        if found := shutil.which(name):     # PATH lookup (Windows PATHEXT-aware)
            return found
    candidates = _freecadcmd_candidates()
    for p in reversed(candidates):          # highest globbed version first
        if os.path.isfile(p):
            return p
    # Nothing resolved: return a best-effort placeholder so the eventual Popen
    # error names a plausible path. Prefer the last (newest) glob candidate if the
    # OS had any pattern, else the bare binary name (which surfaces a clean
    # "not found" rather than a wrong-OS path).
    return candidates[-1] if candidates else _FREECADCMD_NAMES[0]


FREECADCMD = _resolve_freecadcmd()
WORKER_SCRIPT = str(Path(__file__).resolve().parent / "worker.py")


class WorkerError(Exception):
    def __init__(self, payload):
        self.type = payload.get("type", "Unknown")
        self.remote_message = payload.get("message", "")
        self.remote_traceback = payload.get("traceback", "")
        super().__init__(f"{self.type}: {self.remote_message}")


class WorkerDied(RuntimeError):
    pass


class Worker:
    def __init__(
        self,
        freecadcmd=FREECADCMD,
        worker_script=WORKER_SCRIPT,
        capture_stderr=False,
        boot_timeout=15.0,
    ):
        self._stderr_buf = []
        stderr_dest = subprocess.PIPE if capture_stderr else subprocess.DEVNULL
        # start_new_session=True (POSIX) puts freecadcmd in its own session/process
        # group, so external renderer subprocesses it spawns (povray, luxcoreconsole,
        # …) inherit that group. Orphaning on worker death does NOT change a process's
        # group, so shutdown() can still sweep them with a single group kill — without
        # it, a render in flight when the worker dies leaks a 100%-CPU orphan.
        #
        # Windows has no process groups/sessions in the POSIX sense; the equivalent is
        # CREATE_NEW_PROCESS_GROUP so freecadcmd roots a new group, and _reap_tree()
        # sweeps the tree with `taskkill /T` on shutdown (see below). Without it a
        # renderer in flight when the worker dies is orphaned exactly as on POSIX.
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen(
            [freecadcmd, worker_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_dest,
            bufsize=1,
            text=True,
            start_new_session=(os.name == "posix"),
            creationflags=creationflags,
        )
        # The session leader's PGID equals its PID; capture it now so we can group-kill
        # later even after self.proc has been reaped (getpgid would then fail). On
        # Windows there is no PGID — _reap_tree() keys off self.proc.pid directly.
        self._pgid = self.proc.pid if os.name == "posix" else None
        if capture_stderr:
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr, daemon=True,
            )
            self._stderr_thread.start()

        self._id = 0
        # Serializes call(): the worker loop is strictly one request → one
        # response, so two host threads must never interleave stdin writes /
        # stdout readline()s on the same process (that races the id/response
        # pairing → "id mismatch" / WorkerDied). FastMCP runs sync tools in an
        # anyio thread pool, so concurrent tool calls land here in parallel; this
        # lock makes each call atomic without making the worker multi-threaded.
        self._call_lock = threading.Lock()

        ready = self._read_response(timeout=boot_timeout)
        if not ready.get("ready"):
            raise WorkerDied(f"worker did not report ready, got: {ready!r}")
        self.freecad_version = ready.get("freecad")

    def _drain_stderr(self):
        for line in self.proc.stderr:
            self._stderr_buf.append(line)

    def get_stderr(self):
        return "".join(self._stderr_buf)

    def _read_response(self, timeout=None):
        if timeout is None:
            line = self.proc.stdout.readline()
        else:
            result_holder = {}

            def _read():
                result_holder["line"] = self.proc.stdout.readline()

            t = threading.Thread(target=_read, daemon=True)
            t.start()
            t.join(timeout)
            if t.is_alive():
                raise TimeoutError(f"no response within {timeout}s")
            line = result_holder["line"]

        if not line:
            raise WorkerDied("worker closed stdout (process likely died)")
        try:
            return json.loads(line)
        except json.JSONDecodeError as e:
            raise WorkerDied(f"non-JSON on stdout (stdio hygiene broken): {line!r}") from e

    def call(self, _method, _timeout=120.0, **params):
        # _method/_timeout are underscored (like the worker protocol's reserved
        # keys) so a tool param of the same name — e.g. tolerance_stackup(method=)
        # — rides through **params without colliding with the positional arg.
        #
        # Held for the whole write→read round-trip so concurrent callers of one
        # Worker are serialized (see _call_lock in __init__). Distinct Workers —
        # e.g. one per MCP workspace — hold distinct locks and still run in
        # parallel; only same-process calls contend.
        with self._call_lock:
            if self.proc.poll() is not None:
                raise WorkerDied(f"worker exited with code {self.proc.returncode}")
            self._id += 1
            mid = f"r{self._id}"
            req = {"id": mid, "method": _method, "params": params}
            try:
                self.proc.stdin.write(json.dumps(req) + "\n")
                self.proc.stdin.flush()
            except BrokenPipeError as e:
                raise WorkerDied("worker stdin closed") from e

            resp = self._read_response(timeout=_timeout)
            if resp.get("id") != mid:
                raise RuntimeError(f"id mismatch: expected {mid}, got {resp.get('id')!r}")
            if "error" in resp:
                raise WorkerError(resp["error"])
            return resp["result"]

    def _reap_group(self):
        """Kill the worker's whole process tree, sweeping any renderer subprocesses it
        spawned and left behind (e.g. an async render still running when the worker
        exited). Safe to call repeatedly; a no-op if the tree is already gone. Run AFTER
        the worker leader itself is gone. POSIX uses a single group SIGKILL; Windows has
        no process groups, so it walks the tree with `taskkill /T /F`."""
        if os.name == "nt":
            self._reap_tree_windows()
            return
        if self._pgid is None:
            return
        try:
            os.killpg(self._pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass  # group already empty, or not permitted — nothing to clean

    def _reap_tree_windows(self):
        """Windows counterpart of the POSIX group-kill: `taskkill /PID <pid> /T /F`
        terminates the worker and every descendant it spawned (/T = tree), forcefully
        (/F). Best-effort — if the tree is already gone taskkill exits non-zero, which
        we swallow. Uses the captured pid so it works even after self.proc is reaped."""
        try:
            subprocess.run(
                ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, ValueError):
            pass  # taskkill missing (unlikely) or pid unusable — nothing we can do

    def shutdown(self, timeout=5.0):
        if self.proc.poll() is not None:
            self._reap_group()                       # leader gone; sweep stragglers
            return
        try:
            self.proc.stdin.write(json.dumps({"id": "shutdown", "method": "shutdown"}) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        finally:
            # The worker exits without reaping renderer children it launched in
            # background threads; sweep the process group so none are orphaned.
            self._reap_group()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
