"""Minimal append-only run-logger (lab convention, see lab-repro): a JSON record per run,
written AT ENTRY with status=running + pid, rewritten on exit. A stale 'running' record whose
pid is gone is the crash evidence (SIGKILL does not unwind the stack)."""
from __future__ import annotations
import json, os, sys, subprocess, time, uuid, traceback
from contextlib import contextmanager
from pathlib import Path

def _git_sha():
    try:
        here = Path(__file__).resolve().parent
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=here, stderr=subprocess.DEVNULL).decode().strip()
        dirty = subprocess.call(["git", "diff", "--quiet"], cwd=here) != 0
        return sha + ("+dirty" if dirty else "")
    except Exception:
        return "nogit"

@contextmanager
def run_log(name: str, runs_dir: str | Path, config: dict | None = None, inputs: list | None = None):
    runs_dir = Path(runs_dir); runs_dir.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime("%Y%m%dT%H%M%S") + "_" + name + "_" + uuid.uuid4().hex[:6]
    path = runs_dir / f"{run_id}.json"
    rec = {"run_id": run_id, "name": name, "status": "running", "pid": os.getpid(), "argv": sys.argv,
           "git_sha": _git_sha(), "config": config or {}, "inputs": inputs or [], "outputs": [],
           "metrics": {}, "t_start": time.time(), "host": os.uname().nodename}
    path.write_text(json.dumps(rec, indent=1, default=str))
    try:
        yield rec
        rec["status"] = "ok"
    except BaseException:
        rec["status"] = "failed"; rec["traceback"] = traceback.format_exc(); raise
    finally:
        rec["t_end"] = time.time(); rec["path"] = str(path)
        path.write_text(json.dumps(rec, indent=1, default=str))

def crashed_runs(runs_dir: str | Path):
    """Records still 'running' whose pid is dead."""
    out = []
    for p in Path(runs_dir).glob("*.json"):
        r = json.loads(p.read_text())
        if r.get("status") == "running":
            try: os.kill(r["pid"], 0); alive = True
            except OSError: alive = False
            if not alive: out.append(r)
    return out
