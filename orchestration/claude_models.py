"""
One place for the Claude model IDs, prices and response helpers that the
host-side eval harnesses share (issue #506).

Before this module, five files each had their own copy of ``MODELS`` / ``PRICING``.
The copies drifted: the IDs stayed a generation behind, and the Opus row billed at
$15/$75 per MTok, which is the old Opus 3/4.0 rate and 3x Opus 4.8's real $5/$25.
Every harness also read ``resp.content[0].text``, which raises ``AttributeError`` on
current models because a ``thinking`` block can come first.

Dependency-free on purpose: it imports neither ``anthropic`` nor ``ankusdrive``, so
offline tests and dry runs can import it with no API key and no FreeCAD.

Choosing a model for a run:

* ``ANKUSDRIVE_EVAL_MODEL`` takes a tier alias (``opus`` / ``sonnet`` / ``haiku``)
  or a full model ID. It is the default for every harness that calls
  :func:`resolve_model` with no explicit choice.
* The older per-harness knobs (``M2_MODEL``, ``ANKUSDRIVE_MODELS``) still work and
  now also accept full IDs, so a past result can be reproduced exactly with, for
  example, ``M2_MODEL=claude-haiku-4-5``.
"""
from __future__ import annotations

import os
import re

# Tier aliases resolve to the CURRENT generation. Pin a full ID to compare against
# results recorded on an older model (tests/MULTI_AGENT_EVAL.md and RELIABILITY.md
# rows from 2026-06 used the 4.x IDs listed under LEGACY below).
MODELS = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}

DEFAULT_TIER = "opus"

# USD per million tokens: (input, cache write 5-min, cache read, output).
# Source: Anthropic first-party API list prices as of 2026-11 (platform pricing
# page). Cache-read ratios really do differ by model: Opus 5.5 / Sonnet 5.5 /
# Fable 5.2 list cache reads below the classic 0.1x input. Cache writes are 1.25x
# input throughout. The Haiku 5.5 cache-read figure assumes the classic 0.1x and
# needs checking against the pricing page. Haiku 5.5 is priced for prompts up to
# 100K tokens (longer prompts cost $0.50/$2.50, which none of these harnesses reach).
PRICING = {
    # current generation
    "claude-opus-5-5": (4.00, 5.00, 0.20, 20.00),
    "claude-sonnet-5-5": (2.00, 2.50, 0.10, 10.00),
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),
    "claude-fable-5-2": (8.00, 10.00, 0.20, 40.00),
    # still served; kept so recorded runs can be re-costed
    "claude-opus-5": (5.00, 6.25, 0.50, 25.00),
    "claude-sonnet-5": (2.00, 2.50, 0.20, 10.00),
    "claude-opus-4-8": (5.00, 6.25, 0.50, 25.00),
    "claude-sonnet-4-5": (3.00, 3.75, 0.30, 15.00),
    "claude-haiku-4-5": (1.00, 1.25, 0.10, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 1.25, 0.10, 5.00),
}

# Models that return a 400 for a forced tool_choice ({"type": "any"} or
# {"type": "tool", ...}). On these, harnesses fall back to "auto" and rely on the
# system prompt ("call emit_review exactly once") plus a default when no call comes.
_NO_FORCED_TOOL_CHOICE = re.compile(
    r"^claude-(opus-5-5|sonnet-5-5|fable-5-[12]|mythos-5-[12])\b")


def resolve_model(choice: str | None = None, *, env: str = "ANKUSDRIVE_EVAL_MODEL",
                  default: str = DEFAULT_TIER, require_pricing: bool = True) -> str:
    """Turn an alias or a full ID into a model ID.

    Precedence: an explicit ``choice``, then the ``env`` variable, then ``default``.
    An explicit empty string is an error. It does not fall through to the default,
    so a stray comma in a model list can't quietly add an Opus run.

    A full ID (``claude-...``) passes through. With ``require_pricing`` (the
    default), the ID must have a ``PRICING`` row. A harness that reports
    ``cost_of`` then fails here, before any API spend, not after a billed trial.
    Pass ``require_pricing=False`` only from a harness that never calls ``cost_of``.
    An unknown alias raises ``ValueError`` with the valid aliases listed.
    """
    if choice is not None and not choice.strip():
        raise ValueError("empty model name")
    raw = (choice or os.environ.get(env) or default).strip()
    mid = MODELS.get(raw, raw)
    if not mid.startswith("claude-"):
        raise ValueError(
            f"unknown model {raw!r}: use one of {sorted(MODELS)} or a full 'claude-...' ID")
    if require_pricing and mid not in PRICING:
        raise ValueError(
            f"no pricing for {mid!r}: add it to orchestration/claude_models.PRICING "
            "(cost reports would otherwise fail after the paid call)")
    return mid


def cost_of(usage: dict, model: str) -> float:
    """USD for a ``{in_tokens, cache_write, cache_read, out_tokens}`` usage dict."""
    try:
        pi, pcw, pcr, po = PRICING[model]
    except KeyError:
        raise KeyError(f"no pricing for {model!r}; add it to orchestration/claude_models.PRICING") from None
    return (usage.get("in_tokens", 0) * pi + usage.get("cache_write", 0) * pcw
            + usage.get("cache_read", 0) * pcr + usage.get("out_tokens", 0) * po) / 1e6


def usage_dict(resp) -> dict:
    """Pull the four billed token counts off an SDK response (or a stub)."""
    u = resp.usage
    return {"in_tokens": getattr(u, "input_tokens", 0) or 0,
            "out_tokens": getattr(u, "output_tokens", 0) or 0,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0}


def first_text(resp) -> str:
    """The text answer from a response, skipping thinking / tool blocks.

    All text blocks are joined, because a cited or interleaved answer can be split
    across several. Returns ``""`` if the model produced no text. Callers that
    grade the answer then see a clean fail, not an ``AttributeError``.
    """
    parts = [getattr(b, "text", "") for b in getattr(resp, "content", None) or []
             if getattr(b, "type", None) == "text"]
    return "".join(parts)


def tool_choice_for(model: str, tool_name: str) -> dict:
    """A forced choice of ``tool_name`` where the model accepts one, else ``auto``."""
    if _NO_FORCED_TOOL_CHOICE.match(model):
        return {"type": "auto"}
    return {"type": "tool", "name": tool_name}


def response_cache_name(stem: str, model: str, ext: str = "txt") -> str:
    """File name for a cached model RESPONSE, keyed by model as well as case.

    The reliability caches used to be keyed by case name only. After a model
    change, a rerun silently graded the old model's answers and labelled the
    report with the new model's name. Renders stay model-independent and keep
    their old names.
    """
    slug = re.sub(r"[^A-Za-z0-9.-]+", "_", model)
    return f"{stem}.{slug}.{ext}"
