"""
Layer C reliability — agent-loop closure.

For each design spec, build TWO renders: one geometry that matches the spec,
one that violates it. For each:
  1. Show the model the render + the written spec.
  2. Ask: "does this geometry match the spec?"
  3. Compare the model's verdict to the geometric ground-truth check.

The agreement metric is: fraction of (spec, render) pairs where the model's
verdict matches reality. False positives (model says "matches" when it
doesn't) and false negatives (model says "doesn't match" when it does) both
count as misses.

This is the test that determines whether agents can self-correct during
design iteration. If the model can't tell when its own output diverges from
the spec, it can't fix mistakes.

NOTE: We're NOT actually invoking a tool-using agent here — Layer C of this
flavor tests *visual judgment under spec*, not *tool-use correctness*. A
fuller "agent designs from scratch via MCP, we verify the output" test
belongs in a separate Layer D, future work.

Gated behind RUN_RELIABILITY=1. Cache mirrors Layers A/B.

Usage:
    ANTHROPIC_API_KEY=sk-... RUN_RELIABILITY=1 .venv/bin/python3 tests/test_reliability_agent_loop.py
"""
import base64
import io
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402
from driftpin import render as render_lib  # noqa: E402
from tests.reliability_specs import SPECS, Spec  # noqa: E402


CACHE_DIR = REPO / "tests" / "reliability_cache"
MODEL = "claude-sonnet-4-5"

PROMPT_TEMPLATE = (
    "DESIGN SPEC: {spec}\n\n"
    "The image shows a 3D rendered part. Look at the part and decide whether "
    "it matches the design spec above.\n\n"
    "Reply with EXACTLY one of these two words on the first line:\n"
    "  MATCHES\n"
    "  VIOLATES\n\n"
    "Then on a second line, give a one-sentence explanation."
)


def render_handle(w, handle, view: str = "iso", deflection: float = 0.3) -> bytes:
    mesh = w.call("tessellate", handle=handle, deflection=deflection)
    return render_lib.render_mesh(
        mesh["vertices"], mesh["triangles"],
        width=512, height=512, view=view,
    )


def call_claude(image_bytes: bytes, prompt: str) -> str:
    import anthropic
    client = anthropic.Anthropic()
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=200,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )
    return resp.content[0].text


def parse_verdict(text: str) -> bool | None:
    """Returns True if MATCHES, False if VIOLATES, None if neither."""
    first_line = text.strip().split("\n", 1)[0].strip().upper()
    if "MATCHES" in first_line and "VIOLATES" not in first_line:
        return True
    if "VIOLATES" in first_line and "MATCHES" not in first_line:
        return False
    # Fall back: anywhere in the response.
    upper = text.upper()
    if "MATCHES" in upper and "VIOLATES" not in upper:
        return True
    if "VIOLATES" in upper and "MATCHES" not in upper:
        return False
    return None


