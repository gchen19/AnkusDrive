"""Opt-in visual second opinion on a part from Claude's vision (#509).

The agent that built a part is a poor judge of its own render: it sees what it meant
to build. ``visual_review`` renders the standard views with the host-side software
renderer and asks an independent model, through the Anthropic API, whether the
pictures match the stated intent. The verdict is advisory: it never replaces the
geometric gates (``verify_intent``, ``check_shape``, ``verify_feature`` ...), which
measure the solid instead of looking at it.

Nothing leaves the machine by default. A call reaches the network only when ALL of:

* ``ANKUSDRIVE_VISUAL_REVIEW=1`` is set,
* ``ANTHROPIC_API_KEY`` or ``ANTHROPIC_AUTH_TOKEN`` is set, and
* the ``anthropic`` SDK is importable (``pip install ankusdrive[review]``).

Otherwise the tool returns ``{"enabled": False, "reason", "how_to_enable"}`` without
constructing a client. When enabled, what is sent is the rendered PNG views and the
intent text, nothing else (no geometry file, no document, no paths) — see PRIVACY.md.

This module is stdlib-only at import time. The renderer and the declared-intent read
are passed in by the MCP tool, so the offline test drives everything with stubs and a
fake SDK (tests/test_visual_review.py).
"""
from __future__ import annotations

import base64
import importlib.util
import json
import logging
import os

log = logging.getLogger(__name__)

ENV_ENABLE = "ANKUSDRIVE_VISUAL_REVIEW"
ENV_MODEL = "ANKUSDRIVE_VISUAL_REVIEW_MODEL"
CREDENTIAL_ENVS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_VIEWS = ("iso", "front", "top", "right")
IMAGE_PX = 512          # per view; four 512px PNGs keep a review cheap
MAX_TOKENS = 8000

ADVISORY = ("Advisory only: a vision model's reading of rendered views. It does not "
            "replace the geometric gates (verify_intent, check_shape, verify_feature), "
            "which measure the solid.")

# The plain-venv form; _install_hint() rewrites it for pipx / uv / the Claude
# Desktop extension (tests/test_install_hints.py holds every hint to that routing).
_INSTALL_HINT = "pip install 'ankusdrive[review]'  (the Anthropic SDK)"


def _install_hint() -> str:
    from ankusdrive import install_kind as _install_kind
    return _install_kind.adapt(_INSTALL_HINT)


def how_to_enable() -> str:
    return (f"1) {_install_hint()}; 2) set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) and "
            f"{ENV_ENABLE}=1 in the MCP server's environment; 3) restart the server. "
            f"Optional: {ENV_MODEL} (default {DEFAULT_MODEL}). When enabled, the rendered "
            "views and the intent text are sent to the Anthropic API.")

VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["matches_intent", "confidence", "observed_features", "discrepancies", "notes"],
    "properties": {
        "matches_intent": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "observed_features": {"type": "array", "items": {"type": "string"}},
        "discrepancies": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["feature", "expected", "observed"],
                "properties": {
                    "feature": {"type": "string"},
                    "expected": {"type": "string"},
                    "observed": {"type": "string"},
                },
            },
        },
        "notes": {"type": "string"},
    },
}

SYSTEM_PROMPT = """\
You are an independent mechanical inspector reviewing a CAD part from rendered views. \
You did not design this part, and the designer's stated intent may be wrong about what \
was actually built. Your job is to check the intent against the pictures, not to \
confirm it.

How the images were made: a flat-shaded software rasterizer of the part's tessellated \
surface, one image per named view. 'iso' is an isometric view; 'front', 'top', \
'right' (and the others) are orthographic views along a principal axis. There are no \
hidden lines, no dimensions and no scale bar, so absolute sizes can only be judged \
relative to other features in the same view.

Rules:
- Report only what is visible. List each feature you can actually see (holes, slots, \
bosses, fillets, chamfers, steps, pockets, through vs blind, counts, symmetry, \
proportions) in observed_features.
- For every element of the intent that the views contradict, add a discrepancy with \
what was expected and what you observe.
- If the views cannot resolve a feature (a blind hole seen only end-on, an internal \
cavity, a feature on a face no view shows, a dimension that needs a scale), do not \
guess either way: say so in notes and lower your confidence. Unverifiable is not the \
same as matching.
- Set matches_intent to true only if nothing visible contradicts the intent. Use \
confidence 'high' only when every element of the intent is clearly visible."""


def _sdk_available() -> bool:
    return importlib.util.find_spec("anthropic") is not None


def _load_sdk():
    import anthropic
    return anthropic


def model_name(env=None) -> str:
    env = os.environ if env is None else env
    return (env.get(ENV_MODEL) or "").strip() or DEFAULT_MODEL


def gate(env=None) -> dict | None:
    """``None`` when a review may run; otherwise the structured disabled result.
    Checks the opt-in first, so a default install never even probes credentials."""
    env = os.environ if env is None else env
    if (env.get(ENV_ENABLE) or "").strip() != "1":
        reason = f"{ENV_ENABLE} is not set to 1 (visual review is opt-in; nothing is sent by default)"
    elif not any((env.get(k) or "").strip() for k in CREDENTIAL_ENVS):
        reason = "no Anthropic credentials: set ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN"
    elif not _sdk_available():
        reason = f"the anthropic SDK is not installed: {_install_hint()}"
    else:
        return None
    return {"enabled": False, "reason": reason, "how_to_enable": how_to_enable(), "advisory": ADVISORY}


