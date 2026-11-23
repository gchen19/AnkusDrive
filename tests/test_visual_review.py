"""visual_review (#509) — opt-in, nothing leaves the machine by default, verdict parsed.

Offline: no FreeCAD, no network, no API key. The render step and the declared-intent
read are stubs, and the Anthropic SDK is a fake module, so this runs on the bare
fast-checks runner. Covers:

  * the gate — each missing condition (opt-in flag, credentials, SDK) returns the
    structured disabled result and constructs NO client, renders nothing;
  * the request — image blocks then one text block carrying the intent, the model
    (default and env override), max_tokens, output_config json_schema, no
    temperature / thinking;
  * verdict parsing, the declared-intent fallback, refusal / max_tokens / parse
    errors, and SDK error mapping (most-specific first);
  * the registries (annotations open-world, toolset family, privacy note).

When the real ``anthropic`` SDK is importable, one more tier sends the request
through the real client into a mock HTTP transport (still no network), so the
SDK's own serialization of output_config and its error classes are exercised.

Run:  python3 tests/test_visual_review.py
"""
import base64
import importlib.util
import json
import sys
import time
import traceback
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "ankusdrive"
sys.path.insert(0, str(REPO))   # visual_review routes its install hint through ankusdrive.install_kind


def _vr():
    spec = importlib.util.spec_from_file_location("visual_review_under_test", PKG / "visual_review.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._sdk_available = lambda: True
    return mod


ON = {"ANKUSDRIVE_VISUAL_REVIEW": "1", "ANTHROPIC_API_KEY": "test-key-not-real"}
PNG = b"\x89PNG\r\n\x1a\nfake"
VERDICT = {"matches_intent": False, "confidence": "medium",
           "observed_features": ["rectangular plate", "one through hole"],
           "discrepancies": [{"feature": "holes", "expected": "4", "observed": "1"}],
           "notes": "underside not shown"}


# --- a fake SDK ----------------------------------------------------------------

class _Block:
    def __init__(self, type, text=""):
        self.type, self.text = type, text


class _Resp:
    def __init__(self, text=json.dumps(VERDICT), stop_reason="end_turn", stop_details=None,
                 model="claude-opus-5-5"):
        self.content = [_Block("thinking"), _Block("text", text)]
        self.stop_reason, self.stop_details, self.model = stop_reason, stop_details, model
        self.usage = types.SimpleNamespace(input_tokens=1234, output_tokens=56)
        self._request_id = "req_test"


def _fake_sdk(resp=None, raises=None):
    sdk = types.SimpleNamespace(constructed=0, requests=[])

    class APIError(Exception):
        pass

    class APIStatusError(APIError):
        def __init__(self, message, status_code):
            super().__init__(message)
            self.message, self.status_code, self.request_id = message, status_code, "req_err"

    class AuthenticationError(APIStatusError):
        pass

    class RateLimitError(APIStatusError):
        pass

    class APIConnectionError(APIError):
        pass

    class Messages:
        def create(self, **kw):
            sdk.requests.append(kw)
            if raises:
                raise raises(sdk)
            return resp or _Resp()

    class Anthropic:
        def __init__(self, **kw):
            sdk.constructed += 1
            self.messages = Messages()

    sdk.Anthropic, sdk.APIError, sdk.APIStatusError = Anthropic, APIError, APIStatusError
    sdk.AuthenticationError, sdk.RateLimitError = AuthenticationError, RateLimitError
    sdk.APIConnectionError = APIConnectionError
    return sdk


def _run(vr, sdk, env=ON, intent="a 40x20x5 mm plate with four 4 mm through holes",
         views=None, contract=None):
    calls = {"render": 0, "intent": 0}
    vr._load_sdk = lambda: sdk

    def render(names, size):
        calls["render"] += 1
        assert size == vr.IMAGE_PX
        return {n: PNG for n in names}

    def read_intent():
        calls["intent"] += 1
        return contract or {}

    out = vr.review("Plate", intent, views, render=render, read_intent=read_intent, env=env)
    return out, calls


# --- gate ----------------------------------------------------------------------

def test_disabled_paths_construct_no_client_and_render_nothing():
    cases = {
        "flag unset": {"ANTHROPIC_API_KEY": "k"},
        "flag not 1": {"ANKUSDRIVE_VISUAL_REVIEW": "true", "ANTHROPIC_API_KEY": "k"},
        "no credentials": {"ANKUSDRIVE_VISUAL_REVIEW": "1"},
        "blank credentials": {"ANKUSDRIVE_VISUAL_REVIEW": "1", "ANTHROPIC_API_KEY": "  "},
        "empty env": {},
    }
    for label, env in cases.items():
        vr, sdk = _vr(), _fake_sdk()
        out, calls = _run(vr, sdk, env=env)
        assert out["enabled"] is False, (label, out)
        assert out["reason"] and "ANKUSDRIVE_VISUAL_REVIEW=1" in out["how_to_enable"], (label, out)
        assert "ankusdrive[review]" in out["how_to_enable"], out
        assert "advisory" in out.get("advisory", "").lower(), out
        assert sdk.constructed == 0 and not sdk.requests, f"{label}: a client was built"
        assert calls == {"render": 0, "intent": 0}, (label, calls)
    vr, sdk = _vr(), _fake_sdk()
    vr._sdk_available = lambda: False
    out, calls = _run(vr, sdk)
    assert out["enabled"] is False and "anthropic" in out["reason"], out
    assert sdk.constructed == 0 and calls["render"] == 0
    # The flag is checked first: a default install never reports on credentials.
    assert "ANKUSDRIVE_VISUAL_REVIEW" in _vr().gate({})["reason"]


def test_auth_token_alone_enables():
    vr = _vr()
    assert vr.gate({"ANKUSDRIVE_VISUAL_REVIEW": "1", "ANTHROPIC_AUTH_TOKEN": "t"}) is None


def test_real_sdk_probe_is_find_spec():
    spec = importlib.util.spec_from_file_location("vr_probe", PKG / "visual_review.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._sdk_available() is (importlib.util.find_spec("anthropic") is not None)


# --- request shape -------------------------------------------------------------

def test_request_shape():
    vr, sdk = _vr(), _fake_sdk()
    out, calls = _run(vr, sdk)
    assert out["ok"] and sdk.constructed == 1 and len(sdk.requests) == 1, out
    req = sdk.requests[0]
    assert req["model"] == "claude-opus-5-5" == vr.DEFAULT_MODEL
    assert req["max_tokens"] == vr.MAX_TOKENS and 4000 <= req["max_tokens"] <= 16000
    for banned in ("temperature", "top_p", "top_k", "thinking"):
        assert banned not in req, f"{banned} must not be set"
    assert req["output_config"] == {"format": {"type": "json_schema", "schema": vr.VERDICT_SCHEMA}}
    assert "independent" in req["system"] and "not" in req["system"]
    (msg,) = req["messages"]
    assert msg["role"] == "user"
    blocks = msg["content"]
    images, text = blocks[:-1], blocks[-1]
    assert len(images) == 4 and all(b["type"] == "image" for b in images)
    for b in images:
        assert b["source"]["type"] == "base64" and b["source"]["media_type"] == "image/png"
        assert base64.standard_b64decode(b["source"]["data"]) == PNG
    assert text["type"] == "text"
    assert "four 4 mm through holes" in text["text"]
    assert "iso, front, top, right" in text["text"]
    assert out["views"] == list(vr.DEFAULT_VIEWS) and out["intent_source"] == "argument"
    assert calls == {"render": 1, "intent": 0}


def test_model_env_override_and_custom_views():
    vr, sdk = _vr(), _fake_sdk()
    out, _ = _run(vr, sdk, env={**ON, "ANKUSDRIVE_VISUAL_REVIEW_MODEL": "claude-sonnet-5-5"},
                  views=["iso", "bottom"])
    req = sdk.requests[0]
    assert req["model"] == "claude-sonnet-5-5"
    assert len(req["messages"][0]["content"]) == 3
    assert out["views"] == ["iso", "bottom"]


def test_schema_is_closed_and_fully_required():
    vr = _vr()

    def walk(s):
        if s.get("type") == "object":
            assert s["additionalProperties"] is False, s
            assert set(s["required"]) == set(s["properties"]), s
            for v in s["properties"].values():
                walk(v)
        if s.get("type") == "array":
            walk(s["items"])
    walk(vr.VERDICT_SCHEMA)
    assert set(vr.VERDICT_SCHEMA["properties"]) == {
        "matches_intent", "confidence", "observed_features", "discrepancies", "notes"}
    assert vr.VERDICT_SCHEMA["properties"]["confidence"]["enum"] == ["low", "medium", "high"]


# --- verdicts and errors ---------------------------------------------------------

def test_verdict_parsed_with_usage_model_and_advisory():
    vr, sdk = _vr(), _fake_sdk()
    out, _ = _run(vr, sdk)
    assert out["enabled"] is True and out["ok"] is True
    assert out["verdict"] == VERDICT
    assert out["model"] == "claude-opus-5-5"
    assert out["usage"] == {"input_tokens": 1234, "output_tokens": 56}
    assert out["request_id"] == "req_test"
    assert "does not replace" in out["advisory"] and "verify_intent" in out["advisory"]


def test_declared_intent_is_used_when_none_passed():
    vr, sdk = _vr(), _fake_sdk()
    contract = {"watertight": True, "required_faces": ["inlet", "outlet"]}
    out, calls = _run(vr, sdk, intent=None, contract=contract)
    assert out["ok"] and out["intent_source"] == "declare_intent", out
    text = sdk.requests[0]["messages"][0]["content"][-1]["text"]
    assert "watertight" in text and "inlet, outlet" in text
    assert calls == {"render": 1, "intent": 1}


def test_no_intent_anywhere_is_an_error_without_a_call():
    vr, sdk = _vr(), _fake_sdk()
    out, calls = _run(vr, sdk, intent="  ", contract={})
    assert out["ok"] is False and out["error"]["type"] == "no_intent", out
    assert sdk.constructed == 0 and calls["render"] == 0


def test_refusal_and_max_tokens_are_errors():
    details = types.SimpleNamespace(category="cyber", explanation="declined")
    vr = _vr()
    out, _ = _run(vr, _fake_sdk(_Resp(text="", stop_reason="refusal", stop_details=details)))
    assert out["ok"] is False and out["error"]["type"] == "refusal", out
    assert out["error"]["category"] == "cyber" and "verdict" not in out
    assert out["usage"]["input_tokens"] == 1234 and out["request_id"] == "req_test"
    out, _ = _run(_vr(), _fake_sdk(_Resp(text='{"matches_intent": tr', stop_reason="max_tokens")))
    assert out["ok"] is False and out["error"]["type"] == "max_tokens", out


def test_unparseable_or_incomplete_verdict_is_a_parse_error():
    for text in ("not json", json.dumps({"matches_intent": True})):
        out, _ = _run(_vr(), _fake_sdk(_Resp(text=text)))
        assert out["ok"] is False and out["error"]["type"] == "parse", out


def test_sdk_errors_map_most_specific_first():
    cases = [
        (lambda s: s.AuthenticationError("bad key", 401), "authentication", 401),
        (lambda s: s.RateLimitError("slow down", 429), "rate_limit", 429),
        (lambda s: s.APIStatusError("overloaded", 529), "api_status", 529),
        (lambda s: s.APIConnectionError("dns"), "connection", None),
    ]
    for make, kind, status in cases:
        out, _ = _run(_vr(), _fake_sdk(raises=make))
        assert out["enabled"] is True and out["ok"] is False, out
        assert out["error"]["type"] == kind, (kind, out)
        assert out["error"].get("status_code") == status, (kind, out)
        assert "test-key-not-real" not in json.dumps(out), "credential leaked into a result"


# --- the real SDK, through a mock transport (skips without anthropic) -------------

def test_real_sdk_request_and_errors_via_mock_transport():
    if importlib.util.find_spec("anthropic") is None:
        print("    SKIP: anthropic not installed (pip install 'ankusdrive[review]')")
        return
    import anthropic
    try:
        import httpx2 as httpx
    except ImportError:
        import httpx
    seen = []

    def handler(status, body):
        def h(request):
            seen.append(json.loads(request.content))
            return httpx.Response(status, json=body, headers={"request-id": "req_mock"})
        return h

    ok_body = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
               "content": [{"type": "text", "text": json.dumps(VERDICT)}],
               "stop_reason": "end_turn", "stop_sequence": None,
               "usage": {"input_tokens": 10, "output_tokens": 20}}
    err_body = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}

    def shim(status, body):
        def make_client():
            return anthropic.Anthropic(api_key="test-key-not-real", max_retries=0,
                                       http_client=httpx.Client(transport=httpx.MockTransport(handler(status, body))))
        return types.SimpleNamespace(Anthropic=make_client, **{
            n: getattr(anthropic, n) for n in
            ("AuthenticationError", "RateLimitError", "APIStatusError", "APIConnectionError")})

    vr = _vr()
    out, _ = _run(vr, shim(200, ok_body))
    assert out["ok"] and out["verdict"] == VERDICT and out["request_id"] == "req_mock", out
    sent = seen[-1]
    assert sent["output_config"]["format"]["type"] == "json_schema", sent.keys()
    assert sent["model"] == "claude-opus-5-5" and "temperature" not in sent
    assert [b["type"] for b in sent["messages"][0]["content"]] == ["image"] * 4 + ["text"]
    out, _ = _run(_vr(), shim(401, err_body))
    assert out["error"]["type"] == "authentication" and out["error"]["status_code"] == 401, out
    out, _ = _run(_vr(), shim(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "x"}}))
    assert out["error"]["type"] == "rate_limit", out
    print(f"    (real SDK {anthropic.__version__})")