def run_suite() -> dict:
    CACHE_DIR.mkdir(exist_ok=True, parents=True)
    rows = []

    with Worker() as w:
        for spec in SPECS:
            for variant_name, builder, expected_truth, violation in [
                ("correct", spec.correct_builder, True, "n/a"),
                ("wrong", spec.wrong_builder, False, spec.wrong_violation),
            ]:
                key = f"{spec.name}__{variant_name}"
                img_path = CACHE_DIR / f"{key}_loop.png"
                txt_path = CACHE_DIR / f"{key}_loop.txt"

                if img_path.exists():
                    png = img_path.read_bytes()
                    handle = builder(w)  # Need handle for ground-truth check.
                else:
                    handle = builder(w)
                    png = render_handle(w, handle)
                    img_path.write_bytes(png)

                # Ground truth from geometry.
                truth = spec.checker(w, handle)
                ground_truth_matches = truth["matches"]
                # Sanity: our builders should produce what we expect.
                assert ground_truth_matches == expected_truth, (
                    f"BUILDER BUG: {key} ground-truth says "
                    f"matches={ground_truth_matches} but we declared "
                    f"expected_truth={expected_truth}. "
                    f"Reason: {truth['reason']}"
                )

                # Model verdict.
                if txt_path.exists():
                    response = txt_path.read_text(encoding="utf-8")
                    cached = True
                else:
                    prompt = PROMPT_TEMPLATE.format(spec=spec.description)
                    t0 = time.time()
                    response = call_claude(png, prompt)
                    txt_path.write_text(response, encoding="utf-8")
                    cached = False
                    print(f"  [api] {key} ({time.time() - t0:.1f}s)")

                model_verdict = parse_verdict(response)
                if model_verdict is None:
                    agrees = False
                    error = "could not parse MATCHES/VIOLATES"
                else:
                    agrees = (model_verdict == ground_truth_matches)
                    error = None

                rows.append({
                    "key": key,
                    "spec": spec.name,
                    "variant": variant_name,
                    "ground_truth_matches": ground_truth_matches,
                    "model_says_matches": model_verdict,
                    "agrees": agrees,
                    "violation": violation,
                    "checker_reason": truth["reason"],
                    "model_response": response,
                    "parse_error": error,
                    "cached": cached,
                })

    correct = sum(1 for r in rows if r["agrees"])
    total = len(rows)
    accuracy = correct / total if total else 0.0
    false_pos = sum(
        1 for r in rows
        if not r["ground_truth_matches"] and r["model_says_matches"] is True
    )
    false_neg = sum(
        1 for r in rows
        if r["ground_truth_matches"] and r["model_says_matches"] is False
    )
    report = {
        "model": MODEL, "total": total, "correct": correct,
        "accuracy": accuracy,
        "false_positives": false_pos,
        "false_negatives": false_neg,
        "rows": rows,
    }
    (CACHE_DIR / "report_agent_loop.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def print_report(report):
    print()
    print("=" * 76)
    print(f"Reliability Layer C (agent-loop closure): {report['model']}")
    print(
        f"  agreement: {report['correct']}/{report['total']} = "
        f"{report['accuracy']:.0%}    "
        f"FP: {report['false_positives']}, FN: {report['false_negatives']}"
    )
    print("  (FP = model says MATCHES on a wrong build → agent can't self-correct)")
    print("  (FN = model says VIOLATES on a correct build → agent over-rejects)")
    print("=" * 76)
    for r in report["rows"]:
        mark = "OK  " if r["agrees"] else "MISS"
        cache = " (cached)" if r["cached"] else ""
        truth = "MATCHES" if r["ground_truth_matches"] else "VIOLATES"
        said = (
            "MATCHES" if r["model_says_matches"] is True else
            "VIOLATES" if r["model_says_matches"] is False else
            f"UNPARSED ({r['parse_error']})"
        )
        print(f"\n  [{mark}] {r['key']}{cache}")
        print(f"        truth: {truth}    model: {said}")
        print(f"        checker: {r['checker_reason']}")
        if r["variant"] == "wrong":
            print(f"        violation: {r['violation']}")
        truncated = r["model_response"].replace("\n", " | ").strip()
        if len(truncated) > 200:
            truncated = truncated[:200] + "..."
        print(f"        said: {truncated}")


def main():
    if not os.environ.get("RUN_RELIABILITY"):
        print(
            "Layer C agent-loop suite is gated behind RUN_RELIABILITY=1 (it\n"
            "calls the Anthropic API and costs credits). See tests/RELIABILITY.md."
        )
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    report = run_suite()
    print_report(report)

    bar = 0.80
    if report["accuracy"] < bar:
        print(f"\nFAIL: agreement {report['accuracy']:.0%} below bar {bar:.0%}")
        sys.exit(1)
    print(f"\nOK: agreement {report['accuracy']:.0%} ≥ bar {bar:.0%}")


if __name__ == "__main__":
    main()