def intent_from_contract(contract: dict) -> str:
    """A declare_intent contract, as intent text for the reviewer."""
    lines = ["Declared intent contract (recorded with declare_intent):"]
    if contract.get("watertight"):
        lines.append("- the part is a closed, watertight solid")
    ap = contract.get("airtight_path")
    if ap:
        lines.append(f"- an open internal flow path from {ap.get('inlet')!r} to {ap.get('outlet')!r}"
                     + (f" with at least {ap['min_aperture_mm2']} mm^2 aperture"
                        if ap.get("min_aperture_mm2") else ""))
    if contract.get("required_faces"):
        lines.append(f"- these named faces exist: {', '.join(map(str, contract['required_faces']))}")
    extra = {k: v for k, v in contract.items() if k not in ("watertight", "airtight_path", "required_faces")}
    if extra:
        lines.append(f"- other declared fields: {json.dumps(extra, sort_keys=True)}")
    return "\n".join(lines)


def build_request(images: dict, intent: str, model: str) -> dict:
    """The ``messages.create`` keyword arguments: image blocks, then one text block."""
    content = [{"type": "image",
                "source": {"type": "base64", "media_type": "image/png",
                           "data": base64.standard_b64encode(png).decode("ascii")}}
               for png in images.values()]
    content.append({"type": "text", "text": (
        f"The {len(images)} images above are, in order, these views: {', '.join(images)}.\n\n"
        f"<intent>\n{intent}\n</intent>\n\n"
        "Inspect the part against this intent following your rules, and return the verdict.")})
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": content}],
        "output_config": {"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
    }


def _usage(resp) -> dict:
    u = getattr(resp, "usage", None)
    return {"input_tokens": getattr(u, "input_tokens", None),
            "output_tokens": getattr(u, "output_tokens", None)}


def _error(kind: str, message: str, model: str, **extra) -> dict:
    err = {"type": kind, "message": message}
    err.update({k: v for k, v in extra.items() if v is not None})
    return {"enabled": True, "ok": False, "error": err, "model": model, "advisory": ADVISORY}


def parse_response(resp, model: str) -> dict:
    """A Message -> the tool result (verdict, or a structured error)."""
    request_id = getattr(resp, "_request_id", None)
    common = {"model": getattr(resp, "model", None) or model, "usage": _usage(resp),
              "request_id": request_id}
    stop = getattr(resp, "stop_reason", None)
    if stop == "refusal":
        details = getattr(resp, "stop_details", None)
        out = _error("refusal", "the model declined to review these views",
                     common["model"], category=getattr(details, "category", None),
                     explanation=getattr(details, "explanation", None))
        return {**out, "usage": common["usage"], "request_id": request_id}
    if stop == "max_tokens":
        out = _error("max_tokens", f"the verdict was cut off at max_tokens={MAX_TOKENS}", common["model"])
        return {**out, "usage": common["usage"], "request_id": request_id}
    text = "".join(getattr(b, "text", "") for b in (getattr(resp, "content", None) or [])
                   if getattr(b, "type", None) == "text")
    try:
        verdict = json.loads(text)
        missing = [k for k in VERDICT_SCHEMA["required"] if k not in verdict]
        if missing:
            raise ValueError(f"verdict is missing {missing}")
    except (ValueError, TypeError) as e:
        out = _error("parse", f"could not read the verdict: {e}", common["model"])
        return {**out, "usage": common["usage"], "request_id": request_id}
    return {"enabled": True, "ok": True, "verdict": verdict, **common, "advisory": ADVISORY}


def review(handle: str, intent: str | None = None, views=None, *, render, read_intent,
           env=None) -> dict:
    """Run one review. ``render(views, size) -> {view: png_bytes}``; ``read_intent()
    -> contract dict`` ({} when none was declared). Neither is called while gated."""
    disabled = gate(env)
    if disabled:
        return disabled
    model = model_name(env)
    views = list(views) if views else list(DEFAULT_VIEWS)

    intent_source = "argument"
    if not (intent or "").strip():
        contract = read_intent() or {}
        if not contract:
            return _error("no_intent", f"{handle!r} has no declared intent; pass intent='...' "
                          "describing what the part should be", model)
        intent, intent_source = intent_from_contract(contract), "declare_intent"

    images = render(views, IMAGE_PX)
    sdk = _load_sdk()
    try:
        client = sdk.Anthropic()
        resp = client.messages.create(**build_request(images, intent, model))
    except sdk.AuthenticationError as e:
        return _error("authentication", "the Anthropic API rejected the credentials", model,
                      status_code=getattr(e, "status_code", None),
                      request_id=getattr(e, "request_id", None))
    except sdk.RateLimitError as e:
        return _error("rate_limit", "rate limited by the Anthropic API; retry later", model,
                      status_code=getattr(e, "status_code", None),
                      request_id=getattr(e, "request_id", None))
    except sdk.APIStatusError as e:
        return _error("api_status", str(getattr(e, "message", e)), model,
                      status_code=getattr(e, "status_code", None),
                      request_id=getattr(e, "request_id", None))
    except sdk.APIConnectionError as e:
        return _error("connection", f"could not reach the Anthropic API: {e}", model)
    log.info("visual_review %s: model=%s request_id=%s stop_reason=%s", handle,
             getattr(resp, "model", model), getattr(resp, "_request_id", None),
             getattr(resp, "stop_reason", None))
    out = parse_response(resp, model)
    out.update({"handle": handle, "views": views, "intent_source": intent_source})
    return out
