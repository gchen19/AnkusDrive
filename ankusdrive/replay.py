"""Replay a session: the journal as a Python script that regenerates it (#408, epic #309).

Two halves.

**:class:`Session`** is what an exported script runs. ``s.<tool>(**args)`` calls the
same function the MCP server serves: 273 of the 282 tools pass straight through to the
worker, so a script replays through the public surface with no MCP protocol.
Only tools the annotation registry knows are callable, so a script can't reach
internals. ``run_script`` still sends ``origin="mcp"`` and the worker still refuses it
when ``ANKUSDRIVE_ALLOW_RUN_SCRIPT`` is off. :meth:`Session.wait` polls a job to its
end, and :meth:`Session.check` fails at the first result that drifted. A result that
says a solver is missing or a tool is disabled raises instead of passing quietly.

**:func:`export`** turns journal entries (``journal.snapshot(ws)["entries"]``) into
that script. It is pure Python (no FreeCAD, no ``mcp``):

* **Handles are linked by value.** Worker handles are per-prefix counters, which drift
  after a failed call, a ``run_script`` auto-register, or a restart. A result string
  under ``handle`` / ``*_handle`` / ``*handles`` / ``job_id`` / ``doc`` / ``analysis``
  becomes a variable, and a later argument equal to it uses the variable. A result
  that only echoes one of its own arguments binds nothing.
* **Job polls collapse.** ``job_status`` / ``job_list`` are dropped. The first terminal
  ``job_result`` / ``render_job`` for a job becomes ``s.wait(job)``. A job nobody
  fetched is still waited for, so the replay's solve runs to completion.
* **Read-only calls are pruned** (the ``READ_ONLY`` class in ``tool_annotations``)
  unless a kept call uses a value they returned, or (with ``checkpoints``) they returned
  a number worth checking. ``include_read_only=True`` keeps them all. **Analysis calls
  are never pruned** (:func:`analysis_tools` — the ``hand_calcs``, ``simulation`` and
  ``fem`` families): a closed-form estimate is read-only in the MCP sense, but it is
  not an inspection — it is the derivation, and a transcript that drops it is not an
  audit record of anything (#433).
* **Arguments equal to the tool's default are omitted** (defaults read from
  ``mcp_server.py`` by ``ast``, so the exporter never imports the server).
* **Checkpoints:** numeric volume / area / mass / ``max_*`` / ``min_*`` / frequency
  results become ``s.check(...)``. Relative tolerance is 1e-6 for geometry and 1e-3
  for solver output (job results, and ``*_results`` / ``*_result_probe`` reads).
  An **analysis or solve** result is checked *whole* instead: every finite scalar it
  reports (its own keys, a job's ``result``, and the house ``gate`` verdict) becomes a
  checkpoint, minus wall-clock bookkeeping. Its verdict fields — ``ok``, ``pass``,
  ``fidelity``, ``basis``, ``correlation``, ``solver``, ``status`` — become
  ``s.expect(...)``, so a replay that silently drops to a different correlation, a
  different solver, or a failing gate stops there.
* **Paths are redacted.** Absolute paths become ``WORKDIR / "<relative>"``, so a shared
  script carries no ``/Users/<name>/…``. Paths the session read but did not write are
* **The environment is carried with it.** With a ``provenance`` record the script
  opens with a ``PROVENANCE`` literal — AnkusDrive / Python / FreeCAD / platform /
  substrate, and every solver the session reached, with the path it resolved to, the
  substrate it was reached through, and its probed version — and calls
  ``s.provenance(PROVENANCE)``, which re-resolves all of it and prints each
  difference. A replay on a different Elmer is not a failure; it is the finding.
* **``run_script`` carries its content hash**, so a report can cite the code by digest.
* **What the script can't reproduce is stated**, as comments in the script and in
  ``warnings``: ``run_script`` code, failed calls (kept as comments), a worker
  replaced without a ``restart_worker`` call, a truncated journal, and prerequisites.
  Aborted transactions replay as recorded; ``prune_aborted=True`` drops them.

It covers one workspace; workspace management calls are skipped.
"""
from __future__ import annotations

import ast
import builtins
import difflib
import functools
import keyword
import math
import pprint
import re
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

_PKG = Path(__file__).resolve().parent

# --- Session ---------------------------------------------------------------------


