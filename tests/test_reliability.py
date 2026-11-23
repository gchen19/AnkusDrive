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

Grader (RELIABILITY_GRADER, see tests/reliability_judge.py):
    keyword  (default) keyword lists from reliability_shapes.py, no extra calls
    judge    an LLM judge (RELIABILITY_JUDGE_MODEL, default opus) drives the gate
    both     both run, the gate stays on keyword, disagreements are listed

Cache layout:
    tests/reliability_cache/
        cube_iso.png                                  # rendered image
        cube_iso.<model>.txt                          # model response (plain text)
        cube_iso.<model>.judge.<judge model>.json     # judge verdict (judge/both)
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

# ankusdrive (Worker, render) is imported inside the functions that need it, so the
# offline judge test can import the graders here on the FreeCAD-free CI lane.
from orchestration.claude_models import first_text, resolve_model, response_cache_name  # noqa: E402
from tests import reliability_judge as judge_lib  # noqa: E402
from tests.reliability_shapes import SHAPES, ShapeSpec  # noqa: E402


CACHE_DIR = REPO / "tests" / "reliability_cache"
MODEL = resolve_model(default="sonnet")  # ANKUSDRIVE_EVAL_MODEL overrides (alias or full ID)
PROMPT = (
    "You are looking at a rendered 3D mechanical part from an isometric view. "
    "In one or two sentences, describe what shape and features you see. Be "
    "specific about geometric features like holes, fillets, pockets, or "
    "stepped/L-shaped forms if present. If unsure, say so."
)


def render_shape(w, builder, view: str = "iso", deflection: float = 0.3) -> bytes:
    from ankusdrive import render as render_lib
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
        max_tokens=4096,  # headroom: current models think before answering
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
    return first_text(resp)


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


def _anthropic_client():
    import anthropic
    return anthropic.Anthropic()


def grade_all(items: list[dict], mode: str, *, answered_model: str, judge: str | None = None,
              get_client=_anthropic_client, cache_dir: Path = CACHE_DIR) -> dict:
    """Grade collected answers and build the report. No rendering, no Worker.

    ``items`` are ``{"spec", "response", "cached"}`` dicts. In ``judge`` / ``both``
    mode each answer also gets a judge verdict (cached on disk); ``get_client`` is
    only called on a verdict cache miss.
    """
    results = []
    for it in items:
        spec, response = it["spec"], it["response"]
        grade = grade_response(spec, response)
        verdict = None
        if mode in ("judge", "both"):
            verdict = judge_lib.cached_judge(
                get_client, spec, response, PROMPT, cache_dir=cache_dir,
                stem=f"{spec.name}_iso", answered_model=answered_model, judge=judge)
        results.append({
            "shape": spec.name,
            "description": spec.description,
            "response": response,
            "grade": grade,
            "judge": verdict,
            "pass": judge_lib.gate_pass(grade, verdict, mode),
            "cached": it.get("cached", False),
        })

    correct = sum(1 for r in results if r["pass"])
    total = len(results)
    report = {
        "model": answered_model,
        "grader": mode,
        "gate_on": "judge" if mode == "judge" else "keyword",
        "judge_model": judge if mode != "keyword" else None,
        "total": total,
        "correct": correct,
        "accuracy": correct / total if total else 0.0,
        "results": results,
    }
    if mode != "keyword":
        report["judge_summary"] = judge_lib.judge_summary(results)
    return report


def run_reliability_suite(mode: str = "keyword") -> dict:
    from ankusdrive import Worker
    CACHE_DIR.mkdir(exist_ok=True, parents=True)
    items = []

    with Worker() as w:
        for builder, spec in SHAPES:
            img_path = CACHE_DIR / f"{spec.name}_iso.png"
            txt_path = CACHE_DIR / response_cache_name(f"{spec.name}_iso", MODEL)

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
                # An empty answer (e.g. thinking used up max_tokens) is not cached, so it
                # isn't re-graded as a model failure forever. It's graded as a fail this
                # run and re-queried next run.
                if response.strip():
                    txt_path.write_text(response, encoding="utf-8")
                cached = False
                print(f"  [api] {spec.name} ({time.time() - t0:.1f}s)")
            items.append({"spec": spec, "response": response, "cached": cached})

    judge = judge_lib.judge_model() if mode != "keyword" else None
    report = grade_all(items, mode, answered_model=MODEL, judge=judge)
    (CACHE_DIR / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _verdict_tag(verdict: dict | None) -> str:
    if not verdict:
        return ""
    if verdict["status"] != "graded":
        return " judge=UNGRADED"
    return " judge=" + ("PASS" if verdict["pass"] else "FAIL")


def print_report(report: dict):
    mode = report.get("grader", "keyword")
    print()
    print("=" * 70)
    print(f"Reliability suite: {report['model']}  (grader={mode}"
          + (f", judge={report['judge_model']}" if report.get("judge_model") else "") + ")")
    print(f"  {report['correct']}/{report['total']} correct = {report['accuracy']:.0%}"
          f"  (gate on {report.get('gate_on', 'keyword')} verdicts)")
    print("=" * 70)
    for r in report["results"]:
        mark = "PASS" if r["pass"] else "FAIL"
        cache = " (cached)" if r["cached"] else ""
        extra = ""
        if mode == "both":
            extra = " keyword=" + ("PASS" if r["grade"]["pass"] else "FAIL") + _verdict_tag(r["judge"])
        print(f"\n  [{mark}] {r['shape']}{cache}{extra}")
        print(f"        spec: {r['description']}")
        # First 200 chars of response.
        truncated = r["response"].replace("\n", " ").strip()
        if len(truncated) > 220:
            truncated = truncated[:220] + "..."
        print(f"        said: {truncated}")
        if not r["pass"]:
            if mode == "judge":
                print(f"        why:  {r['judge']['reason'] if r['judge'] else 'no judge verdict'}")
            else:
                for reason in r["grade"]["reasons"]:
                    print(f"        why:  {reason}")
    print_judge_summary(report)


def print_judge_summary(report: dict):
    js = report.get("judge_summary")
    if not js:
        return
    print()
    print("-" * 70)
    print(f"Judge ({report['judge_model']}): {js['judge_correct']}/{js['graded']} pass,"
          f" {js['ungraded']} ungraded (refusal / max_tokens / bad output, not counted as pass)")
    if report.get("grader") != "both":
        return
    print(f"Judge vs keyword: {js['agree']} agree, {js['disagree']} disagree"
          f" ({js['judge_pass_keyword_fail']} judge-pass/keyword-fail,"
          f" {js['judge_fail_keyword_pass']} judge-fail/keyword-pass)."
          " The gate above uses the KEYWORD grade.")
    for d in js["disagreements"]:
        kw = "PASS" if d["keyword_pass"] else "FAIL"
        jd = "PASS" if d["judge_pass"] else "FAIL"
        print(f"\n  {d['shape']}: keyword={kw} judge={jd}")
        print(f"        judge: {d['judge_reason']}")
        for reason in d["keyword_reasons"]:
            print(f"        keyword: {reason}")


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

    mode = judge_lib.grader_mode()  # validate before spending anything
    report = run_reliability_suite(mode)
    print_report(report)

    # Hard fail if accuracy drops below a sensible bar.
    bar = 0.70
    if report["accuracy"] < bar:
        print(f"\nFAIL: accuracy {report['accuracy']:.0%} below bar {bar:.0%}")
        sys.exit(1)
    print(f"\nOK: accuracy {report['accuracy']:.0%} ≥ bar {bar:.0%}")


if __name__ == "__main__":
    main()
