"""Free functional test of the M2 per-trial checkpoint: simulated kill ->
resume without re-billing -> auto-delete on completion -> contract-hash
invalidation. Stubs the builders; no API, no worker."""
import importlib.util
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.update(RUN_RELIABILITY="1", ANTHROPIC_API_KEY="dummy",
                  M2_TOYS="tchain3", M2_TRIALS="3", M2_COND="partition",
                  M2_MODEL="haiku")

spec = importlib.util.spec_from_file_location("m2", REPO / "tests/test_multiagent_m2.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

m.CKPT_PATH.unlink(missing_ok=True)
calls = {"n": 0}


def stub_ok(client, model, toy, tmp):
    calls["n"] += 1
    return {"condition": "partition", "built": True, "passed": True,
            "reason": "stub", "in_tokens": 1, "out_tokens": 1,
            "cache_read": 0, "cache_write": 0}


def stub_die(client, model, toy, tmp):
    if calls["n"] >= 2:
        raise SystemExit("simulated kill")  # BaseException: escapes the trial try/except
    return stub_ok(client, model, toy, tmp)


# A: kill after 2 of 3 trials -> 2 checkpoint lines survive
m.run_partition = stub_die
try:
    m.main()
    raise AssertionError("expected SystemExit")
except SystemExit:
    pass
assert len(m.CKPT_PATH.read_text().splitlines()) == 2
print("A ok: simulated kill leaves 2 checkpointed trials")

# B: resume -> exactly 1 fresh call, aggregates 3/3, checkpoint deleted
calls["n"] = 0
m.run_partition = stub_ok
m.main()
assert calls["n"] == 1, calls
assert not m.CKPT_PATH.exists()
row = json.load(open(m.CACHE_DIR / "report_m2.json"))["rows"][0]
assert row["trials"] == 3 and row["passed"] == 3
print("B ok: resumed 2 trials free, billed 1, report aggregates 3/3, checkpoint gone")

# C: leftover checkpoint + edited contract -> hash mismatch, all trials fresh
calls["n"] = 0
m.run_partition = stub_die
try:
    m.main()
except SystemExit:
    pass
assert len(m.CKPT_PATH.read_text().splitlines()) == 2
m.TOYS["tchain3"].single_task += " (CONTRACT EDITED)"
calls["n"] = 0
m.run_partition = stub_ok
m.main()
assert calls["n"] == 3, calls
print("C ok: edited contract invalidates stale checkpoint (3 fresh trials)")

m.CKPT_PATH.unlink(missing_ok=True)
print("ALL OK")
