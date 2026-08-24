"""
Project / workspace container — make the MULTI_AGENT directory convention a
primitive (issue #143, the D1 work item of docs/DESIGN_HIERARCHY.md §2 Theme D).

`MULTI_AGENT.md` §3/§7 describe a directory layout — a `manifest.json` (the
assembly ICD), a `components/` directory of single-writer component files, a
`.dp_lib/` cache of generated standard/recipe parts, and a lockfile baseline —
but it is **unenforced convention**. There is no project container, no single-
source-of-truth master/skeleton slot, and no guard against broken cross-file
references (the chronic PDM failure mode). This module promotes that convention
to a first-class object:

  * a **project manifest** (`project.json`) — a manifest-of-manifests / item-
    registry root that names the layout's pieces and ties them together;
  * a **scaffolding** function (:func:`scaffold`) that lays out a well-formed
    project — dirs, registry, seed manifest, lockfile slot — in one call;
  * a **master/skeleton** single-source-of-truth slot — the lean interface-
    geometry master (datums + key dims only) that children subscribe to via the
    existing ``publish_interface`` (top-down / skeleton-driven design, the NX/Creo
    master-model pattern mapped onto AnkusDrive's publish/subscribe);
  * a **reference-integrity guard** (:func:`check_references`) — catch a broken
    cross-file reference (a moved/renamed/missing component file, a dangling item
    ref) *before* a merge, plus naming-convention checks.

Like ankusdrive/items.py and ankusdrive/manifest.py this is **pure Python** — no
FreeCAD, no LLM, no key — so any host (the reference coordinator, a CI script,
another AI tool) can scaffold/validate/resolve a project. The MCP handlers in
worker.py are thin append-only wrappers over the functions here.

--- The contract (project.json, schema "ankusdrive.project/1") -------------------

    {
      "schema": "ankusdrive.project/1",
      "name": "gearbox",                  # project id (naming-convention checked)
      "manifest": "manifest.json",        # the assembly ICD merge_assembly consumes
      "registry": "items.json",           # the C1 (#140) item registry sidecar
      "master": "skeleton",               # OPTIONAL: the component id that is the
                                          #   single-source-of-truth master/skeleton
      "components_dir": "components",      # where single-writer component files live
      "lib_dir": ".dp_lib",               # generated standard/recipe part cache
      "lockfile": "manifest.lock.json"    # change-detection baseline slot
    }

All paths are **relative to the project.json's directory** — the layout survives
being moved or renamed wholesale, which is the entire point of a container.

--- The master / skeleton slot (single source of truth) ------------------------

The ``master`` field names one component id in the assembly manifest as the
**skeleton**: a lean part carrying only interface geometry (datums, key dims),
which the other components mate against via ``publish_interface`` +
``merge_assembly`` mates. This is top-down design — publish/subscribe a control
structure, never entangle implementations with live cross-file expression links
(the lesson NX WAVE, Creo skeletons, and the §11.1 resolve step all converged on).
This module holds + validates the slot; the publish/subscribe itself is the
existing, unchanged ``publish_interface`` / mate machinery.

--- The item-ref seam (deferred from #140) -------------------------------------

A manifest component may name an **item** (C1 identity) instead of a bare file:

    { "item": "<item_id>" }

:func:`lower_item_refs` resolves each such component to a ``file`` component
(picking the item's CAD artifact from the registry), mirroring
``recipes.lower_manifest`` — the bridge that lets an item-ref flow through the
existing ``merge_assembly`` machinery unchanged. The matching minimal edit to
``worker._validate_manifest`` adds ``"item"`` to the component source-kind set so
an un-lowered item-ref manifest is itself structurally valid.
"""
import copy
import json
import os
import re

SCHEMA = "ankusdrive.project/1"

# Naming convention for project / component / item identifiers: an alphanumeric
# start, then alphanumerics, underscores, or hyphens. Catches the "two components
# named *Box* collide" / space-in-a-name class of bug (MULTI_AGENT.md §5) before a
# merge, the cheap way the rest of AnkusDrive catches a malformed contract at the door.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# The conventional layout (MULTI_AGENT.md §3). Scaffolding writes exactly this;
# validation checks a project's declared paths against these defaults' shape.
_DEFAULTS = {
    "manifest": "manifest.json",
    "registry": "items.json",
    "components_dir": "components",
    "lib_dir": ".dp_lib",
    "lockfile": "manifest.lock.json",
}

