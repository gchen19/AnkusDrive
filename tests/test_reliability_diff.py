"""
Layer B reliability test: render baseline + modified shape pairs, ask Claude
what changed, grade by keyword.

Diff detection is harder than classification — it requires the model to
localize a feature change, not just identify a primitive. The accuracy bar
is correspondingly lower (≥60%).

Gated behind RUN_RELIABILITY=1 (paid API). Cache layout mirrors Layer A but
adds `_diff_` suffix so the two harnesses don't collide.

Usage:
    ANTHROPIC_API_KEY=sk-... RUN_RELIABILITY=1 .venv/bin/python3 tests/test_reliability_diff.py
"""
import base64
import io
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402
from driftpin import render as render_lib  # noqa: E402
from tests.reliability_diffs import DIFFS, DiffSpec  # noqa: E402


CACHE_DIR = REPO / "tests" / "reliability_cache"
MODEL = "claude-sonnet-4-5"
PROMPT = (
    "Image A and Image B show the same 3D mechanical part with one "
    "modification between them. In one or two sentences, describe what "
    "changed from A to B. Be specific (size, missing feature, repositioned "
    "feature, etc.). If you can't tell the difference, say so."
)


def render_one(w, builder, view: str = "iso", deflection: float = 0.3) -> bytes:
    handle = builder(w)
    mesh = w.call("tessellate", handle=handle, deflection=deflection)
    return render_lib.render_mesh(
        mesh["vertices"], mesh["triangles"],
        width=384, height=384, view=view,
    )


def make_pair_image(png_a: bytes, png_b: bytes) -> bytes:
    """Combine two PNGs side-by-side with an A/B label band."""
    a = Image.open(io.BytesIO(png_a)).convert("RGB")
    b = Image.open(io.BytesIO(png_b)).convert("RGB")
    h = max(a.height, b.height)
    label_h = 24
    out = Image.new("RGB", (a.width + b.width + 8, h + label_h), (255, 255, 255))
    out.paste(a, (0, label_h))
    out.paste(b, (a.width + 8, label_h))

    from PIL import ImageDraw
    draw = ImageDraw.Draw(out)
    draw.text((a.width // 2 - 6, 4), "A", fill=(0, 0, 0))
    draw.text((a.width + 8 + b.width // 2 - 6, 4), "B", fill=(0, 0, 0))

    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def call_claude(image_bytes: bytes) -> str:
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


def grade(spec: DiffSpec, text: str) -> dict:
    lower = text.lower()
    hits = [kw for kw in spec.must_match_any if kw in lower]
    return {
        "pass": bool(hits),
        "hits": hits,
        "expected_any_of": spec.must_match_any[:6],  # truncate for the report
    }


def run_suite() -> dict:
    CACHE_DIR.mkdir(exist_ok=True, parents=True)
    results = []

    with Worker() as w:
        for builder_a, builder_b, spec in DIFFS:
            img_path = CACHE_DIR / f"{spec.name}_diff.png"
            txt_path = CACHE_DIR / f"{spec.name}_diff.txt"

            if img_path.exists():
                pair = img_path.read_bytes()
            else:
                png_a = render_one(w, builder_a)
                png_b = render_one(w, builder_b)
                pair = make_pair_image(png_a, png_b)
                img_path.write_bytes(pair)

            if txt_path.exists():
                response = txt_path.read_text(encoding="utf-8")
                cached = True
            else:
                t0 = time.time()
                response = call_claude(pair)
                txt_path.write_text(response, encoding="utf-8")
                cached = False
                print(f"  [api] {spec.name} ({time.time() - t0:.1f}s)")

            g = grade(spec, response)
            results.append({
                "diff": spec.name,
                "description": spec.description,
                "response": response,
                "grade": g,
                "cached": cached,
            })

    correct = sum(1 for r in results if r["grade"]["pass"])
    total = len(results)
    accuracy = correct / total if total else 0.0
    report = {
        "model": MODEL, "total": total, "correct": correct,
        "accuracy": accuracy, "results": results,
    }
    (CACHE_DIR / "report_diff.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def print_report(report):
    print()
    print("=" * 72)
    print(f"Reliability Layer B (diff detection): {report['model']}")
    print(f"  {report['correct']}/{report['total']} = {report['accuracy']:.0%}")
    print("=" * 72)
    for r in report["results"]:
        mark = "PASS" if r["grade"]["pass"] else "FAIL"
        cache = " (cached)" if r["cached"] else ""
        print(f"\n  [{mark}] {r['diff']}{cache}")
        print(f"        spec:  {r['description']}")
        truncated = r["response"].replace("\n", " ").strip()
        if len(truncated) > 220:
            truncated = truncated[:220] + "..."
        print(f"        said:  {truncated}")
        if not r["grade"]["pass"]:
            print(f"        wanted any of: {r['grade']['expected_any_of']}")


def main():
    if not os.environ.get("RUN_RELIABILITY"):
        print(
            "Layer B reliability suite is gated behind RUN_RELIABILITY=1 (it\n"
            "calls the Anthropic API and costs credits). See tests/RELIABILITY.md."
        )
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    report = run_suite()
    print_report(report)

    bar = 0.60
    if report["accuracy"] < bar:
        print(f"\nFAIL: accuracy {report['accuracy']:.0%} below bar {bar:.0%}")
        sys.exit(1)
    print(f"\nOK: accuracy {report['accuracy']:.0%} ≥ bar {bar:.0%}")


if __name__ == "__main__":
    main()
