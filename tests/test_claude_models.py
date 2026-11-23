"""Offline tests for orchestration/claude_models.py (issue #506). No API key, no FreeCAD.

    python tests/test_claude_models.py
"""
import os
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestration import claude_models as cm  # noqa: E402


def _blk(**kw):
    return types.SimpleNamespace(**kw)


def test_aliases_point_at_priced_models():
    for alias, mid in cm.MODELS.items():
        assert mid in cm.PRICING, f"alias {alias} -> {mid} has no pricing row"


def test_resolve_precedence(monkeypatch_env):
    monkeypatch_env("ANKUSDRIVE_EVAL_MODEL", None)
    assert cm.resolve_model() == cm.MODELS["opus"]
    assert cm.resolve_model(default="haiku") == cm.MODELS["haiku"]
    monkeypatch_env("ANKUSDRIVE_EVAL_MODEL", "sonnet")
    assert cm.resolve_model(default="haiku") == cm.MODELS["sonnet"]
    assert cm.resolve_model("claude-haiku-4-5") == "claude-haiku-4-5"  # explicit wins
    try:
        cm.resolve_model("gpt")
    except ValueError as e:
        assert "opus" in str(e)
    else:
        raise AssertionError("unknown alias must raise")


def test_cost_matches_list_price():
    # 1M of each bucket on Opus 4.8 = 5 + 6.25 + 0.5 + 25 — NOT the old 15/75 row
    one_m = {"in_tokens": 1e6, "cache_write": 1e6, "cache_read": 1e6, "out_tokens": 1e6}
    assert abs(cm.cost_of(one_m, "claude-opus-4-8") - 36.75) < 1e-9
    assert abs(cm.cost_of({"out_tokens": 1e6}, "claude-opus-5-5") - 20.0) < 1e-9
    try:
        cm.cost_of(one_m, "claude-unknown")
    except KeyError as e:
        assert "PRICING" in str(e)
    else:
        raise AssertionError("unpriced model must raise")


def test_first_text_skips_thinking():
    resp = _blk(content=[_blk(type="thinking", thinking=""),
                         _blk(type="text", text="a cube "), _blk(type="text", text="with a hole")])
    assert cm.first_text(resp) == "a cube with a hole"
    assert cm.first_text(_blk(content=[_blk(type="tool_use", name="x")])) == ""


def test_tool_choice_fallback():
    assert cm.tool_choice_for("claude-opus-5-5", "t") == {"type": "auto"}
    assert cm.tool_choice_for("claude-sonnet-5-5", "t") == {"type": "auto"}
    assert cm.tool_choice_for("claude-haiku-5-5", "t") == {"type": "tool", "name": "t"}
    assert cm.tool_choice_for("claude-opus-4-8", "t") == {"type": "tool", "name": "t"}


def test_usage_dict_tolerates_missing_cache_fields():
    u = cm.usage_dict(_blk(usage=_blk(input_tokens=3, output_tokens=4)))
    assert u == {"in_tokens": 3, "out_tokens": 4, "cache_read": 0, "cache_write": 0}


def test_response_cache_name_is_model_keyed():
    a = cm.response_cache_name("cube_iso", "claude-sonnet-5-5")
    b = cm.response_cache_name("cube_iso", "claude-opus-5-5")
    assert a != b and a.endswith(".txt") and "/" not in a


def _env_patcher():
    saved = {}

    def setenv(k, v):
        saved.setdefault(k, os.environ.get(k))
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    def restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return setenv, restore


def main():
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        setenv, restore = _env_patcher()
        try:
            fn(setenv) if fn.__code__.co_argcount else fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name}: {e}")
        finally:
            restore()
    print(f"{'FAILED' if fails else 'OK'}: claude_models")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
