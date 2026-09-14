"""Every MCP tool's display title and behaviour hints, in one place (#368).

MCP clients use ``ToolAnnotations`` to decide what may run without asking: a tool
marked read-only can be auto-approved; one marked destructive should be confirmed.
Anthropic's desktop-extension directory requires every tool to carry a ``title``
and the applicable ``readOnlyHint`` / ``destructiveHint``. With 281 tools that is a
registry, not 281 decorator edits, and ``tests/test_tool_annotations.py`` holds the
registry to the server: every tool is in exactly one class below, and none is
unclassified — so tool 282 cannot ship without a decision.

The three classes, and the rule each one follows:

* ``READ_ONLY`` — ``readOnlyHint: true``. Changes nothing the user owns: no document,
  object, property, handle, workspace, job, or file outside a private temp dir.
  Measurements, inspections, gates, analytical calculators, catalog / registry
  reads, capability and status reports. A ``recompute()`` to read fresh geometry, or
  a temp document / temp file created and removed before returning, still counts.

* ``ADDITIVE`` — ``readOnlyHint: false, destructiveHint: false``. Creates new state
  and never changes or removes existing state: new documents, objects, features,
  sketches, drawing views, handles, workspaces, background jobs, or files written
  only into private temp dirs. Opening a document or switching the active one.

* ``DESTRUCTIVE`` — ``readOnlyHint: false, destructiveHint: true``. MAY change or
  remove existing state, even if a typical call does not:
    - edits existing objects or properties (placement, visibility, solver settings,
      drawing view positions, title block);
    - replaces a stored record on a part (intent, performance spec/verdict,
      interface, face role, balloon numbering);
    - discards work (closing a document or workspace, aborting a transaction,
      restarting a worker, purging prior FEM results, dropping a render job);
    - writes to a caller-supplied path or directory that may already hold a file
      (exports, saves, reports, lockfiles, manifests, registries, run-in-place
      solver case dirs) — conservative: a tool that COULD overwrite is marked;
    - executes caller-supplied code (``run_script``).
  The classification is conservative on purpose: a false "read-only" lets a client
  run a mutation unasked, while a false "destructive" only costs a confirmation.

``openWorldHint`` is false for every tool: AnkusDrive makes no network requests
(no HTTP client, no telemetry). Local solver subprocesses, WSL and the Multipass VM
run on the user's own machine.

Titles are derived from the tool name (``fem_run`` -> "FEM Run") with the acronym
table below, plus explicit overrides where the name alone reads badly.
"""
from __future__ import annotations

READ_ONLY = frozenset("""
ping list_workspaces version list_objects list_documents get_object
measure_distance measure_angle bounding_box min_clearance mass_properties
check_shape check_airtight_path classify_face_sides list_faces list_edges query_faces
resolve_face resolve_edge verify_feature close_sketch list_assembly_parts
interference_check bom_extract standard_part_designate designation_check
catalog_search catalog_nearest catalog_check envelope_check list_face_roles
verify_intent verify_contract component_contract_check interface_align_check
validate_manifest assembly_lock_check get_interface
drawing_gate drawing_legibility inspection_plan
render_view render_views render_fem_results render_capabilities
solve_capabilities setup_status
fem_modal_results fem_buckling_results fem_thermal_results fem_results fem_result_probe
material_get material_select material_list fluid_props
bolted_joint_check bearing_life spring_check gear_rating belt_drive press_fit_stress
seal_check chain_drive weld_group tolerance_stackup fit_check fit_class gdt_check
fatigue_check fracture_check wear_estimate creep_flag h_estimate acoustic_screen
plate_check beam_buckling plastic_collapse elastica_deflection hertz_contact
laminate_properties monopole_sphere rigid_sphere_scattering waveguide_cutoff
dipole_resonance fsi_plate_deflection fsi_interface_balance fsi_channel_pressure
molding_screen moldability_screen drop_impact thermal_lumped thermal_transient_1d
thermal_composite_wall harmonic_response em_skin_depth em_dc_resistance em_field
cfd_pipe_flow grid_convergence cfd_body_drag random_vibration beam_modal
dfm_check dfa_check pack_check cost_estimate tolerance_cost_check suggest_loosening
cnc_machinability_check cnc_time_estimate slice_estimate mechanism_kinematics
optics_raytrace optics_lens_design optics_lens_optimize optics_solid_trace
optics_moldability_check moldability_check granular_screen sheet_check
job_status job_result job_list
recipe_list recipe_schema recipe_validate
items_validate items_resolve items_check_manifest
feature_list feature_schema feature_validate family_validate substitutability_check
lifecycle_editable lifecycle_classify_change
project_validate project_check_references where_used change_impact
eco_validate baseline_verify
""".split())