# --- registries ----------------------------------------------------------------------

def test_registries_and_privacy_note():
    spec = importlib.util.spec_from_file_location("ta_vr", PKG / "tool_annotations.py")
    ta = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ta)
    assert ta.hints_for("visual_review") == {"readOnlyHint": True, "openWorldHint": True}
    assert ta.OPEN_WORLD == {"visual_review"}
    spec = importlib.util.spec_from_file_location("ts_vr", PKG / "toolsets.py")
    ts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ts)
    assert ts.family_of("visual_review") == "rendering"
    assert "rendering" not in ts.BUNDLE_DEFAULT, "off by default in the desktop bundle"
    privacy = (REPO / "PRIVACY.md").read_text(encoding="utf-8")
    assert "visual_review" in privacy and "ANKUSDRIVE_VISUAL_REVIEW" in privacy
    py = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'review = [\n    "anthropic>=' in py
    server = (PKG / "mcp_server.py").read_text(encoding="utf-8")
    assert "import orchestration" not in server and "from orchestration" not in server
    src = (PKG / "visual_review.py").read_text(encoding="utf-8")
    assert "orchestration" not in src
    # stdlib-only at import: the SDK is imported lazily inside _load_sdk.
    top = [ln for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    assert all(ln.split()[1] in {"__future__", "base64", "importlib.util", "json", "logging", "os"}
               for ln in top), top


def _discover():
    g = globals()
    return [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]


def main():
    failures = []
    t_suite = time.time()
    tests = _discover()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:52s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:52s} ({time.time() - t0:.2f}s)")
    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()
