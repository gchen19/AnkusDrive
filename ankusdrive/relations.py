"""
Relations — driving vs driven parameters over the manifest (issue #137).

THE PROBLEM (RFC docs/DESIGN_HIERARCHY.md §2.1 A2). The resolve step
(:mod:`ankusdrive.manifest`) only does sum-to-target on an optional manufacturing
grid (MULTI_AGENT.md §11.1). There is no general way to *drive* a dimension from a
formula of master parameters (``pitch_d = module * teeth``), and no **driving vs
driven** distinction — so nothing stops a value being set two ways and silently
disagreeing.

THE LAYER. A small, *deterministic* relation layer — strictly **arithmetic +
table lookup, no iterative/geometric solve** (the RFC §5 non-goal / §13 line). It
adds, alongside the existing ``constraints`` block:

    {
      "parameters": { "module": 2.0, "teeth": 24, "pinion_teeth": 12 },
      "tables":     { "bore": { "24": 10.0, "12": 6.0 } },
      "relations":  {
        "pitch_d":         "module * teeth",
        "pinion_pitch_d":  "module * pinion_teeth",
        "center_distance": "(pitch_d + pinion_pitch_d) / 2",
        "gearA.bore_mm":   "lookup('bore', teeth)",
        "gearA.pitch_mm":  "pitch_d"
      },
      "components": { "gearA": { "file": "gearA.FCStd",
                                 "parameters": { ... } } }
    }

* ``parameters``  — top-level **driving** master params (free literal inputs).
* ``tables``      — named lookup tables for ``lookup(name, key)``.
* ``relations``   — ``target -> formula``; the target is a **driven** value
  (read-only output). A target is either a bare master name (an intermediate
  derived param) or a ``<component>.<param>`` ref that writes a literal value
  straight into that component's slice.

THE DRIVING / DRIVEN RULE (Creo's *"table OR relation, never both"*). A value
driven two ways is a **loud error**:

* a relation target that is also a top-level ``parameters`` literal, and
* a relation target ``comp.param`` where the component already declares that
  parameter as a literal (the table/row value).

A reference to an unknown symbol, and a dependency **cycle**, are likewise loud —
the whole contract fails BEFORE any builder is billed, the same discipline as the
existing resolver.

PURE PYTHON. No FreeCAD, no LLM — any host can run it on the shared manifest, and
the tests run free and fast. Arithmetic is evaluated over a whitelisted AST (never
``eval``): ``+ - * / // % **`` with unary sign and parentheses, plus the functions
``lookup``, ``min``, ``max``, ``abs`` and ``round``. Anything else is rejected.

:func:`resolve_relations` returns a deep-copied manifest with driven ``comp.param``
values written into the component slices and a ``relations_resolved`` audit block
mapping every target to its literal value.
"""
import ast
import copy

_ALLOWED_FUNCS = {"lookup", "min", "max", "abs", "round"}

_NUM = (int, float)


def _is_number(v):
    return isinstance(v, _NUM) and not isinstance(v, bool)