# The CAD artifact extensions an item-ref prefers when resolving to a single file
# merge_assembly can link (an item may map to several files — .FCStd is the one a
# component links; .step/.stl are exports).
_CAD_EXTS = (".fcstd",)


# --- the project manifest ----------------------------------------------------

def default_project(name, master=None, **overrides):
    """A well-formed, conventional project manifest dict for `name` (the in-memory
    object :func:`scaffold` writes). `master` optionally names the master/skeleton
    component id. `overrides` may replace any conventional path. Pure data — no I/O."""
    proj = {"schema": SCHEMA, "name": name}
    proj.update(_DEFAULTS)
    proj.update({k: v for k, v in overrides.items() if k in _DEFAULTS})
    if master is not None:
        proj["master"] = master
    return proj


def load_project(path):
    """Read a project.json into a dict. Raises on unreadable / non-JSON — a bad
    container must fail loudly, never resolve to silence (the house rule)."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --- validation --------------------------------------------------------------

def validate_project(project, base_dir=None):
    """Structural + naming + (when `base_dir` is given) on-disk validation of a
    project manifest. Returns a list of human-readable problems; empty == valid
    (mirrors items.validate_registry / worker._validate_manifest). An absent
    `schema` is accepted as unversioned for back-compat; a PRESENT one must match.
    With `base_dir` set, also checks the referenced manifest + registry exist and
    the convention directories are present — the "scaffold produced a layout that
    loads clean" gate."""
    problems = []
    if not isinstance(project, dict):
        return ["project must be a JSON object"]

    schema = project.get("schema")
    if schema is not None and schema != SCHEMA:
        problems.append(f"unknown schema {schema!r} (expected {SCHEMA!r} or none)")

    name = project.get("name")
    if not isinstance(name, str) or not name:
        problems.append("project name must be a non-empty string")
    elif not NAME_RE.match(name):
        problems.append(
            f"project name {name!r} violates the naming convention {NAME_RE.pattern}")

    for key in ("manifest", "registry", "components_dir", "lib_dir", "lockfile"):
        val = project.get(key, _DEFAULTS.get(key))
        if not isinstance(val, str) or not val:
            problems.append(f"project {key} must be a non-empty path string")

    master = project.get("master")
    if master is not None and (not isinstance(master, str) or not master):
        problems.append("project master must be a non-empty component-id string")

    if base_dir is not None:
        mani = project.get("manifest", _DEFAULTS["manifest"])
        reg = project.get("registry", _DEFAULTS["registry"])
        if isinstance(mani, str) and not os.path.exists(os.path.join(base_dir, mani)):
            problems.append(f"manifest {mani!r} does not exist in the project")
        if isinstance(reg, str) and not os.path.exists(os.path.join(base_dir, reg)):
            problems.append(f"registry {reg!r} does not exist in the project")
        cdir = project.get("components_dir", _DEFAULTS["components_dir"])
        if isinstance(cdir, str) and not os.path.isdir(os.path.join(base_dir, cdir)):
            problems.append(f"components_dir {cdir!r} is not a directory in the project")
        # The master slot, if named, must be a component of the assembly manifest.
        if master and isinstance(mani, str):
            mpath = os.path.join(base_dir, mani)
            if os.path.exists(mpath):
                with open(mpath, encoding="utf-8") as f:
                    man = json.load(f)
                if master not in (man.get("components") or {}):
                    problems.append(
                        f"master {master!r} is not a component of manifest {mani!r}")
    return problems


# --- the item-ref seam (deferred from #140) ----------------------------------

def _pick_cad_file(files):
    """Choose the single CAD artifact merge_assembly should link from an item's
    file list — the first .FCStd, else the first file. None for an empty list."""
    for fp in files:
        if isinstance(fp, str) and fp.lower().endswith(_CAD_EXTS):
            return fp
    return files[0] if files else None


