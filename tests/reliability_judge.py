"""
LLM-as-judge grader for the reliability suites (issue #507).

The keyword grader in ``test_reliability.py`` fails correct paraphrases
("rectangular prism" for a box) and trips on negations ("not a sphere" hits
``must_not_match: sphere``). This module adds an opt-in judge: a second Claude
call that reads the shape's ground-truth description, the keyword spec (as a
hint), and the answer under test, and returns a structured ``{pass, reason}``
verdict through structured outputs (``output_config.format``), so there is no
free-text parsing.

Pick the grader with ``RELIABILITY_GRADER``:

* ``keyword`` (default) - the keyword grader only. No extra API calls.
* ``judge`` - the judge's verdict drives the accuracy gate.
* ``both`` - both run; the gate stays on keyword, and the report lists every
  case where the two disagree, with the judge's reason. Use it to find keyword
  specs that need fixing.

The judge model is ``RELIABILITY_JUDGE_MODEL`` (alias or full ID, default
``opus``). The judge sees text only, not the render, which keeps it cheap.

Verdicts are cached as JSON next to the cached responses, keyed by the judge
model AND the answered model, so reruns are free. A cached verdict is reused
only if the answer text and the judge prompt version still match. Ungraded
results (refusal, ``max_tokens``, unparseable output) are not cached, so the
next run retries them.

Import-light on purpose: no ``anthropic`` and no ``ankusdrive`` at import time,
so the offline test runs on the FreeCAD-free CI lane with a fake client.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from orchestration.claude_models import first_text, resolve_model, response_cache_name, usage_dict

GRADER_MODES = ("keyword", "judge", "both")

# Bump when the judge prompt or schema changes: cached verdicts from the old
# prompt are then ignored instead of silently reused.
JUDGE_PROMPT_VERSION = 1

JUDGE_MAX_TOKENS = 4096  # headroom: current models think before they answer

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "pass": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["pass", "reason"],
    "additionalProperties": False,
}

JUDGE_SYSTEM = """\
You grade answers from a vision test. A model was shown a rendered image of a \
3D mechanical part and asked to describe it. You do not see the image. You get \
the ground-truth description of the part and the model's answer, and you decide \
whether the answer correctly describes the part.

Grade on meaning, not wording:
- A paraphrase or synonym is correct. "Rectangular prism" or "cuboid" is a box; \
"bore" or "through-passage" is a hole; "rounded corner" is a fillet.
- Only count what the answer claims the part IS or HAS. A negation or an \
explicit rejection ("not a sphere", "no holes visible", "I first thought it was \
a cylinder, but it is a box") is not a claim of that shape or feature.
- Pass when the answer identifies the overall shape and every feature that the \
ground truth names as distinctive (holes, fillets, pockets, steps, an L form, a \
count of holes). Exact dimensions are not required, since they cannot be read \
from an unscaled render.
- Fail when the answer misses a distinctive feature, claims a feature or shape \
that is not there, or hedges between options without committing to the right one.

The keyword spec shows what a simpler keyword grader looks for. Use it as a \
hint about which features matter, not as a word list the answer must contain.