def _dotted(node):
    """Return 'a.b' for an Attribute over a Name (a component-parameter ref),
    else None. Only a single level of dotting is meaningful here."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return None


def _refs_in(node, target):
    """Yield every symbol name referenced by an expression AST — bare names and
    one-level dotted ``comp.param`` refs — excluding function-call callees."""
    if isinstance(node, ast.Call):
        # the callee (a Name) is a function, not a symbol; its args ARE symbols
        for a in node.args:
            yield from _refs_in(a, target)
        for kw in node.keywords:
            yield from _refs_in(kw.value, target)
        return
    dotted = _dotted(node)
    if dotted is not None:
        yield dotted
        return
    if isinstance(node, ast.Name):
        yield node.id
        return
    for child in ast.iter_child_nodes(node):
        yield from _refs_in(child, target)


class _Evaluator:
    """Whitelisted arithmetic + table-lookup evaluator over a symbol env."""

    def __init__(self, env, tables, target):
        self.env = env
        self.tables = tables
        self.target = target

    def _err(self, msg):
        return ValueError(f"relation {self.target!r}: {msg}")

    def eval(self, node):
        m = getattr(self, f"_e_{type(node).__name__}", None)
        if m is None:
            raise self._err(f"unsupported expression element {type(node).__name__}")
        return m(node)

    def _e_Expression(self, n):
        return self.eval(n.body)

    def _e_Constant(self, n):
        if not _is_number(n.value):
            raise self._err(f"non-numeric constant {n.value!r}")
        return n.value

    def _e_Name(self, n):
        if n.id not in self.env:
            raise self._err(f"unknown reference {n.id!r}")
        return self.env[n.id]

    def _e_Attribute(self, n):
        dotted = _dotted(n)
        if dotted is None or dotted not in self.env:
            raise self._err(f"unknown reference {dotted or '<expr>.attr'!r}")
        return self.env[dotted]

    def _e_UnaryOp(self, n):
        v = self.eval(n.operand)
        if isinstance(n.op, ast.UAdd):
            return +v
        if isinstance(n.op, ast.USub):
            return -v
        raise self._err(f"unsupported unary op {type(n.op).__name__}")

    def _e_BinOp(self, n):
        a, b = self.eval(n.left), self.eval(n.right)
        op = type(n.op)
        try:
            if op is ast.Add:
                return a + b
            if op is ast.Sub:
                return a - b
            if op is ast.Mult:
                return a * b
            if op is ast.Div:
                return a / b
            if op is ast.FloorDiv:
                return a // b
            if op is ast.Mod:
                return a % b
            if op is ast.Pow:
                return a ** b
        except ZeroDivisionError:
            raise self._err("division by zero (infeasible)")
        raise self._err(f"unsupported binary op {op.__name__}")

    def _e_Call(self, n):
        if not isinstance(n.func, ast.Name) or n.func.id not in _ALLOWED_FUNCS:
            name = getattr(n.func, "id", "<expr>")
            raise self._err(f"call to non-whitelisted function {name!r}")
        fn = n.func.id
        if fn == "lookup":
            # arg[0] is the table NAME (a bare string literal, never evaluated as
            # a value); arg[1] is the key expression.
            if len(n.args) != 2:
                raise self._err("lookup(table, key) takes exactly 2 args")
            tname = _string_arg(n.args[0])
            if tname is None:
                raise self._err("lookup() table name must be a string literal")
            return self._lookup(tname, self.eval(n.args[1]))
        args = [self.eval(a) for a in n.args]
        if fn == "min":
            return min(args)
        if fn == "max":
            return max(args)
        if fn == "abs":
            return abs(args[0])
        if fn == "round":
            return round(*args)
        raise self._err(f"unsupported function {fn!r}")  # unreachable

    def _lookup(self, tname, key):
        if tname not in self.tables:
            raise self._err(f"lookup of unknown table {tname!r}")
        table = self.tables[tname]
        # JSON object keys are strings; normalize the (possibly numeric) key so
        # lookup('bore', 24) finds the "24" row and lookup('bore', 24.0) too.
        cands = [key, str(key)]
        if _is_number(key) and float(key).is_integer():
            cands.append(str(int(key)))
        for cand in cands:
            if cand in table:
                v = table[cand]
                if not _is_number(v):
                    raise self._err(f"table {tname!r} value for {cand!r} not numeric")
                return v
        raise self._err(f"lookup miss: key {key!r} not in table {tname!r} (infeasible)")

    # string constants are only allowed as the lookup() table-name argument
    def _e_Str(self, n):  # py<3.8 compatibility; ast.Str deprecated but harmless
        return n.s


def _string_arg(node):
    """A bare string literal used as a lookup() table name."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Str):  # pragma: no cover - legacy
        return node.s
    return None


