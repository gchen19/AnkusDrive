"""
DriftPin host-side client — spawns `freecadcmd worker.py`, talks to it over
stdin/stdout using newline-delimited JSON.

Usage:
    with Worker() as w:
        w.call("new_document", name="part")
        box = w.call("add_primitive", kind="box", w=10, d=20, h=5)
        print(box["volume"])
"""
import json
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path


_DEFAULT_FREECADCMD_CANDIDATES = (
    "/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd",
    "/usr/bin/freecadcmd",
    "/usr/local/bin/freecadcmd",
    "/snap/bin/freecad.cmd",
)


def _resolve_freecadcmd():
    if env := os.environ.get("DRIFTPIN_FREECADCMD"):
        return env
    if found := shutil.which("freecadcmd"):
        return found
    for p in _DEFAULT_FREECADCMD_CANDIDATES:
        if os.path.isfile(p):
            return p
    return _DEFAULT_FREECADCMD_CANDIDATES[0]


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
        self.proc = subprocess.Popen(
            [freecadcmd, worker_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_dest,
            bufsize=1,
            text=True,
            start_new_session=(os.name == "posix"),
        )
        # The session leader's PGID equals its PID; capture it now so we can group-kill
        # later even after self.proc has been reaped (getpgid would then fail).
        self._pgid = self.proc.pid if os.name == "posix" else None
        if capture_stderr:
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr, daemon=True,
            )
            self._stderr_thread.start()

        self._id = 0

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
        """SIGKILL the worker's whole process group, sweeping any renderer
        subprocesses it spawned and left behind (e.g. an async render still running
        when the worker exited). Safe to call repeatedly; a no-op if the group is
        already empty. Run AFTER the worker leader itself is gone."""
        if self._pgid is None:
            return
        try:
            os.killpg(self._pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass  # group already empty, or not permitted — nothing to clean

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
