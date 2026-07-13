"""
Layer-A reliability test: render N known shapes and ask Claude what they are.
Grade by keyword match. The point is to detect "renderer is correct on
pixel-invariants but the agent still can't see the part" — a class of bug
that the silhouette tests cannot catch.

Gated behind RUN_RELIABILITY=1 because it costs API credits. Renderings AND
model responses are cached on disk so re-runs of the grader (e.g. after
tweaking synonyms) don't re-bill.

Usage:
    export ANTHROPIC_API_KEY=...
    RUN_RELIABILITY=1 .venv/bin/python3 tests/test_reliability.py

Cache layout:
    tests/reliability_cache/
        cube_iso.png            # rendered image
        cube_iso.txt            # model response (plain text)
        ...
        report.json             # structured per-shape grades

Tweaking the grader and re-running is free as long as the .txt cache exists.
Delete a .txt file to force a re-call for that shape.
"""
import base64
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402
from driftpin import render as render_lib  # noqa: E402
from tests.reliability_shapes import SHAPES, ShapeSpec  # noqa: E402


CACHE_DIR = REPO / "tests" / "reliability_cache"
MODEL = "claude-sonnet-4-5"  # cheaper than opus, plenty for shape ID
PROMPT = (
    "You are looking at a rendered 3D mechanical part from an isometric view. "
    "In one or two sentences, describe what shape and features you see. Be "
    "specific about geometric features like holes, fillets, pockets, or "
    "stepped/L-shaped forms if present. If unsure, say so."
)


def render_shape(w, builder, view: str = "iso", deflection: float = 0.3) -> bytes:
    handle = builder(w)
    mesh = w.call("tessellate", handle=handle, deflection=deflection)
    return render_lib.render_mesh(
        mesh["vertices"], mesh["triangles"],
        width=512, height=512, view=view,
    )


def call_claude(image_bytes: bytes) -> str:
    """One shot: image + prompt → model text. No streaming, no retries."""
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
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
    )
    return resp.content[0].text


def grade_response(spec: ShapeSpec, text: str) -> dict:
    """Keyword-match grader. Returns {pass, reasons[], hits, anti_hits}."""
    lower = text.lower()
    hits = [kw for kw in spec.must_match_any if kw in lower]
    anti = [kw for kw in spec.must_not_match if kw in lower]
    all_groups = []
    for group in spec.must_match_all:
        group_hits = [kw for kw in group if kw in lower]
        all_groups.append({"required_any_of": group, "hits": group_hits})

    reasons = []
    if not hits:
        reasons.append(f"missing primary keyword (any of {spec.must_match_any})")
    for g in all_groups:
        if not g["hits"]:
            reasons.append(f"missing required group keyword (any of {g['required_any_of']})")
    if anti:
        reasons.append(f"contains anti-keyword: {anti}")

    passed = bool(hits) and not anti and all(g["hits"] for g in all_groups)
    return {
        "pass": passed,
        "reasons": reasons,
        "hits": hits,
        "anti_hits": anti,
        "groups": all_groups,
    }


def run_reliability_suite() -> dict:
    CACHE_DIR.mkdir(exist_ok=True, parents=True)
    results = []

    with Worker() as w:
        for builder, spec in SHAPES:
            img_path = CACHE_DIR / f"{spec.name}_iso.png"
            txt_path = CACHE_DIR / f"{spec.name}_iso.txt"

            if img_path.exists():
                png = img_path.read_bytes()
            else:
                png = render_shape(w, builder)
                img_path.write_bytes(png)

            if txt_path.exists():
                response = txt_path.read_text(encoding="utf-8")
                cached = True
            else:
                t0 = time.time()
                response = call_claude(png)
                txt_path.write_text(response, encoding="utf-8")
                cached = False
                print(f"  [api] {spec.name} ({time.time() - t0:.1f}s)")

            grade = grade_response(spec, response)
            results.append({
                "shape": spec.name,
                "description": spec.description,
                "response": response,
                "grade": grade,
                "cached": cached,
            })

    correct = sum(1 for r in results if r["grade"]["pass"])
    total = len(results)
    accuracy = correct / total if total else 0.0
    report = {
        "model": MODEL,
        "total": total,
        "correct": correct,
        "accuracy": accuracy,
        "results": results,
    }
    (CACHE_DIR / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def print_report(report: dict):
    print()
    print("=" * 70)
    print(f"Reliability suite: {report['model']}")
    print(f"  {report['correct']}/{report['total']} correct = {report['accuracy']:.0%}")
    print("=" * 70)
    for r in report["results"]:
        mark = "PASS" if r["grade"]["pass"] else "FAIL"
        cache = " (cached)" if r["cached"] else ""
        print(f"\n  [{mark}] {r['shape']}{cache}")
        print(f"        spec: {r['description']}")
        # First 200 chars of response.
        truncated = r["response"].replace("\n", " ").strip()
        if len(truncated) > 220:
            truncated = truncated[:220] + "..."
        print(f"        said: {truncated}")
        if not r["grade"]["pass"]:
            for reason in r["grade"]["reasons"]:
                print(f"        why:  {reason}")


def main():
    if not os.environ.get("RUN_RELIABILITY"):
        print(
            "Reliability suite is gated behind RUN_RELIABILITY=1 (it calls the\n"
            "Anthropic API and costs credits). Set RUN_RELIABILITY=1 to run.\n"
            "Cache at tests/reliability_cache/ — delete entries to force re-call."
        )
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    report = run_reliability_suite()
    print_report(report)

    # Hard fail if accuracy drops below a sensible bar.
    bar = 0.70
    if report["accuracy"] < bar:
        print(f"\nFAIL: accuracy {report['accuracy']:.0%} below bar {bar:.0%}")
        sys.exit(1)
    print(f"\nOK: accuracy {report['accuracy']:.0%} ≥ bar {bar:.0%}")


if __name__ == "__main__":
    main()