ADDITIVE = frozenset("""
use_workspace open_document set_active_document new_document register_handle
add_primitive add_gear add_rack add_sprocket add_pulley add_spring add_fastener
add_bearing oring_groove add_thread add_rib shell_solid scale_shape copy_shape
section_view chamfer_edges fillet_edges engrave_text boolean_op
make_body make_datum_plane make_sketch add_sketch_geometry add_sketch_constraint
add_sketch_external pad pocket revolve partdesign_fillet partdesign_chamfer hole
list_thread_options linear_pattern polar_pattern mirrored loft sweep helix
thickness draft transaction_open transaction_commit
make_assembly add_part merge_assembly
make_drawing_page add_projection_group add_dimension add_annotation add_feature_note
add_gdt_callout add_thumbnail add_section_view
render_photoreal render_photoreal_submit
fem_new_analysis fem_set_solver fem_set_material fem_add_constraint contact_setup
fem_set_nonlinear_material fem_mesh_refinement fem_mesh
acoustic_radiation_submit em_fullwave_submit fsi_pressure_plate_submit
em_conduction_submit em_induction_submit cfd_mesh_independence_submit
dem_pack_submit dem_flow_submit mechanism_simulate_submit topology_optimize_submit
topology_to_solid study_submit optimize_submit slice_gcode_submit async_demo_submit
recipe feature_instantiate items_new
sheet_base sheet_flange sheet_tab sheet_hem sheet_unfold sheet_refold
""".split())

# name -> why it may change or remove existing state (shown to reviewers, kept here
# so every destructive mark carries its reason).
DESTRUCTIVE = {
    "restart_worker": "kills the worker; open documents and unsaved changes in the workspace are lost",
    "close_workspace": "shuts a workspace down; its documents and unsaved changes are lost",
    "close_document": "closes a document, discarding unsaved changes",
    "transaction_abort": "rolls back the open transaction's edits",
    "save_document": "writes the .FCStd to a caller path, overwriting an existing file",
    "export_shape": "writes STEP/IGES/BREP/STL to a caller path, overwriting an existing file",
    "export_drawing": "writes PDF/SVG/DXF to a caller path, overwriting an existing file",
    "sheet_flat_export": "writes a DXF to a caller path, overwriting an existing file",
    "fai_report": "writes the report to a caller path, overwriting an existing file",
    "assembly_lock": "writes a lockfile to a caller path, overwriting an existing one",
    "project_resolve_manifest": "writes the resolved manifest to a caller path",
    "eco_create": "writes the change-order record to a caller path",
    "baseline_create": "writes the baseline record to a caller path",
    "release_package": "writes the release bundle into a caller directory",
    "scaffold_project": "writes project.json, the manifest and items.json, replacing existing ones",
    "family_materialize": "rewrites the item registry and materializes variant files",
    "lifecycle_transition": "changes a released item's lifecycle state in the registry",
    "lifecycle_apply_change": "applies a change to a released item and rewrites its record",
    "transform": "moves/rotates an existing object in place",
    "set_property": "changes a property on an existing object",
    "set_visibility": "changes an existing object's persistent visibility",
    "publish_interface": "records an interface frame on a part, replacing one of the same name",
    "annotate_face": "records a face role on a part, replacing an existing declaration",
    "declare_intent": "replaces the part's stored intent contract",
    "declare_performance": "replaces the part's stored performance spec",
    "verify_performance": "records the verdict on the part, replacing the previous one",
    "set_title_block": "overwrites the drawing's title-block fields",
    "fit_page": "moves existing drawing views on the sheet",
    "balloon_drawing": "persists balloon numbers on drawing items; renumber=True discards existing numbering",
    "fem_modal": "reconfigures the analysis's existing solver for a frequency analysis",
    "fem_buckling": "reconfigures the analysis's existing solver for a buckling analysis",
    "fem_run": "purges the analysis's previous results and writes solver files into the workdir",
    "fem_cantilever_demo": "writes solver files into a caller-supplied workdir",
    "render_job": "discard=True drops the finished render's result",
    "thermal_transient_submit": "runs the solver inside a caller-supplied case_dir, writing into it",
    "thermal_radiation_submit": "runs the solver inside a caller-supplied case_dir, writing into it",
    "cht_channel_submit": "runs the solver inside a caller-supplied case_dir, writing into it",
    "cht_graetz_submit": "runs the solver inside a caller-supplied case_dir, writing into it",
    "acoustic_fem_submit": "runs the solver inside a caller-supplied case_dir, writing into it",
    "harmonic_response_submit": "runs the solver inside a caller-supplied case_dir, writing into it",
    "em_induction_heating_submit": "runs the solver inside a caller-supplied case_dir, writing into it",
    "cfd_internal_flow_submit": "runs the solver inside a caller-supplied case_dir, writing into it",
    "cfd_external_flow_submit": "runs the solver inside a caller-supplied case_dir, writing into it",
    "molding_fill_submit": "runs the solver inside a caller-supplied case_dir, writing into it",
    "molding_warpage_submit": "runs the solver inside caller-supplied case dirs, writing into them",
    "run_script": "executes caller-supplied Python with full access to the documents and filesystem",
}