class ReplayError(RuntimeError):
    """A replayed step failed, drifted, or could not run in this install."""


def _known_tools() -> frozenset:
    from ankusdrive import tool_annotations as ta
    return frozenset(ta.READ_ONLY) | frozenset(ta.ADDITIVE) | frozenset(ta.DESTRUCTIVE)


class Session:
    """Drive AnkusDrive's tools from Python, the way an exported transcript does.

    ::

        with Session() as s:
            box = s.add_primitive(kind="box", w=40, d=20, h=5)["handle"]
            s.check(s.mass_properties(handle=box), "volume", 4000.0)

    ``strict`` (default True) raises :class:`ReplayError` when a tool returns a
    structured refusal — a missing solver, or ``run_script`` disabled — instead of
    letting the script go on without the result it needed."""

    def __init__(self, strict: bool = True, *, server=None):
        if server is None:
            from ankusdrive import mcp_server as server
        self._srv = server
        self._known = _known_tools()
        self.strict = strict
        self.step = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        """Shut down every worker this session started."""
        self._srv._cleanup()

    def __getattr__(self, name: str):
        if name.startswith("_") or name not in self._known:
            near = difflib.get_close_matches(name, sorted(self._known), n=3)
            hint = f"; did you mean {', '.join(near)}?" if near else ""
            raise AttributeError(f"AnkusDrive has no tool {name!r}{hint}")
        fn = getattr(self._srv, name)

        @functools.wraps(fn)
        def call(**kwargs):
            try:
                result = fn(**kwargs)
            except Exception as e:
                raise ReplayError(f"{self._where()}{name} failed: {e}") from e
            if self.strict:
                self._refuse_if_unavailable(name, result)
            return result
        return call

    def _where(self) -> str:
        return f"step {self.step}: " if self.step is not None else ""

    def _refuse_if_unavailable(self, name: str, result: Any) -> None:
        if not isinstance(result, dict) or result.get("ok") is not False:
            return
        if result.get("status") == "disabled" or "solver" in result:
            why = result.get("reason") or result.get("status")
            how = result.get("enable") or result.get("install") or ""
            raise ReplayError(f"{self._where()}{name} could not run in this install: {why}"
                              + (f" — {how}" if how else ""))

    def wait(self, job: Any, timeout: float | None = None, poll_s: float = 0.5) -> dict:
        """Poll a job (its id, or the ``*_submit`` result) until it ends. Returns the
        final ``job_result`` (``render_job`` for renders); raises on failure or timeout."""
        job_id = job.get("job_id") if isinstance(job, dict) else job
        if not job_id:
            raise ReplayError(f"{self._where()}nothing to wait for: {job!r}")
        render = str(job_id).startswith("render_job")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            state = (self.render_job(job_id=job_id) if render
                     else self.job_status(job_id=job_id))
            if state.get("status") != "running":
                break
            if deadline is not None and time.monotonic() > deadline:
                raise ReplayError(f"{self._where()}job {job_id} still running after {timeout}s")
            time.sleep(poll_s)
        final = state if render else self.job_result(job_id=job_id)
        if final.get("status") == "failed":
            raise ReplayError(f"{self._where()}job {job_id} failed: {final.get('error')}")
        return final

    def check(self, result: Any, path, expected: float, rel: float = 1e-6,
              abs_tol: float = 1e-9) -> Any:
        """Assert ``result[path]`` is within tolerance of ``expected``; return it."""
        keys = (path,) if isinstance(path, (str, int)) else tuple(path)
        got = result
        try:
            for k in keys:
                got = got[k]
        except (KeyError, IndexError, TypeError):
            raise ReplayError(f"{self._where()}result has no {list(keys)}") from None
        if not isinstance(got, (int, float)) or not math.isclose(
                got, expected, rel_tol=rel, abs_tol=abs_tol):
            raise ReplayError(f"{self._where()}{'.'.join(map(str, keys))} = {got!r}, "
                              f"recorded {expected!r} (rel {rel})")
        return got

    def expect(self, result: Any, path, recorded) -> Any:
        """Assert ``result[path]`` still equals ``recorded`` exactly; return it.

        The non-numeric twin of :meth:`check`, for the verdict fields an analysis
        carries — ``ok``, ``pass``, ``fidelity``, ``basis``, ``correlation``,
        ``solver``, ``status``. A replay that reaches a different solver, falls back
        to a different correlation, or fails a gate that passed is a different
        analysis, and stops here rather than producing a number that looks fine."""
        keys = (path,) if isinstance(path, (str, int)) else tuple(path)
        got = result
        try:
            for k in keys:
                got = got[k]
        except (KeyError, IndexError, TypeError):
            raise ReplayError(f"{self._where()}result has no {list(keys)}") from None
        if got != recorded:
            raise ReplayError(f"{self._where()}{'.'.join(map(str, keys))} = {got!r}, "
                              f"recorded {recorded!r}")
        return got

    def provenance(self, recorded: dict, strict: bool | None = None) -> list:
        """Compare the recorded environment with this machine's and report every
        difference. Returns the difference lines (empty when they agree).

        Prints rather than raises by default: replaying on a newer Elmer is not an
        error, it is the thing an audit wants stated. ``strict=True`` raises instead
        — for a re-verification that is only meaningful on the same environment."""
        from ankusdrive import provenance as prov
        lines = prov.drift(recorded)
        if not lines:
            print("provenance: environment matches the recording")
            return lines
        print("provenance: the environment DIFFERS from the recording —")
        for line in lines:
            print(f"  ! {line}")
        if strict:
            raise ReplayError("environment differs from the recording: " + "; ".join(lines))
        return lines


