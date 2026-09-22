"""
AnkusDrive command-line interface. Each subcommand spawns a worker, issues a few
calls, prints a one-line summary, and shuts down cleanly.

Run as:  python3 -m ankusdrive <command> [args]
"""
import argparse
import json
import sys
from pathlib import Path

from .client import Worker, WorkerError, WorkerDied


def _print_kv(**kv):
    parts = [f"{k}={v}" for k, v in kv.items()]
    print(" ".join(parts))


def cmd_ping(args):
    with Worker() as w:
        reply = w.call("ping")
    _print_kv(ping=reply, freecad=".".join(w.freecad_version))


def cmd_version(args):
    with Worker() as w:
        v = w.call("version")
    print(f"freecad={'.'.join(v['freecad'][:3])} python={v['python']}")


def cmd_box(args):
    with Worker() as w:
        w.call("new_document", name=Path(args.out).stem or "part")
        r = w.call("add_primitive", kind="box", w=args.w, d=args.d, h=args.h)
        s = w.call("save_document", path=args.out)
    _print_kv(object=r["name"], volume=r["volume"], path=s["path"], bytes=s["size"])


def cmd_cylinder(args):
    with Worker() as w:
        w.call("new_document", name=Path(args.out).stem or "part")
        r = w.call("add_primitive", kind="cylinder", r=args.r, h=args.h)
        s = w.call("save_document", path=args.out)
    _print_kv(object=r["name"], volume=r["volume"], path=s["path"], bytes=s["size"])


def cmd_export(args):
    with Worker() as w:
        o = w.call("open_document", path=args.input)
        r = w.call("export_shape", path=args.out, object=args.object)
    _print_kv(
        doc=o["doc"],
        object=r["object"],
        path=r["path"],
        bytes=r["size"],
    )


def cmd_run(args):
    code = Path(args.script).read_text(encoding="utf-8")
    with Worker() as w:
        r = w.call("run_script", _timeout=args.timeout, code=code, path=args.script)
    if r.get("result") is not None:
        print(json.dumps(r["result"], indent=2))


def cmd_mcp(args):
    """Lazy-import to keep `mcp` an optional dependency."""
    try:
        from .mcp_server import run
    except ImportError as e:
        print(
            f"error: mcp package not installed in this interpreter ({e}).\n"
            f"  install with:  {sys.executable} -m pip install mcp\n"
            f"  or use the project venv:  .venv/bin/python3 -m ankusdrive mcp",
            file=sys.stderr,
        )
        sys.exit(4)
    run()


def cmd_setup(args):
    """Interactive (or --yes scripted) provisioner: FreeCAD path -> config.toml,
    opt-in pip-wheel extras, MCP registration snippet (issues #200/#201)."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    from . import setup_cmd
    if args.print_mcp_config:
        print(setup_cmd.mcp_config_text())
        return
    sys.exit(setup_cmd.run_setup(
        assume_yes=args.yes,
        extras=args.extras.split(",") if args.extras is not None else None,
        freecadcmd=args.freecadcmd,
    ))


def cmd_doctor(args):
    """Resolve FreeCAD + every solver family and print a per-item health checklist.
    Exits non-zero when FreeCAD is unresolved or the MCP server cannot start, so it
    doubles as a CI/setup preflight."""
    from . import doctor
    # Install hints carry non-ASCII (em-dashes, arrows); force UTF-8 so they render on
    # a Windows console (cp1252 default) instead of mojibake. Best-effort — older
    # streams without reconfigure() just keep their default encoding.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    report = doctor.build_report(
        probe_version=not args.no_boot,
        # The deep MCP probe spawns a server process; --no-boot means "resolve only,
        # start nothing", so it silences this too (issue #278).
        mcp_serve=not (args.no_boot or args.no_mcp_serve),
        # Opt-in: the only doctor step that reaches the network (#423).
        verify_image=args.verify_image and not args.no_boot,
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(doctor.render(report))
    mcp = report["mcp"]
    # A dead MCP server is a failed preflight, not a footnote: `ankusdrive doctor`
    # exiting 0 while `ankusdrive mcp` cannot import is the exact field defect that
    # issue #278 exists to close.
    unusable = (
        not report["freecad"]["available"]
        or not mcp["import"]["available"]
        or (mcp["serve"]["checked"] and not mcp["serve"]["ok"])
    )
    if unusable:
        sys.exit(1)


def cmd_fem_cantilever(args):
    params = {
        "length": args.length,
        "width": args.width,
        "height": args.height,
        "force": args.force,
        "mesh_size": args.mesh_size,
    }
    if args.workdir:
        params["workdir"] = args.workdir
    with Worker() as w:
        r = w.call("fem_cantilever_demo", _timeout=args.timeout, **params)
    _print_kv(
        nodes=r["nodes"],
        tets=r["tets"],
        max_disp_mm=f"{r['max_displacement_mm']:.4f}",
        max_vM_MPa=f"{r['max_vonmises_mpa']:.2f}",
        workdir=r["workdir"],
    )


def cmd_journal_list(args):
    """The durable journal's sessions on disk (#433) — no worker needed."""
    from . import journal_store
    where = args.dir or journal_store.journal_dir()
    if not where:
        print("the durable journal is off: set ANKUSDRIVE_JOURNAL_DIR (or journal_dir "
              "in config.toml), or pass --dir", file=sys.stderr)
        sys.exit(1)
    found = journal_store.sessions(where)
    if args.json:
        print(json.dumps({"dir": where, "sessions": found}, indent=2))
        return
    for s in found:
        print(f"{s['session']}  {s['modified']}  {s['bytes']:>10} B  {s['file']}")
    if not found:
        print(f"no journal sessions in {where}", file=sys.stderr)