Keep the reason to one or two sentences, and name the deciding phrase from the \
answer."""


def grader_mode(env: str = "RELIABILITY_GRADER") -> str:
    """The grader mode from the environment, validated. Default ``keyword``."""
    mode = (os.environ.get(env) or "keyword").strip().lower()
    if mode not in GRADER_MODES:
        raise ValueError(f"{env}={mode!r}: use one of {list(GRADER_MODES)}")
    return mode


def judge_model() -> str:
    """The judge model: ``RELIABILITY_JUDGE_MODEL`` (alias or ID), default ``opus``.

    Deliberately does not fall back to ``ANKUSDRIVE_EVAL_MODEL``: that variable
    picks the model under test, and a model grading its own answers is a weaker
    check.
    """
    return resolve_model(env="RELIABILITY_JUDGE_MODEL", default="opus")


def _spec_summary(spec) -> str:
    lines = [f"- primary shape, any of: {spec.must_match_any}"]
    for group in spec.must_match_all:
        lines.append(f"- required feature, any of: {group}")
    if spec.must_not_match:
        lines.append(f"- wrong answers would claim: {spec.must_not_match}")
    return "\n".join(lines)


def build_judge_prompt(spec, answer: str, question: str) -> str:
    """The user turn for the judge: question, ground truth, keyword hint, answer."""
    return (
        f"<question_asked>\n{question}\n</question_asked>\n\n"
        f"<ground_truth>\n{spec.description}\n</ground_truth>\n\n"
        f"<keyword_spec>\n{_spec_summary(spec)}\n</keyword_spec>\n\n"
        f"<answer>\n{answer}\n</answer>\n\n"
        "Does the answer correctly describe the part? Return your verdict."
    )


def build_judge_request(spec, answer: str, question: str, model: str) -> dict:
    """Keyword arguments for ``client.messages.create``."""
    return {
        "model": model,
        "max_tokens": JUDGE_MAX_TOKENS,
        "system": JUDGE_SYSTEM,
        "messages": [{"role": "user", "content": build_judge_prompt(spec, answer, question)}],
        "output_config": {"format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
    }


def _ungraded(reason: str, stop_reason, resp=None) -> dict:
    return {"status": "ungraded", "pass": None, "reason": reason,
            "stop_reason": stop_reason,
            "usage": usage_dict(resp) if resp is not None and getattr(resp, "usage", None) else None}


def parse_verdict(resp) -> dict:
    """Turn a judge response into a verdict dict.

    ``status`` is ``graded`` with a boolean ``pass``, or ``ungraded`` with
    ``pass: None`` when the judge refused, ran out of tokens, or returned
    something that does not match the schema. An ungraded result is never a pass.
    """
    stop = getattr(resp, "stop_reason", None)
    if stop == "refusal":
        details = getattr(resp, "stop_details", None)
        cat = getattr(details, "category", None) if details else None
        return _ungraded(f"judge refused (category={cat})", stop, resp)
    if stop == "max_tokens":
        return _ungraded(f"judge hit max_tokens={JUDGE_MAX_TOKENS} before a verdict", stop, resp)
    text = first_text(resp)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return _ungraded(f"judge output is not JSON: {text[:120]!r}", stop, resp)
    if not isinstance(data, dict) or not isinstance(data.get("pass"), bool):
        return _ungraded(f"judge output does not match the schema: {text[:120]!r}", stop, resp)
    return {"status": "graded", "pass": data["pass"], "reason": str(data.get("reason", "")),
            "stop_reason": stop, "usage": usage_dict(resp)}


def judge_answer(client, spec, answer: str, question: str, model: str) -> dict:
    """One judge call. ``client`` is an ``anthropic.Anthropic`` or a test fake."""
    resp = client.messages.create(**build_judge_request(spec, answer, question, model))
    return parse_verdict(resp)


def verdict_cache_path(cache_dir: Path, stem: str, answered_model: str, judge: str) -> Path:
    """``<stem>.<answered model>.judge.<judge model>.json``."""
    return Path(cache_dir) / response_cache_name(
        response_cache_name(stem, answered_model, "judge"), judge, "json")


def _fingerprint(spec, answer: str, question: str) -> str:
    # The keyword lists are left out on purpose: they are only a hint to the judge,
    # and fixing them after a `both` run should not re-bill every verdict.
    h = hashlib.sha256()
    for part in (str(JUDGE_PROMPT_VERSION), JUDGE_SYSTEM, question, spec.description, answer):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def cached_judge(get_client, spec, answer: str, question: str, *, cache_dir: Path,
                 stem: str, answered_model: str, judge: str) -> dict:
    """The judge verdict for one answer, from the disk cache when possible.

    ``get_client`` is a zero-argument callable, called only on a cache miss, so a
    fully cached run never constructs a client (or needs an API key).
    """
    path = verdict_cache_path(cache_dir, stem, answered_model, judge)
    fp = _fingerprint(spec, answer, question)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = None
        if isinstance(data, dict) and data.get("fingerprint") == fp:
            return {**data["verdict"], "cached": True}
    verdict = judge_answer(get_client(), spec, answer, question, judge)
    if verdict["status"] == "graded":
        path.write_text(json.dumps({"fingerprint": fp, "judge_model": judge,
                                    "answered_model": answered_model, "verdict": verdict},
                                   indent=2), encoding="utf-8")
    return {**verdict, "cached": False}


def gate_pass(keyword_grade: dict, judge_verdict: dict | None, mode: str) -> bool:
    """The pass/fail that counts toward the accuracy gate in ``mode``.

    ``judge`` mode uses the judge verdict (an ungraded verdict counts as a fail).
    ``keyword`` and ``both`` use the keyword grade.
    """
    if mode == "judge":
        return bool(judge_verdict and judge_verdict.get("pass") is True)
    return bool(keyword_grade["pass"])


def disagreements(results: list[dict]) -> list[dict]:
    """Cases where a graded judge verdict and the keyword grade differ.

    Each result needs ``shape``, ``grade`` (keyword) and ``judge`` (or None).
    Ungraded judge results are not disagreements; they are counted separately.
    """
    out = []
    for r in results:
        j = r.get("judge")
        if not j or j.get("status") != "graded":
            continue
        if j["pass"] != r["grade"]["pass"]:
            out.append({
                "shape": r["shape"],
                "keyword_pass": r["grade"]["pass"],
                "judge_pass": j["pass"],
                "judge_reason": j.get("reason", ""),
                "keyword_reasons": r["grade"].get("reasons", []),
            })
    return out


def judge_summary(results: list[dict]) -> dict:
    """Counts for the report: judged, graded, ungraded, judge passes, disagreements."""
    judged = [r for r in results if r.get("judge")]
    graded = [r for r in judged if r["judge"]["status"] == "graded"]
    dis = disagreements(results)
    return {
        "judged": len(judged),
        "graded": len(graded),
        "ungraded": len(judged) - len(graded),
        "judge_correct": sum(1 for r in graded if r["judge"]["pass"]),
        "agree": len(graded) - len(dis),
        "disagree": len(dis),
        "judge_pass_keyword_fail": sum(1 for d in dis if d["judge_pass"]),
        "judge_fail_keyword_pass": sum(1 for d in dis if not d["judge_pass"]),
        "disagreements": dis,
    }