def lower_item_refs(manifest, registry):
    """Return a deep copy of `manifest` with every item-reference component
    (``{"item": "<id>"}``) rewritten to the existing ``{"file": ...}`` form, the CAD
    artifact resolved from `registry` (the C1 items.json). The result is a standard
    manifest ``merge_assembly`` / ``validate_manifest`` accept unchanged — the bridge
    that lets an item-ref flow through the assembly machinery without editing it
    (mirrors recipes.lower_manifest). Carries an optional ``object``/``envelope`` over.
    Raises KeyError on a dangling item-ref (an unknown id) — a broken cross-reference
    fails loudly, never a silent resolve. Components already in file/manifest/library
    form pass through untouched."""
    from ankusdrive import items as _items
    out = copy.deepcopy(manifest)
    comps = out.get("components")
    if not isinstance(comps, dict):
        return out
    for cid, spec in list(comps.items()):
        if not (isinstance(spec, dict) and "item" in spec):
            continue
        files = _items.resolve_item_ref(registry, {"item": spec["item"]})
        cad = _pick_cad_file(files)
        if cad is None:
            raise ValueError(
                f"component {cid!r} item {spec['item']!r} has no file to link")
        lowered = {"file": cad}
        for carry in ("object", "envelope"):
            if carry in spec:
                lowered[carry] = spec[carry]
        comps[cid] = lowered
    return out


# --- the reference-integrity guard -------------------------------------------

def check_references(manifest, registry=None, base_dir=None):
    """The reference-integrity guard: catch a broken cross-file reference *before* a
    merge — the chronic PDM failure mode. Walks an assembly `manifest` and returns a
    list of problems (empty == every reference is live):

      * a ``file`` / ``manifest`` component whose path is missing on disk (a moved,
        renamed, or never-built component) — checked when `base_dir` is given;
      * an ``item`` component that dangles against `registry` (an unknown item id),
        or whose resolved CAD artifact is missing on disk;
      * an instance referencing an unknown component;
      * a component id (or item id) that violates the naming convention.

    This is the guard that makes the "a part is its filename" fragility go away: a
    broken reference is reported loudly here instead of surfacing as a cryptic merge
    failure (or, worse, a silently-wrong assembly)."""
    from ankusdrive import items as _items
    problems = []
    comps = manifest.get("components")
    if not isinstance(comps, dict) or not comps:
        return ["manifest components must be a non-empty object"]

    for cid, spec in comps.items():
        if not NAME_RE.match(cid):
            problems.append(
                f"component id {cid!r} violates the naming convention "
                f"{NAME_RE.pattern}")
        if not isinstance(spec, dict):
            problems.append(f"component {cid!r} must be an object")
            continue

        if "item" in spec:
            item_id = spec["item"]
            if not isinstance(item_id, str) or not NAME_RE.match(item_id):
                problems.append(
                    f"component {cid!r} item id {item_id!r} violates the naming "
                    f"convention {NAME_RE.pattern}")
            if registry is None:
                problems.append(
                    f"component {cid!r} is an item-ref but no registry was supplied")
                continue
            try:
                files = _items.resolve_item_ref(registry, {"item": item_id})
            except (KeyError, ValueError) as e:
                problems.append(f"component {cid!r}: {e}")
                continue
            cad = _pick_cad_file(files)
            if cad is None:
                problems.append(
                    f"component {cid!r} item {item_id!r} maps to no file")
            elif base_dir is not None and not os.path.exists(
                    os.path.join(base_dir, cad)):
                problems.append(
                    f"component {cid!r} item {item_id!r} -> file {cad!r} is missing "
                    f"on disk (moved or never built)")
        elif "file" in spec:
            fp = spec["file"]
            if base_dir is not None and not os.path.exists(
                    os.path.join(base_dir, fp)):
                problems.append(
                    f"component {cid!r} file {fp!r} is missing on disk (moved, "
                    f"renamed, or never built)")
        elif "manifest" in spec:
            mp = spec["manifest"]
            if base_dir is not None and not os.path.exists(
                    os.path.join(base_dir, mp)):
                problems.append(
                    f"component {cid!r} subassembly manifest {mp!r} is missing on disk")
        # a "library" component is generated on the fly — nothing to dereference.

    insts = manifest.get("instances")
    if isinstance(insts, list):
        for i, inst in enumerate(insts):
            if isinstance(inst, dict) and inst.get("component") not in comps:
                problems.append(
                    f"instance {i} references unknown component "
                    f"{inst.get('component')!r}")
    return problems