# --- exporter ------------------------------------------------------------------------

SKIP_TOOLS = {
    "use_workspace": "workspace management",
    "list_workspaces": "workspace management",
    "close_workspace": "workspace management",
    "job_status": "job polling (replaced by s.wait)",
    "job_list": "job polling (replaced by s.wait)",
    "session_transcript": "the transcript tool itself",
    "save_transcript": "the transcript tool itself",
}
_JOB_FETCH = {"job_result", "render_job"}
_LINK_KEYS = {"handle", "job_id", "doc", "analysis"}
# volume / volume_mm3 / surface_area_mm2 / mass_kg / max_von_mises / frequencies_hz ...
_CHECK_KEY = re.compile(r"^((volume|area|surface_area|mass)(_[a-z0-9]+)?|max_[a-z0-9_]+|"
                        r"min_[a-z0-9_]+|frequenc[a-z0-9_]*)$")
_ABS_PATH = re.compile(r"^(/|~[/\\]|[A-Za-z]:[\\/])")
_SOLVER_READ = re.compile(r"(_results|_result_probe)$")
# Wall-clock and bookkeeping keys: real numbers, but they measure the machine, not the
# part. Checking them would fail every replay for the wrong reason.
_NOT_A_CHECK = {"elapsed_s", "elapsed", "wall_time_s", "cpu_time_s", "runtime_s",
                "solve_time_s", "seq", "pid", "timestamp", "ts", "t_submit", "t_finish"}
# Verdict fields — what the analysis was, not what it measured. Compared exactly.
_EXPECT_KEY = ("ok", "pass", "fidelity", "basis", "correlation", "solver", "status",
               "converged", "mode")
# Parameters naming a path the call READS. The exporter's default is the opposite —
# a path argument is somewhere the call writes — which is right for `path` / `out_dir`
# and wrong for every prepared input: a file the replay needs before it starts, and
# which therefore belongs under Prerequisites. `case_dir` / `sif` / `stl_path` are the
# solver half (a prepared .sif + mesh, an OpenFOAM case tree, an STL to trace or
# slice); the solver also WRITES its output into a case dir, but what matters for a
# replay is that the deck has to be there first (#437).
_INPUT_PARAMS = {"registry", "manifest", "manifest_path", "input", "source", "src", "file",
                 "lockfile", "case_dir", "cooling_case_dir", "sif", "stl_path"}
_RESERVED = {"s", "sys", "Path", "Session", "WORKDIR"}


@functools.lru_cache(maxsize=1)
def tool_defaults() -> dict:
    """``{tool: {param: default}}`` for every ``@mcp.tool()``, read by ``ast``."""
    tree = ast.parse((_PKG / "mcp_server.py").read_text(encoding="utf-8"))
    out = {}
    for n in tree.body:
        if not (isinstance(n, ast.FunctionDef) and any(
                isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "tool"
                for d in n.decorator_list)):
            continue
        params, defs = n.args.args, n.args.defaults
        table = {}
        for p, d in zip(params[len(params) - len(defs):], defs):
            try:
                table[p.arg] = ast.literal_eval(d)
            except ValueError:
                pass
        for p, d in zip(n.args.kwonlyargs, n.args.kw_defaults):
            if d is not None:
                try:
                    table[p.arg] = ast.literal_eval(d)
                except ValueError:
                    pass
        out[n.name] = {"order": [p.arg for p in params + n.args.kwonlyargs], "defaults": table}
    return out