def cmd_journal_export(args):
    """A journal file -> the session_transcript record (#433). ``target`` is a session
    id, ``latest``, or a path to a ``session-*.jsonl`` file (anywhere: this is the
    user's own shell, not an MCP tool)."""
    from . import journal_store
    target = args.target
    is_file = target.endswith(".jsonl") or Path(target).is_file()
    try:
        out = journal_store.export(
            target, path=str(Path(target).expanduser()) if is_file else None,
            where=args.dir, workspace=args.workspace,
            include_read_only=args.include_read_only, checkpoints=not args.no_checkpoints,
            prune_aborted=args.prune_aborted, provenance=not args.no_provenance)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    text = json.dumps(out, indent=2, default=str) + "\n" if args.json else out["script"]
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)
    for w in out.get("warnings") or []:
        print(f"warning: {w}", file=sys.stderr)


def build_parser():
    from . import __version__
    p = argparse.ArgumentParser(prog="ankusdrive", description="FreeCAD CLI over a live worker.")
    p.add_argument(
        "--version", action="version", version=f"ankusdrive {__version__}",
        help="Print AnkusDrive's version and exit.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="Boot a worker, ping it, tear it down.").set_defaults(func=cmd_ping)
    sub.add_parser(
        "version",
        help="Print FreeCAD + bundled Python versions of the worker. "
             "For AnkusDrive's own version use --version.",
    ).set_defaults(func=cmd_version)

    pb = sub.add_parser("box", help="Create a box and save as .FCStd.")
    pb.add_argument("--w", type=float, required=True, help="length (mm)")
    pb.add_argument("--d", type=float, required=True, help="width (mm)")
    pb.add_argument("--h", type=float, required=True, help="height (mm)")
    pb.add_argument("-o", "--out", required=True, help="output .FCStd path")
    pb.set_defaults(func=cmd_box)

    pc = sub.add_parser("cylinder", help="Create a cylinder and save as .FCStd.")
    pc.add_argument("--r", type=float, required=True, help="radius (mm)")
    pc.add_argument("--h", type=float, required=True, help="height (mm)")
    pc.add_argument("-o", "--out", required=True, help="output .FCStd path")
    pc.set_defaults(func=cmd_cylinder)

    pe = sub.add_parser("export", help="Export a shape from a .FCStd to STEP/IGES/BREP/STL.")
    pe.add_argument("input", help="input .FCStd path")
    pe.add_argument("-o", "--out", required=True, help="output path (format inferred from extension)")
    pe.add_argument("--object", default=None, help="object name (defaults to the single final shape; errors if ambiguous)")
    pe.set_defaults(func=cmd_export)

    pr = sub.add_parser("run", help="Run a Python script in a live worker. Set __result__ to return a value.")
    pr.add_argument("script", help="path to .py file")
    pr.add_argument("--timeout", type=float, default=300.0)
    pr.set_defaults(func=cmd_run)

    pm = sub.add_parser("mcp", help="Start the MCP server over stdio.")
    pm.set_defaults(func=cmd_mcp)

    pd = sub.add_parser(
        "doctor",
        help="Resolve FreeCAD, the MCP server, and every solver family, and print "
             "a setup/health checklist with the exact fix per item. Exits non-zero "
             "if FreeCAD is unresolved or the MCP server cannot start (usable as a "
             "CI/setup preflight).",
    )
    pd.add_argument("--json", action="store_true", help="emit the report as JSON")
    pd.add_argument(
        "--no-boot", action="store_true",
        help="start nothing: skip booting FreeCAD to read its version and skip the "
             "MCP serve round-trip (resolve/import only; the solver half never "
             "needs a boot)",
    )
    pd.add_argument(
        "--verify-image", action="store_true",
        help="check that the solver container's image was signed by this repository "
             "(needs network and `gh`; an image you built yourself is reported as "
             "unsigned, which ANKUSDRIVE_ALLOW_UNVERIFIED_IMAGE=1 silences)",
    )
    pd.add_argument(
        "--no-mcp-serve", action="store_true",
        help="skip only the MCP serve round-trip (do not spawn `ankusdrive mcp` over "
             "stdio); the mcp import check still runs",
    )
    pd.set_defaults(func=cmd_doctor)

    ps = sub.add_parser(
        "setup",
        help="Interactive provisioner: confirm/persist the FreeCAD path to the "
             "config file, opt into pip-wheel solver extras, and print the MCP "
             "registration snippet. --yes for scripted use.",
    )
    ps.add_argument("--yes", action="store_true",
                    help="no prompts: accept the resolved FreeCAD path, install "
                         "only what --extras names")
    ps.add_argument("--extras", default=None, metavar="A,B",
                    help="comma-separated pip-wheel extras to install "
                         "(mbd,topology,optics,fluids)")
    ps.add_argument("--freecadcmd", default=None, metavar="PATH",
                    help="persist this freecadcmd path instead of auto-resolving")
    ps.add_argument("--print-mcp-config", action="store_true",
                    help="print only the MCP host registration block "
                         "(mcpServers JSON + claude mcp add one-liner) and exit")
    ps.set_defaults(func=cmd_setup)

    pj = sub.add_parser(
        "journal",
        help="The durable analysis journal (#433): list past sessions, or export one "
             "as the same replay script + provenance record session_transcript gives.",
    )
    jsub = pj.add_subparsers(dest="journal_command", required=True)
    pjl = jsub.add_parser("list", help="List the session files in the journal directory.")
    pjl.add_argument("--dir", default=None,
                     help="journal directory (default: ANKUSDRIVE_JOURNAL_DIR / journal_dir)")
    pjl.add_argument("--json", action="store_true", help="emit JSON")
    pjl.set_defaults(func=cmd_journal_list)
    pje = jsub.add_parser("export", help="Export one session as a replay script.")
    pje.add_argument("target", help="session id, 'latest', or a path to a session-*.jsonl")
    pje.add_argument("-o", "--out", default=None, help="write here instead of stdout")
    pje.add_argument("--json", action="store_true",
                     help="the whole record (script, env, ledger, digests, provenance)")
    pje.add_argument("--dir", default=None,
                     help="journal directory (default: ANKUSDRIVE_JOURNAL_DIR / journal_dir)")
    pje.add_argument("--workspace", default=None,
                     help="workspace to export (default: current when the session ended)")
    pje.add_argument("--include-read-only", action="store_true")
    pje.add_argument("--no-checkpoints", action="store_true")
    pje.add_argument("--prune-aborted", action="store_true")
    pje.add_argument("--no-provenance", action="store_true",
                     help="skip the environment record and its solver version probes")
    pje.set_defaults(func=cmd_journal_export)

    pf = sub.add_parser("fem", help="FEM subcommands.")
    fsub = pf.add_subparsers(dest="fem_command", required=True)
    pfc = fsub.add_parser("cantilever", help="Run the built-in cantilever demo.")
    pfc.add_argument("--length", type=float, default=8000.0)
    pfc.add_argument("--width", type=float, default=1000.0)
    pfc.add_argument("--height", type=float, default=1000.0)
    pfc.add_argument("--force", type=float, default=9_000_000.0)
    pfc.add_argument("--mesh-size", type=float, default=500.0)
    pfc.add_argument("--workdir", default=None)
    pfc.add_argument("--timeout", type=float, default=300.0)
    pfc.set_defaults(func=cmd_fem_cantilever)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except WorkerError as e:
        print(f"error: {e}", file=sys.stderr)
        if e.remote_traceback:
            print(e.remote_traceback, file=sys.stderr)
        sys.exit(2)
    except WorkerDied as e:
        print(f"worker died: {e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