def check_project_references(project, base_dir):
    """Run the :func:`check_references` guard for a *project* — loads the project's
    assembly manifest and item registry from disk and checks every cross-file
    reference resolves. Returns a list of problems (empty == clean). The pre-merge
    integrity gate a coordinator runs before fanning a manifest out to ``merge_assembly``."""
    from ankusdrive import items as _items
    problems = validate_project(project, base_dir)
    mani = project.get("manifest", _DEFAULTS["manifest"])
    mpath = os.path.join(base_dir, mani)
    if not os.path.exists(mpath):
        return problems + [f"manifest {mani!r} does not exist in the project"]
    with open(mpath, encoding="utf-8") as f:
        manifest = json.load(f)
    reg = None
    reg_rel = project.get("registry", _DEFAULTS["registry"])
    reg_path = os.path.join(base_dir, reg_rel)
    if os.path.exists(reg_path):
        reg = _items.load_registry(reg_path)
    return problems + check_references(manifest, reg, base_dir)


# --- scaffolding -------------------------------------------------------------

def scaffold(base_dir, name, components=None, instances=None,
             shared_parameters=None, master=None, items=None,
             part_number_format=None, **overrides):
    """Lay out a well-formed project under `base_dir` in one call — the D1 scaffolding
    primitive. Creates the convention directories (``components/``, ``.dp_lib/``),
    writes an empty (or `items`-seeded) item registry, a seed assembly manifest from
    the supplied `components`/`instances`/`shared_parameters`, and the project.json
    container. The result loads clean (:func:`validate_project`) and, when its
    components resolve (library specs, or files the caller then builds), is consumed
    by ``merge_assembly`` unchanged.

    base_dir:   the project root directory (created if absent).
    name:       the project id (naming-convention checked).
    components: optional assembly manifest ``components`` object.
    instances:  optional assembly manifest ``instances`` list.
    master:     optional component id to record as the master/skeleton slot.
    items:      optional {item_id: {files?, metadata?}} to seed the registry with
                (each gets a sequential part number).
    Returns a layout dict {project_file, manifest, registry, components_dir, lib_dir,
    lockfile, master, dirs}."""
    from ankusdrive import items as _items
    proj = default_project(name, master=master, **overrides)
    os.makedirs(base_dir, exist_ok=True)
    cdir = os.path.join(base_dir, proj["components_dir"])
    ldir = os.path.join(base_dir, proj["lib_dir"])
    os.makedirs(cdir, exist_ok=True)
    os.makedirs(ldir, exist_ok=True)

    # item registry (C1) — empty, or seeded with the supplied items
    reg = _items.empty_registry(part_number_format)
    for item_id, rec in (items or {}).items():
        _items.new_item(reg, item_id, files=(rec or {}).get("files"),
                        metadata=(rec or {}).get("metadata"))
    reg_path = os.path.join(base_dir, proj["registry"])
    with open(reg_path, "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=2, sort_keys=True)

    # seed assembly manifest (the ICD merge_assembly consumes)
    manifest = {
        "schema": "ankusdrive.manifest/1",
        "name": name,
        "root": name + ".FCStd",
        "components": components or {},
        "instances": instances or [],
    }
    if shared_parameters:
        manifest["shared_parameters"] = shared_parameters
    mani_path = os.path.join(base_dir, proj["manifest"])
    with open(mani_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    proj_path = os.path.join(base_dir, "project.json")
    with open(proj_path, "w", encoding="utf-8") as f:
        json.dump(proj, f, indent=2, sort_keys=True)

    return {
        "project_file": proj_path,
        "manifest": mani_path,
        "registry": reg_path,
        "components_dir": cdir,
        "lib_dir": ldir,
        "lockfile": os.path.join(base_dir, proj["lockfile"]),
        "master": master,
        "dirs": [cdir, ldir],
    }
