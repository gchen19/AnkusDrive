"""Motion oracle on the real gearbox — the positive/negative control.

ONE set of gear/shaft/plate geometry, merged TWICE with two different `mechanism`
declarations, to show the §11.9 motion gate separates a moving assembly from a dead
one that every static gate (interference / gear_mesh / bore_fit / mass) still passes:

  RIGID     every gear keyed to its shaft -> over-constrained, mobility DOF < 1,
            LOCKED. This is exactly what scratch/gearbox_experiment.py built.
  SELECTIVE output gears freewheel, one dog-clutched per speed -> each speed is a
            determinate 1-DOF train whose realised ratio hits the design ratio.

  .venv/bin/python3 scratch/gearbox_motion.py [n_speeds]   (default 3)
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import gearbox_real as gb            # noqa: E402
from ankusdrive import Worker          # noqa: E402


def _run(w, tmp, n, files, mode, name):
    mp = gb.build_manifest(tmp, n, files, mechanism=mode, manifest_name=name)
    rep = w.call("merge_assembly", manifest=str(mp))
    return rep


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        print(f"== motion oracle on the {n}-speed gearbox (one geometry, two declarations) ==")
        files = gb.build_components(tmp, n)
        with Worker() as w:
            rigid = _run(w, tmp, n, files, "rigid", "manifest_rigid.json")
            sel = _run(w, tmp, n, files, "selective", "manifest_selective.json")

        for label, rep in (("RIGID", rigid), ("SELECTIVE", sel)):
            g = rep["gates"]
            mob = rep.get("mobility", {})
            statics_ok = (not g["interference"]) and (not g.get("typed")) \
                and (not g.get("requirements"))
            print(f"\n--- {label} ---")
            print(f"  static gates (interference/gear_mesh/bore_fit/mass): "
                  f"{'PASS' if statics_ok else 'fail'}")
            print(f"  motion gate ok={not g.get('mobility')}  verdict={mob.get('verdict')}")
            if mob.get("rigid"):
                rg = mob["rigid"]
                print(f"  rigid mobility_dof={rg['mobility_dof']:+d}  "
                      f"consistent={rg['consistent']}")
                for c in rg.get("conflicts", [])[:1]:
                    print(f"    conflict: {c['reason']}")
            for sname, st in (mob.get("states") or {}).items():
                rr = st.get("ratio_realised")
                dr = st.get("ratio_design")
                print(f"    {sname}: power_path_dof={st['power_path_dof']:+d}  "
                      f"realised={rr}  design={round(dr,4) if dr else dr}  "
                      f"ok={st.get('ok')}")
            for v in g.get("mobility", [])[:3]:
                print(f"  VIOLATION: {v.get('reason')}")

        print("\n  Same parts, same static gates. The motion gate is the only thing "
              "that\n  tells the dead lock from the working transmission.")


if __name__ == "__main__":
    main()