def _load_sibling(stem: str):
    """Load a sibling module by path — the exporter reads the registries without
    importing the package (and so without pulling in the server)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"_{stem}_for_replay", _PKG / f"{stem}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_only() -> frozenset:
    return frozenset(_load_sibling("tool_annotations").READ_ONLY)


@functools.lru_cache(maxsize=1)
def analysis_tools() -> frozenset:
    """The tools whose call *is* the derivation: the ``hand_calcs`` (closed-form
    estimates and gates), ``simulation`` (external-solver submits) and ``fem``
    families, from the toolset registry.

    Most are ``READ_ONLY`` — they compute a number and touch nothing — so the
    read-only prune would drop them, and a thermal transcript would consist of the
    CAD around the thermal work with the thermal work removed (#433). An inspection
    can be re-read at any time; an analysis is the record of a decision."""
    fam = _load_sibling("toolsets").FAMILIES
    return frozenset().union(*(fam[f] for f in ("hand_calcs", "simulation", "fem")
                               if f in fam))


def _walk_strings(value, path=()):
    """Yield (path, parent_key, string) for every string inside a JSON value."""
    if isinstance(value, str):
        yield path, (path[-1] if path else None), value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _walk_strings(v, path + (k,))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk_strings(v, path + (i,))


def _linkable(path) -> bool:
    keys = [k for k in path if isinstance(k, str)]
    if not keys:
        return False
    k = keys[-1]
    if isinstance(path[-1], int):              # an item of a list under `k`
        return k.endswith("handles")
    return k in _LINK_KEYS or k.endswith("_handle")


def _arg_strings(args) -> set:
    return {s for _, _, s in _walk_strings(args)}


def _bindings(entry) -> list:
    """[(value, path)] a successful call's result binds: link-key strings that are not
    an echo of one of the call's own arguments."""
    if not entry.get("ok") or not isinstance(entry.get("result"), (dict, list)):
        return []
    echoes = _arg_strings(entry.get("args") or {})
    seen, out = set(), []
    for path, _k, value in _walk_strings(entry["result"]):
        if value and value not in echoes and value not in seen and _linkable(path):
            seen.add(value)
            out.append((value, path))
    return out


def _ident(value: str, key: str) -> str:
    base = re.sub(r"\W", "_", value)
    if key in ("doc", "analysis") or not base or base[0].isdigit() or keyword.iskeyword(base) \
            or base in _RESERVED or hasattr(builtins, base):
        base = f"{key}_{base}"
    return base


class _Paths:
    """Map absolute paths onto ``WORKDIR`` without keeping the recording machine's layout.

    Pure path arithmetic in the *recorded* paths' flavor, never ``os.path``: a session
    recorded on macOS can be exported on Windows and the other way round, and ``~`` is
    kept as a component rather than expanded to the exporting machine's home."""

    def __init__(self, paths, workdir=None):
        self.paths = sorted(set(paths))
        self.root = None
        windows = any(re.match(r"^([A-Za-z]:|~\\)", p) for p in self.paths)
        self.flavor = flavor = PureWindowsPath if windows else PurePosixPath
        if workdir:
            self.root = flavor(workdir)
        elif self.paths:
            parts = [flavor(p).parts for p in self.paths]
            if len(parts) == 1:                 # one file: root at its own folder
                parts = [parts[0][:-1]]
            common = []
            for group in zip(*parts):
                if len({g.lower() if windows else g for g in group}) != 1:
                    break
                common.append(group[0])
            if len(common) > 1:                 # never the bare filesystem root
                self.root = flavor(*common)
        self.dirs: dict = {}

    def rel(self, p: str) -> tuple:
        pp = self.flavor(p)
        if self.root is not None:
            try:
                return pp.relative_to(self.root).parts
            except ValueError:
                pass
        d = str(pp.parent)
        if d not in self.dirs:
            self.dirs[d] = f"dir{len(self.dirs) + 1}"
        return (self.dirs[d], pp.name)


def _is_path(value: str) -> bool:
    return len(value) > 1 and "\n" not in value and bool(_ABS_PATH.match(value))


def _collect_paths(entries) -> list:
    return [v for e in entries for _, _, v in _walk_strings(e.get("args") or {}) if _is_path(v)]


def _produced_paths(entries) -> dict:
    """``{path: seq}`` for every path a call RETURNED, at the first step that returned
    it. A tool that hands back where it worked — a solver's ``case_dir``, an export's
    resolved path — names a path the session made, not one the user supplied."""
    out: dict = {}
    for e in sorted(entries, key=lambda e: e["seq"]):
        if not e.get("ok"):
            continue
        for _path, _k, v in _walk_strings(e.get("result") or {}):
            if _is_path(v) and v not in out:
                out[v] = e["seq"]
    return out


def _scopes(result) -> list:
    """The places a result keeps its numbers: itself, a job's ``result``, and the
    house ``gate`` verdict on either."""
    if not isinstance(result, dict):
        return []
    out = [((), result)]
    for prefix, scope in list(out):
        for key in ("result", "gate"):
            inner = scope.get(key)
            if isinstance(inner, dict):
                out.append((prefix + (key,), inner))
                nested = inner.get("gate")
                if key == "result" and isinstance(nested, dict):
                    out.append((prefix + (key, "gate"), nested))
    return out


def _checks(result, wide: bool = False) -> list:
    """[(path, number)] a result offers as checkpoints.

    Narrow (the default, for geometry and everything else): top-level numeric volume /
    area / mass / max_* / min_* / frequency keys. **Wide** (an analysis or a solve):
    every finite scalar the result reports, minus wall-clock bookkeeping — for an
    audit the whole reported result is the claim, not the two keys that happen to
    match a name pattern."""
    out = []
    for prefix, scope in _scopes(result):
        for k, v in scope.items():
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
                continue
            if str(k) in _NOT_A_CHECK:
                continue
            if wide or _CHECK_KEY.match(str(k)):
                out.append((prefix + (k,), v))
    return out


def _expects(result) -> list:
    """[(path, value)] an analysis result offers as exact checks: its verdict fields
    (bool or short string). What the analysis *was* — which solver, which correlation,
    whether the gate passed — as distinct from what it measured."""
    out = []
    for prefix, scope in _scopes(result):
        for k in _EXPECT_KEY:
            if k not in scope:
                continue
            v = scope[k]
            if k == "status" and v in ("running", "queued"):
                continue            # in flight when it was recorded, not a verdict
            if isinstance(v, bool) or (isinstance(v, str) and 0 < len(v) <= 64):
                out.append((prefix + (k,), v))
    return out


def _call_text(tool, args, expr) -> str:
    sig = tool_defaults().get(tool, {"order": [], "defaults": {}})
    order = {p: i for i, p in enumerate(sig["order"])}
    parts = []
    for k in sorted(args, key=lambda k: (order.get(k, len(order)), k)):
        v = args[k]
        if k in sig["defaults"] and sig["defaults"][k] == v and type(sig["defaults"][k]) in (type(v), type(None), int, float):
            continue
        parts.append(f"{k}={expr(v)}")
    return f"s.{tool}({', '.join(parts)})"


def _provenance_lines(rec: dict) -> list:
    """The environment record as docstring lines — what an auditor reads first."""
    env = rec.get("env") or {}
    plat = env.get("platform") or {}
    fc = (env.get("freecad") or {}).get("version")
    bits = [f"ankusdrive {env.get('ankusdrive', '?')}", f"Python {env.get('python', '?')}"]
    if fc:
        bits.append(f"FreeCAD {fc}")
    bits.append(" ".join(x for x in (plat.get("system"), plat.get("machine")) if x) or "?")
    if env.get("substrate"):
        bits.append(f"solvers via {env['substrate']}")
    lines = ["", "Recorded on:", "  " + ", ".join(bits)]
    solvers = rec.get("solvers") or {}
    if solvers:
        lines.append("Solvers this session reached:")
        for name, info in sorted(solvers.items()):
            where = info.get("path") or info.get("module") or "not resolved"
            ver = info.get("version") or f"version unknown ({info.get('version_source', '?')})"
            via = info.get("via") or "host"
            lines.append(f"  - {name} {ver} — {where} (via {via})")
            for old_where in info.get("moved") or []:
                lines.append(f"      earlier in the session: "
                             f"{old_where.get('path') or old_where.get('module')} "
                             f"(via {old_where.get('via', 'host')})")
    lines.append("s.provenance(PROVENANCE) re-resolves all of it here and prints what differs.")
    return lines


def export(entries: list, *, workspace: str = "default", include_read_only: bool = False,
           checkpoints: bool = True, prune_aborted: bool = False, workdir: str | None = None,
           truncated: bool = False, generator: str | None = None,
           provenance: dict | None = None) -> dict:
    """Turn journal entries for one workspace into a replay script.

    ``provenance`` is the environment record (:func:`ankusdrive.provenance.from_entries`)
    to carry with the script; omit it for a script with no environment header.

    Returns ``{script, calls, exported, skipped, warnings, prerequisites}``: ``calls``
    is how many entries came in, ``exported`` how many became live calls, ``skipped``
    ``[{seq, tool, reason}]``."""
    entries = sorted(entries, key=lambda e: e["seq"])
    read_only = _read_only()
    analysis = analysis_tools()
    warnings: list[str] = []
    skipped: list[dict] = []
    if truncated:
        warnings.append("the journal hit its entry cap; calls after it were not recorded, "
                        "so the script stops short of the end of the session")

    # 1. what each entry is ------------------------------------------------------------
    kind = {}                                   # seq -> call|fetch|fail|skip|prune
    job_waited = set()
    for e in entries:
        tool, seq = e["tool"], e["seq"]
        if tool in SKIP_TOOLS:
            kind[seq] = "skip"
            skipped.append({"seq": seq, "tool": tool, "reason": SKIP_TOOLS[tool]})
        elif not e.get("ok"):
            kind[seq] = "fail"
        elif tool in _JOB_FETCH:
            jid = (e.get("args") or {}).get("job_id")
            status = (e.get("result") or {}).get("status") if isinstance(e.get("result"), dict) else None
            if status in ("done", "failed") and jid not in job_waited:
                job_waited.add(jid)
                kind[seq] = "fetch"
            else:
                kind[seq] = "skip"
                skipped.append({"seq": seq, "tool": tool, "reason": "job polling (replaced by s.wait)"})
        elif tool in analysis:
            kind[seq] = "call"          # the derivation itself, read-only or not (#433)
        elif tool in read_only and not include_read_only and not (checkpoints and _checks(e.get("result"))):
            kind[seq] = "prune"
        else:
            kind[seq] = "call"

    # 2. aborted transactions ----------------------------------------------------------
    if prune_aborted:
        stack, pid = [], None
        for e in entries:
            if e.get("worker_pid") not in (None, pid):
                stack, pid = [], e.get("worker_pid")
            if kind[e["seq"]] in ("skip",) or not e.get("ok"):
                continue
            if e["tool"] == "transaction_open":
                stack.append(e["seq"])
            elif e["tool"] == "transaction_commit" and stack:
                stack.pop()
            elif e["tool"] == "transaction_abort" and stack:
                start = stack.pop()
                for x in entries:
                    if start <= x["seq"] <= e["seq"] and kind[x["seq"]] not in ("skip",):
                        kind[x["seq"]] = "skip"
                        skipped.append({"seq": x["seq"], "tool": x["tool"],
                                        "reason": "inside an aborted transaction"})

    # 3. who binds what, and which pruned calls a kept call depends on ------------------
    binder, refs = {}, {}                       # value -> seq (as of now); seq -> {value: binder}
    for e in entries:
        seq = e["seq"]
        used = {}
        for _, _, v in _walk_strings(e.get("args") or {}):
            if v in binder:
                used[v] = binder[v]
        refs[seq] = used
        if kind[seq] in ("skip", "fail"):
            continue
        for v, _path in _bindings(e):
            binder[v] = seq
    live = {"call", "fetch"}
    for e in reversed(entries):
        seq = e["seq"]
        if kind[seq] in live or kind[seq] == "fail":
            for b in refs[seq].values():
                if kind.get(b) == "prune":
                    kind[b] = "call"
    for e in entries:
        if kind[e["seq"]] == "prune":
            skipped.append({"seq": e["seq"], "tool": e["tool"], "reason": "read-only"})

    # 4. paths -------------------------------------------------------------------------
    emitted = [e for e in entries if kind[e["seq"]] in live | {"fail"}]
    paths = _Paths(_collect_paths(emitted), workdir)
    produced = _produced_paths(entries)
    prerequisites, written = [], set()
    for e in emitted:
        if kind[e["seq"]] == "fail":
            continue
        for k, v in (e.get("args") or {}).items():
            if not (isinstance(v, str) and _is_path(v)):
                continue
            reads = e["tool"] == "open_document" or e["tool"] in read_only or k in _INPUT_PARAMS
            rel = "/".join(paths.rel(v))
            if not reads:
                written.add(v)
            elif v in written:
                pass                       # an earlier step in the script writes it
            elif produced.get(v, e["seq"]) < e["seq"]:
                # an earlier step's own output, handed on by path and written by nothing
                # the script does — a solver case dir from a previous solve, say. A
                # replay regenerates that somewhere else, so it is not a file to copy
                # in; it is a hand-off the script cannot make.
                warnings.append(
                    f"step {e['seq']}: {k} is a path step {produced[v]} produced, not an "
                    "input you supply; a replay regenerates it elsewhere (often a fresh "
                    "temp dir), so re-point this argument by hand before running")
            elif rel not in prerequisites:
                prerequisites.append(rel)

    # 5. emit --------------------------------------------------------------------------
    names: dict[str, str] = {}                  # linked value -> variable
    var_of_binder: dict[tuple, str] = {}        # (binder seq, value) -> variable
    need_result = {b for used in refs.values() for b in used.values()}
    submitted: dict[str, tuple] = {}            # job_id -> (seq, variable) not yet waited
    body: list[str] = []
    last_pid, n_exported = None, 0
    dirs_needed: set = set()
    geometry_tol, job_tol = 1e-6, 1e-3

    def expr(value):
        if isinstance(value, str):
            if value in names:
                return names[value]
            if _is_path(value):
                parts = paths.rel(value)
                if len(parts) > 1:
                    dirs_needed.add(parts[:-1])
                return "str(WORKDIR" + "".join(f" / {p!r}" for p in parts) + ")" if parts else "str(WORKDIR)"
            return repr(value)
        if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, list):
            return "[" + ", ".join(expr(v) for v in value) + "]"
        if isinstance(value, dict):
            return "{" + ", ".join(f"{k!r}: {expr(v)}" for k, v in value.items()) + "}"
        return repr(value)

    def flush_jobs():
        for jid, (_seq, var) in list(submitted.items()):
            body.append(f"s.wait({var})  # submitted but never fetched in the session")
            submitted.pop(jid)

    for e in emitted:
        seq, tool, args = e["seq"], e["tool"], e.get("args") or {}
        pid = e.get("worker_pid")
        if pid is not None and last_pid is not None and pid != last_pid and tool != "restart_worker":
            flush_jobs()
            body.append("")
            body.append("# The original worker was replaced here (idle reap, crash, or a closed and")
            body.append("# reused workspace); its documents and handles were gone.")
            body.append("s.restart_worker()")
            warnings.append(f"step {seq}: a fresh worker replaced the old one without a "
                            "restart_worker call; the script restarts it explicitly")
        if tool == "restart_worker":
            flush_jobs()
        if pid is not None:
            last_pid = pid

        body.append("")
        body.append(f"s.step = {seq}")
        if kind[seq] == "fail":
            call = _call_text(tool, args, expr)
            err = " ".join(str(e.get("error", "")).split())      # one line: it sits in a comment
            if len(err) > 240:
                err = err[:239] + "…"
            body.append(f"# FAILED in the session, not replayed: {err}")
            body.append("# " + " ".join(call.split()))
            warnings.append(f"step {seq}: {tool} failed in the session and is left as a comment")
            continue

        if tool == "run_script":
            warnings.append(f"step {seq}: run_script replays agent-written code, and needs "
                            "ANKUSDRIVE_ALLOW_RUN_SCRIPT on")
            body.append("# run_script: agent-written code. Read it before running this script.")
            if e.get("code_sha256"):
                body.append(f"# code {e['code_sha256']}")

        if kind[seq] == "fetch":
            jid = args.get("job_id")
            ref = names.get(jid, repr(jid))
            call = f"s.wait({ref})"
            submitted.pop(jid, None)
            tol = job_tol
        else:
            call = _call_text(tool, args, expr)
            tol = job_tol if (_SOLVER_READ.search(tool) or tool in analysis) else geometry_tol

        result = e.get("result")
        binds = [(v, p) for v, p in _bindings(e) if (seq, v) not in var_of_binder]
        used_binds = [(v, p) for v, p in binds if seq in need_result and any(
            refs[x].get(v) == seq for x in refs)]
        wide = kind[seq] == "fetch" or tool in analysis
        checks = _checks(result, wide=wide) if checkpoints else []
        expects = _expects(result) if (checkpoints and wide) else []
        job_binds = [v for v, p in binds if p and p[-1] == "job_id"]

        if used_binds or checks or expects or job_binds:
            rv = f"r{seq}"
            body.append(f"{rv} = {call}")
            for v, p in binds:
                if (v, p) not in used_binds and v not in job_binds:
                    continue
                var = _ident(v, next(k for k in reversed(p) if isinstance(k, str)))
                body.append(f"{var} = {rv}" + "".join(f"[{k!r}]" for k in p))
                names[v] = var
                var_of_binder[(seq, v)] = var
                if v in job_binds:
                    submitted[v] = (seq, var)
            for path, v in checks:
                keyexpr = repr(path[0]) if len(path) == 1 else repr(path)
                body.append(f"s.check({rv}, {keyexpr}, {v!r}" + (f", rel={tol})" if tol != geometry_tol else ")"))
            for path, v in expects:
                keyexpr = repr(path[0]) if len(path) == 1 else repr(path)
                body.append(f"s.expect({rv}, {keyexpr}, {v!r})")
        else:
            body.append(call)
        n_exported += 1
    flush_jobs()

    if any(e["tool"] == "open_document" for e in emitted):
        warnings.append("the session opened existing documents; copy them into WORKDIR "
                        "(see Prerequisites) before running")
    unmapped = [d for d in paths.dirs]
    if unmapped:
        warnings.append(f"{len(unmapped)} path(s) had no common folder; they map to "
                        "WORKDIR/dirN/ subfolders")

    # 6. assemble ----------------------------------------------------------------------
    gen = generator or _generator()
    head = [
        "#!/usr/bin/env python3",
        f'"""AnkusDrive session transcript: regenerates workspace {workspace!r}.',
        "",
        f"Exported by {gen} from {len(entries)} recorded calls "
        f"({n_exported} replayed, {len(skipped)} skipped).",
        "",
        "Run:  python <this file> [WORKDIR]",
        "Needs AnkusDrive and FreeCAD, plus any solver the recorded simulations used.",
        "Each s.check() fails the run at the first result that drifted from the recording.",
    ]
    if provenance:
        head += _provenance_lines(provenance)
    if prerequisites:
        head += ["", "Prerequisites (copy into WORKDIR at these relative paths):"]
        head += [f"  - {p}" for p in prerequisites]
    if warnings:
        head += ["", "Warnings:"]
        head += [f"  - {w}" for w in warnings]
    head += ['"""', "import sys", "from pathlib import Path", "",
             "from ankusdrive.replay import Session", "",
             'WORKDIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "regenerated"',
             "WORKDIR.mkdir(parents=True, exist_ok=True)"]
    for d in sorted(dirs_needed):
        head.append("(WORKDIR" + "".join(f" / {p!r}" for p in d) + ").mkdir(parents=True, exist_ok=True)")
    if provenance:
        head += ["",
                 "# The environment this session was recorded in. Machine-readable twin of",
                 "# the header above; s.provenance() compares it with this machine's.",
                 "PROVENANCE = " + pprint.pformat(provenance, width=94, sort_dicts=True)]
    head += ["", "with Session() as s:"]
    if not any(line.strip() for line in body):
        body = ["pass  # the journal has no calls to replay in this workspace"]
    if provenance:
        body.insert(0, "s.provenance(PROVENANCE)")
    script = "\n".join(head + [("    " + line) if line else "" for line in body]) + "\n"
    return {"script": script, "calls": len(entries), "exported": n_exported,
            "skipped": skipped, "warnings": warnings, "prerequisites": prerequisites}


def _generator() -> str:
    try:
        src = (_PKG / "__init__.py").read_text(encoding="utf-8")
        m = re.search(r'__version__\s*=\s*"([^"]+)"', src)
        return f"ankusdrive {m.group(1)}" if m else "ankusdrive"
    except OSError:
        return "ankusdrive"
