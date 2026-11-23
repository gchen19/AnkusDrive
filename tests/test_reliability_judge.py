"""
Offline test for the reliability LLM judge (issue #507). No API key, no network,
no FreeCAD: every judge call goes to a fake client that records the request and
returns a canned response.

Covers: the request shape (model, structured-output schema), verdict parsing,
refusal / max_tokens / bad-JSON handling (ungraded, never a pass), the verdict
cache (a hit never touches the client; a changed answer or another judge model
misses), and the judge-vs-keyword disagreement accounting and gate selection.

Run: .venv/bin/python tests/test_reliability_judge.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import reliability_judge as J  # noqa: E402
from tests import test_reliability as R  # noqa: E402  (must not need FreeCAD)
from tests.reliability_shapes import SHAPES  # noqa: E402

SPECS = {spec.name: spec for _, spec in SHAPES}
FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def fake_response(text=None, stop_reason="end_turn", category=None):
    content = [SimpleNamespace(type="thinking", thinking="")]
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        content=content, stop_reason=stop_reason,
        stop_details=SimpleNamespace(type="refusal", category=category) if stop_reason == "refusal" else None,
        usage=SimpleNamespace(input_tokens=500, output_tokens=60,
                              cache_read_input_tokens=0, cache_creation_input_tokens=0))


class FakeClient:
    """Answers each judge call with ``responder(request_kwargs)``; records calls."""

    def __init__(self, responder):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self._responder = responder

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responder(kwargs)


def verdict(passed, reason="r"):
    return lambda _kw: fake_response(json.dumps({"pass": passed, "reason": reason}))


def test_request_shape():
    print("request shape")
    spec = SPECS["cube_with_hole"]
    client = FakeClient(verdict(True))
    J.judge_answer(client, spec, "A rectangular prism with a bore through it.", R.PROMPT,
                   "claude-opus-5-5")
    check(len(client.calls) == 1, "one call per judgement")
    kw = client.calls[0]
    check(kw["model"] == "claude-opus-5-5", "model passed through")
    check(kw["max_tokens"] >= 4096, "max_tokens has thinking headroom")
    fmt = kw.get("output_config", {}).get("format", {})
    check(fmt.get("type") == "json_schema", "structured outputs: output_config.format json_schema")
    schema = fmt.get("schema", {})
    check(schema.get("additionalProperties") is False, "schema closes additionalProperties")
    check(set(schema.get("required", [])) == {"pass", "reason"}, "schema requires pass + reason")
    check(schema["properties"]["pass"]["type"] == "boolean", "pass is boolean")
    user = kw["messages"][0]["content"]
    check(spec.description in user, "prompt carries the ground-truth description")
    check("bore through it" in user, "prompt carries the answer under test")
    check(str(spec.must_match_all[0]) in user, "prompt carries the keyword spec")
    check(R.PROMPT in user, "prompt carries the question that was asked")
    check(not any(isinstance(m["content"], list) for m in kw["messages"]), "text-only: no image block")
    check("negation" in kw["system"].lower() and "paraphrase" in kw["system"].lower(),
          "system prompt covers paraphrase and negation")
    check("tool_choice" not in kw and "tools" not in kw, "no tool fallback needed")


def test_parse():
    print("verdict parsing")
    v = J.parse_verdict(fake_response('{"pass": true, "reason": "says cuboid with bore"}'))
    check(v["status"] == "graded" and v["pass"] is True, "graded pass, thinking block skipped")
    check(v["reason"] == "says cuboid with bore", "reason kept")
    check(v["usage"]["in_tokens"] == 500, "usage recorded")
    v = J.parse_verdict(fake_response('{"pass": false, "reason": "no hole"}'))
    check(v["status"] == "graded" and v["pass"] is False, "graded fail")

    v = J.parse_verdict(fake_response(None, stop_reason="refusal", category="cyber"))
    check(v["status"] == "ungraded" and v["pass"] is None, "refusal -> ungraded, pass None")
    check("refused" in v["reason"] and "cyber" in v["reason"], "refusal reason names category")
    v = J.parse_verdict(fake_response('{"pass": tr', stop_reason="max_tokens"))
    check(v["status"] == "ungraded" and v["pass"] is None, "max_tokens -> ungraded")
    v = J.parse_verdict(fake_response("looks right to me"))
    check(v["status"] == "ungraded", "non-JSON -> ungraded")
    v = J.parse_verdict(fake_response('{"pass": "yes", "reason": "x"}'))
    check(v["status"] == "ungraded", "non-boolean pass -> ungraded")
    check(J.gate_pass({"pass": True}, {"status": "ungraded", "pass": None}, "judge") is False,
          "ungraded never counts as a pass in judge mode")


def test_cache():
    print("verdict cache")
    spec = SPECS["cube"]
    answer = "A plain cube."
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        client = FakeClient(verdict(True, "cube"))
        kw = dict(cache_dir=d, stem="cube_iso", answered_model="claude-sonnet-5-5",
                  judge="claude-opus-5-5")
        v1 = J.cached_judge(lambda: client, spec, answer, R.PROMPT, **kw)
        check(len(client.calls) == 1 and v1["cached"] is False, "miss calls the client")
        path = J.verdict_cache_path(d, "cube_iso", "claude-sonnet-5-5", "claude-opus-5-5")
        check(path.name == "cube_iso.claude-sonnet-5-5.judge.claude-opus-5-5.json",
              f"cache keyed by answered + judge model ({path.name})")
        check(path.exists(), "verdict written next to the responses")

        def boom():
            raise AssertionError("client constructed on a cache hit")
        v2 = J.cached_judge(boom, spec, answer, R.PROMPT, **kw)
        check(v2["cached"] is True and v2["pass"] is True and v2["reason"] == "cube",
              "hit returns the stored verdict without constructing a client")

        from dataclasses import replace
        J.cached_judge(boom, replace(spec, must_match_any=["cube", "cuboid"]), answer, R.PROMPT, **kw)
        check(True, "editing a keyword list keeps the cached verdict (hint only)")
        J.cached_judge(lambda: client, replace(spec, description="Solid 30mm cube"), answer,
                       R.PROMPT, **kw)
        check(len(client.calls) == 2, "editing the ground-truth description re-judges")

        J.cached_judge(lambda: client, spec, "A sphere.", R.PROMPT, **kw)
        check(len(client.calls) == 3, "changed answer text misses the cache")
        J.cached_judge(lambda: client, spec, answer, R.PROMPT, **{**kw, "judge": "claude-sonnet-5-5"})
        check(len(client.calls) == 4, "another judge model misses the cache")

        refuser = FakeClient(lambda _kw: fake_response(None, stop_reason="refusal"))
        kw2 = {**kw, "stem": "sphere_iso"}
        J.cached_judge(lambda: refuser, spec, answer, R.PROMPT, **kw2)
        J.cached_judge(lambda: refuser, spec, answer, R.PROMPT, **kw2)
        check(len(refuser.calls) == 2, "ungraded verdicts are not cached (retried next run)")


def test_disagreements_and_gate():
    print("disagreement accounting + gate")
    # Keyword grader outcomes on these answers (the classic #507 failure modes):
    answers = {
        "cube": "A solid cube; it is not a sphere.",            # kw FAIL (anti 'sphere'), judge PASS
        "rect_box": "A rectangular prism, longer than it is tall.",  # kw PASS, judge PASS
        "cylinder": "A cylinder.",                               # kw PASS, judge FAIL (contrived)
        "sphere": "A ball.",                                     # kw PASS, judge refuses
    }
    judge_says = {"cube": (True, "negation is not a claim"), "rect_box": (True, "box"),
                  "cylinder": (False, "contrived fail")}

    def responder(kw):
        user = kw["messages"][0]["content"]
        for name, (p, why) in judge_says.items():
            if SPECS[name].description in user:
                return fake_response(json.dumps({"pass": p, "reason": why}))
        return fake_response(None, stop_reason="refusal")

    items = [{"spec": SPECS[n], "response": a, "cached": True} for n, a in answers.items()]
    for mode, want_correct, want_gate in (("keyword", 3, "keyword"), ("both", 3, "keyword"),
                                          ("judge", 2, "judge")):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient(responder)
            rep = R.grade_all(items, mode, answered_model="claude-sonnet-5-5",
                              judge="claude-opus-5-5", get_client=lambda c=client: c,
                              cache_dir=Path(d))
        check(rep["correct"] == want_correct, f"{mode}: gate counts {want_correct}/4 (got {rep['correct']})")
        check(rep["gate_on"] == want_gate, f"{mode}: gate on {want_gate}")
        if mode == "keyword":
            check(not client.calls and "judge_summary" not in rep, "keyword: no judge calls")
            continue
        js = rep["judge_summary"]
        check(len(client.calls) == 4, f"{mode}: one judge call per answer")
        check((js["graded"], js["ungraded"], js["judge_correct"]) == (3, 1, 2),
              f"{mode}: 3 graded, 1 ungraded, 2 judge passes")
        check((js["agree"], js["disagree"]) == (1, 2), f"{mode}: 1 agree, 2 disagree")
        check((js["judge_pass_keyword_fail"], js["judge_fail_keyword_pass"]) == (1, 1),
              f"{mode}: one disagreement each way")
        by = {x["shape"]: x for x in js["disagreements"]}
        check(set(by) == {"cube", "cylinder"}, f"{mode}: disagreements are cube + cylinder")
        check(by["cube"]["judge_reason"] == "negation is not a claim", f"{mode}: judge reason carried")
        check(json.dumps(rep) is not None, f"{mode}: report is JSON-serialisable")
        if mode == "both":
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                R.print_report(rep)
            out = buf.getvalue()
            check("2 disagree" in out and "negation is not a claim" in out,
                  "both: report prints the disagreement section with reasons")
            check("gate above uses the KEYWORD grade" in out, "both: report says the gate is keyword")


def test_mode_and_model_env():
    print("env knobs")
    saved = {k: os.environ.pop(k, None) for k in
             ("RELIABILITY_GRADER", "RELIABILITY_JUDGE_MODEL", "ANKUSDRIVE_EVAL_MODEL")}
    try:
        check(J.grader_mode() == "keyword", "default grader is keyword")
        os.environ["RELIABILITY_GRADER"] = "Both"
        check(J.grader_mode() == "both", "mode is case-insensitive")
        os.environ["RELIABILITY_GRADER"] = "llm"
        try:
            J.grader_mode()
            check(False, "bad mode raises")
        except ValueError:
            check(True, "bad mode raises")
        os.environ["ANKUSDRIVE_EVAL_MODEL"] = "haiku"
        check(J.judge_model() == "claude-opus-5-5",
              "judge defaults to opus, not the model under test")
        os.environ["RELIABILITY_JUDGE_MODEL"] = "sonnet"
        check(J.judge_model() == "claude-sonnet-5-5", "RELIABILITY_JUDGE_MODEL alias")
        os.environ["RELIABILITY_JUDGE_MODEL"] = "claude-opus-5"
        check(J.judge_model() == "claude-opus-5", "RELIABILITY_JUDGE_MODEL full ID")
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_sdk_accepts_output_config():
    print("SDK signature (skipped when anthropic is not installed)")
    try:
        import inspect

        import anthropic
    except ImportError:
        print("  skip anthropic not installed")
        return
    params = inspect.signature(anthropic.resources.messages.Messages.create).parameters
    check("output_config" in params, f"anthropic {anthropic.__version__} messages.create takes output_config")


def main():
    check("ankusdrive" not in sys.modules,
          "importing test_reliability did not import ankusdrive (CI lane has no FreeCAD)")
    test_request_shape()
    test_parse()
    test_cache()
    test_disagreements_and_gate()
    test_mode_and_model_env()
    test_sdk_accepts_output_config()
    if FAILS:
        print(f"\n{len(FAILS)} FAILED")
        sys.exit(1)
    print("\nOK: reliability judge offline checks pass")


if __name__ == "__main__":
    main()
