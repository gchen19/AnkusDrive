"""
Free self-checks for the orchestration layer (no API).

  python -m orchestration.selftest

Validates the pure / stub-drivable pieces — validate_brief and the decompose
forced-tool wiring — so a malformed decomposition is caught here, not halfway
through a billed run. The end-to-end coordinator loop has its own free proof in
orchestration.dryrun.
"""
import sys
import types

from . import coordinator as C


def _brief(components, instances, name="x"):
    return {"name": name, "components": components, "instances": instances}


_GOOD = _brief(
    {"plate": {"file": "plate.FCStd", "task": "build a plate; save_component"},
     "peg": {"file": "peg.FCStd", "task": "build a peg; save_component"}},
    [{"component": "plate", "placement": [0, 0, 0]},
     {"component": "peg", "placement": [30, 30, -5]}],
)

_NEG = {
    "unknown component": _brief(
        _GOOD["components"],
        _GOOD["instances"] + [{"component": "ghost", "placement": [0, 0, 0]}]),
    "duplicate file": _brief(
        {"a": {"file": "x.FCStd", "task": "t; save_component"},
         "b": {"file": "x.FCStd", "task": "t; save_component"}},
        [{"component": "a", "placement": [0, 0, 0]},
         {"component": "b", "placement": [0, 0, 0]}]),
    "empty task": _brief(
        {"plate": {"file": "p.FCStd", "task": "   "},
         "peg": {"file": "peg.FCStd", "task": "t; save_component"}},
        _GOOD["instances"]),
    "unplaced component": _brief(
        _GOOD["components"], [{"component": "plate", "placement": [0, 0, 0]}]),
    "empty decomposition": _brief({}, []),
}


# --- a stub client that returns a forced emit_brief tool call -----------------

class _Usage:
    input_tokens = 50
    output_tokens = 80
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _ToolBlock:
    type = "tool_use"

    def __init__(self, inp):
        self.name = "emit_brief"
        self.input = inp


class _StubClient:
    def __init__(self, brief):
        self.messages = self
        self._brief = brief

    def create(self, **kw):
        assert kw["tool_choice"]["name"] == "emit_brief", "decompose must force the tool"
        return types.SimpleNamespace(content=[_ToolBlock(self._brief)], usage=_Usage())


def main():
    fails = 0

    if C.validate_brief(_GOOD) == []:
        print("  PASS  validate_brief: clean brief accepted")
    else:
        print(f"  FAIL  validate_brief: clean brief rejected: {C.validate_brief(_GOOD)}")
        fails += 1

    for label, brief in _NEG.items():
        probs = C.validate_brief(brief)
        if probs:
            print(f"  PASS  validate_brief: caught {label} -> {probs[0]}")
        else:
            print(f"  FAIL  validate_brief: missed {label}")
            fails += 1

    # decompose returns the (validated) brief the model emitted
    brief, usage = C.decompose(_StubClient(_GOOD), "m", "build a peg and a plate",
                               log=lambda *a: None)
    if brief == _GOOD and usage["in_tokens"] == 50:
        print("  PASS  decompose: forced-tool path returns a validated brief")
    else:
        print("  FAIL  decompose: did not return the emitted brief")
        fails += 1

    # decompose refuses to pass through an invalid brief
    try:
        C.decompose(_StubClient(_NEG["unknown component"]), "m", "spec",
                    log=lambda *a: None)
        print("  FAIL  decompose: passed through an invalid brief")
        fails += 1
    except ValueError:
        print("  PASS  decompose: rejects an invalid emitted brief")

    print("\n== orchestration self-checks OK ==" if not fails
          else f"\n== {fails} self-check(s) FAILED ==")
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    main()