def resolve_relations(manifest):
    """Evaluate ``manifest['relations']`` over ``manifest['parameters']`` (and
    ``tables``), returning a deep-copied manifest whose driven ``comp.param``
    targets are literal values and with a ``relations_resolved`` audit block.

    Back-compatible: a manifest with neither ``relations`` nor ``parameters`` is
    returned as an untouched deep copy. Raises ``ValueError`` loudly on a
    double-driven value, an unknown reference, a dependency cycle, or an
    infeasible expression — the whole contract fails before any fan-out."""
    out = copy.deepcopy(manifest)
    relations = out.get("relations") or {}
    parameters = out.get("parameters") or {}
    tables = out.get("tables") or {}
    comps = out.get("components") or {}

    if not relations and not parameters:
        return out

    if not isinstance(relations, dict):
        raise ValueError("'relations' must be an object of target -> formula")
    if not isinstance(parameters, dict):
        raise ValueError("'parameters' must be an object of name -> number")

    # --- driving symbols ------------------------------------------------------
    # top-level master parameters (free literal inputs) ...
    env = {}
    for name, v in parameters.items():
        if not _is_number(v):
            raise ValueError(f"parameter {name!r} must be a number (got {v!r})")
        env[name] = v
    # ... plus component parameters declared as literals (driving INPUTS that a
    # formula may read; a relation must NOT also target them — that is the
    # double-driven / "table OR relation" error, checked below).
    comp_literals = set()
    for cid, spec in comps.items():
        if not isinstance(spec, dict):
            continue
        for pname, v in (spec.get("parameters") or {}).items():
            if _is_number(v):
                full = f"{cid}.{pname}"
                env[full] = v
                comp_literals.add(full)

    # --- driven symbols + the driving/driven (double-driven) guard -----------
    targets = list(relations)
    target_set = set(targets)
    for tgt in targets:
        if not isinstance(relations[tgt], str):
            raise ValueError(f"relation {tgt!r}: formula must be a string "
                             f"(got {relations[tgt]!r})")
        if tgt in parameters:
            raise ValueError(
                f"relation {tgt!r}: value is driven by BOTH a parameter literal "
                f"and a relation — table OR relation, never both")
        if "." in tgt:
            cid, _, pname = tgt.partition(".")
            if cid not in comps:
                raise ValueError(f"relation {tgt!r}: unknown component {cid!r}")
            if tgt in comp_literals:
                raise ValueError(
                    f"relation {tgt!r}: parameter is driven by BOTH a component "
                    f"literal (table/row value) and a relation — table OR "
                    f"relation, never both")

    known = set(env) | target_set

    # --- parse, validate refs, build the dependency DAG ----------------------
    asts, deps = {}, {}
    for tgt in targets:
        try:
            tree = ast.parse(relations[tgt], mode="eval")
        except SyntaxError as e:
            raise ValueError(f"relation {tgt!r}: cannot parse formula ({e.msg})")
        asts[tgt] = tree
        edges = set()
        for ref in set(_refs_in(tree.body, tgt)):
            # a lookup() table-name string literal surfaces as a bare Name in
            # older grammars; skip names that are actually table names handled
            # by the evaluator. A genuine unknown reference is loud.
            if ref in known:
                if ref in target_set:
                    edges.add(ref)
            elif ref not in tables:
                raise ValueError(
                    f"relation {tgt!r}: unknown reference {ref!r} "
                    f"(not a parameter, component parameter, or relation)")
        deps[tgt] = edges

    # --- topological order (Kahn, name-sorted = deterministic) ---------------
    order = _toposort(targets, deps)

    # --- evaluate in order ----------------------------------------------------
    audit = {}
    for tgt in order:
        ev = _Evaluator(env, tables, tgt)
        # surface lookup() string table-name args (which the AST gives as a
        # Constant str) without exposing strings as general values.
        value = ev.eval(asts[tgt].body)
        if not _is_number(value):
            raise ValueError(f"relation {tgt!r}: result is not a number ({value!r})")
        env[tgt] = value
        audit[tgt] = value

    # --- write driven comp.param values into the slices ----------------------
    for tgt, value in audit.items():
        if "." in tgt:
            cid, _, pname = tgt.partition(".")
            comps[cid].setdefault("parameters", {})[pname] = value

    out["relations_resolved"] = audit
    return out


def _toposort(targets, deps):
    """Kahn's algorithm over the driven targets; raises ValueError naming the
    members of a cycle. Name-sorted ready-set keeps the order deterministic."""
    indeg = {t: 0 for t in targets}
    for t in targets:
        for d in deps[t]:
            indeg[t] += 1  # t depends on d -> edge d -> t
    ready = sorted(t for t in targets if indeg[t] == 0)
    order = []
    while ready:
        t = ready.pop(0)
        order.append(t)
        for u in targets:
            if t in deps[u]:
                indeg[u] -= 1
                if indeg[u] == 0:
                    ready.append(u)
                    ready.sort()
    if len(order) != len(targets):
        stuck = sorted(t for t in targets if t not in set(order))
        raise ValueError(
            f"relations form a dependency cycle among {stuck} — the DAG must be "
            f"acyclic (arithmetic + lookup only, no iterative solve)")
    return order