_ACRONYMS = {
    "fem": "FEM", "cfd": "CFD", "cht": "CHT", "em": "EM", "fsi": "FSI", "dem": "DEM",
    "dfm": "DFM", "dfa": "DFA", "cnc": "CNC", "bom": "BOM", "eco": "ECO", "fai": "FAI",
    "gdt": "GD&T", "dc": "DC", "id": "ID", "mcp": "MCP", "1d": "1D", "oring": "O-Ring",
    "partdesign": "PartDesign", "gcode": "G-code",
}
_SMALL = {"of", "to", "and", "or", "in", "on", "by", "for"}

TITLE_OVERRIDES = {
    "h_estimate": "Heat Transfer Coefficient Estimate",
    "fit_page": "Fit Drawing to Page",
    "list_thread_options": "List Hole Thread Options",
    "async_demo_submit": "Async Demo Job",
    "recipe": "Build Recipe",
    "run_script": "Run Python Script",
    "items_new": "New Item",
    "creep_flag": "Creep Risk Flag",
    "pack_check": "Molding Pack Check",
}


def title_for(name: str) -> str:
    if name in TITLE_OVERRIDES:
        return TITLE_OVERRIDES[name]
    words = name.split("_")
    out = []
    for i, w in enumerate(words):
        if w in _ACRONYMS:
            out.append(_ACRONYMS[w])
        elif i and w in _SMALL:
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


def hints_for(name: str) -> dict:
    """The ToolAnnotations fields for ``name``. Raises KeyError for an unclassified
    tool — never a silent default."""
    if name in READ_ONLY:
        return {"readOnlyHint": True, "openWorldHint": False}
    if name in ADDITIVE:
        return {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
    if name in DESTRUCTIVE:
        return {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}
    raise KeyError(f"tool {name!r} has no annotation class — add it to READ_ONLY, "
                   f"ADDITIVE or DESTRUCTIVE in ankusdrive/tool_annotations.py")


def apply(mcp) -> int:
    """Set ``title`` and ``annotations`` on every tool registered on ``mcp`` (a
    FastMCP). Returns the count. Raises on an unclassified tool, so a new tool
    without a decision fails at server import, not in a reviewer's hands."""
    from mcp.types import ToolAnnotations

    tools = mcp._tool_manager._tools
    for name, tool in tools.items():
        title = title_for(name)
        tool.title = title
        tool.annotations = ToolAnnotations(title=title, **hints_for(name))
    return len(tools)
