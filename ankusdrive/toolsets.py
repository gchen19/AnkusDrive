"""Tool families an install can switch on and off (#377).

Every registered tool costs the client context before the first call: all 281 come to
~456k characters of ``tools/list`` (~114k tokens). Anthropic's directory policy asks
MCP servers to be "frugal with their use of tokens", and most sessions use a fraction
of the surface — a CAD session never needs the external-solver submits, and a
simulation session never needs the PLM layer. So tools are grouped into families,
and only the enabled families are registered: a disabled family costs nothing.

Selection — ``ANKUSDRIVE_TOOLSETS`` (env, then ``toolsets`` in config.toml):

* **unset, or ``all``** — every family. This is the default, so a pip / pipx / uvx /
  clone install behaves exactly as before; nobody silently loses a tool.
* **a comma list** — e.g. ``drawings,fem,simulation``. ``core`` is always included.
* **an unknown family name raises** at server start, naming the valid ones — a typo
  must not quietly drop a whole family.

The Claude Desktop extension (``mcpb/``) sets it from install-dialog toggles whose
defaults are ``BUNDLE_DEFAULT`` — the lean model -> drawing -> analysis loop.

A disabled family is never invisible: ``setup_status`` / ``ankusdrive doctor`` and
``solve_capabilities`` report what is off and exactly how to turn it on, so an agent
asked for a CFD run can say which switch to flip instead of concluding the capability
does not exist.

``tests/test_toolsets.py`` holds every ``@mcp.tool()`` to exactly one family and the
bundle manifest's toggles to these families and defaults; ``tests/test_toolsets_budget.py``
measures the bundle default's real ``tools/list`` against a budget.
"""
from __future__ import annotations

ENV = "ANKUSDRIVE_TOOLSETS"
CORE = "core"

FAMILIES: dict = {
    "core": """
        ping version restart_worker use_workspace list_workspaces close_workspace session_transcript
        new_document open_document save_document list_documents set_active_document
        close_document list_objects get_object set_property set_visibility register_handle
        transaction_open transaction_commit transaction_abort run_script export_shape
        setup_status solve_capabilities job_status job_result job_list
        add_primitive boolean_op transform scale_shape copy_shape fillet_edges chamfer_edges
        shell_solid add_rib engrave_text list_faces list_edges query_faces resolve_face
        resolve_edge verify_feature make_body make_datum_plane make_sketch add_sketch_geometry
        add_sketch_constraint add_sketch_external close_sketch pad pocket revolve hole loft
        sweep helix thickness draft partdesign_fillet partdesign_chamfer linear_pattern
        polar_pattern mirrored measure_distance measure_angle bounding_box min_clearance
        check_shape section_view mass_properties envelope_check interference_check
        render_view render_views material_list material_get material_select
    """,
    "drawings": """
        make_drawing_page add_projection_group add_section_view add_thumbnail add_dimension
        add_annotation add_feature_note add_gdt_callout set_title_block drawing_gate fit_page
        drawing_legibility balloon_drawing inspection_plan fai_report export_drawing
    """,
    "fem": """
        fem_new_analysis fem_set_solver fem_set_material fem_set_nonlinear_material
        fem_add_constraint contact_setup fem_mesh fem_mesh_refinement fem_run fem_run_submit
        fem_results
        fem_result_probe fem_modal fem_modal_results fem_buckling fem_buckling_results
        fem_thermal_results fem_cantilever_demo render_fem_results
    """,
    "components": """
        add_gear add_rack add_sprocket add_pulley add_spring add_fastener add_bearing
        add_thread list_thread_options oring_groove catalog_search catalog_nearest
        catalog_check standard_part_designate designation_check
    """,
    "sheet_metal": """
        sheet_base sheet_flange sheet_tab sheet_hem sheet_unfold sheet_refold
        sheet_flat_export sheet_check
    """,
    "assembly": """
        make_assembly add_part list_assembly_parts merge_assembly publish_interface
        get_interface interface_align_check assembly_lock assembly_lock_check bom_extract
        component_contract_check verify_contract validate_manifest
    """,
    "intent": """
        annotate_face list_face_roles classify_face_sides check_airtight_path declare_intent
        verify_intent declare_performance verify_performance
    """,
    "manufacturing": """
        dfm_check dfa_check moldability_check optics_moldability_check pack_check
        cost_estimate slice_estimate cnc_machinability_check cnc_time_estimate
        tolerance_cost_check tolerance_stackup suggest_loosening fit_check fit_class gdt_check
    """,
    "hand_calcs": """
        fluid_props bolted_joint_check bearing_life spring_check gear_rating belt_drive
        press_fit_stress seal_check chain_drive weld_group fatigue_check fracture_check
        wear_estimate creep_flag h_estimate plate_check beam_buckling plastic_collapse
        elastica_deflection hertz_contact laminate_properties beam_modal drop_impact bar_impact
        random_vibration harmonic_response thermal_lumped thermal_transient_1d
        thermal_composite_wall em_skin_depth em_dc_resistance em_field waveguide_cutoff
        dipole_resonance monopole_sphere rigid_sphere_scattering acoustic_screen cfd_pipe_flow
        cfd_body_drag grid_convergence fsi_plate_deflection fsi_interface_balance
        fsi_channel_pressure molding_screen moldability_screen granular_screen
        mechanism_kinematics optics_raytrace optics_lens_design optics_lens_optimize
    """,
    "simulation": """
        thermal_transient_submit thermal_radiation_submit cht_channel_submit cht_graetz_submit
        acoustic_fem_submit acoustic_radiation_submit harmonic_response_submit
        em_conduction_submit em_induction_submit em_induction_heating_submit
        em_fullwave_submit cfd_internal_flow_submit cfd_external_flow_submit
        cfd_mesh_independence_submit fsi_pressure_plate_submit molding_fill_submit
        molding_warpage_submit impact_dynamics_submit dem_pack_submit dem_flow_submit mechanism_simulate_submit
        topology_optimize_submit topology_to_solid optics_solid_trace study_submit
        optimize_submit slice_gcode_submit async_demo_submit
    """,
    "plm": """
        items_new items_validate items_resolve items_check_manifest recipe recipe_list
        recipe_schema recipe_validate feature_list feature_schema feature_validate
        feature_instantiate family_validate family_materialize substitutability_check
        lifecycle_editable lifecycle_transition lifecycle_classify_change
        lifecycle_apply_change scaffold_project project_validate project_check_references
        project_resolve_manifest where_used change_impact eco_validate eco_create
        baseline_create baseline_verify release_package
    """,
    "rendering": "render_photoreal render_photoreal_submit render_job render_capabilities",
}
FAMILIES = {k: frozenset(v.split()) for k, v in FAMILIES.items()}

# What the install dialog and the reports call each family.
LABELS = {
    "core": "Core CAD modeling, measurement, documents and jobs",
    "drawings": "Technical drawings, GD&T and first-article inspection",
    "fem": "Structural FEM (CalculiX)",
    "components": "Standard components: gears, fasteners, bearings, threads, catalogs",
    "sheet_metal": "Sheet metal: flanges, hems, flat patterns",
    "assembly": "Assemblies, interfaces, lockfiles and BOMs",
    "intent": "Design intent and performance contracts",
    "manufacturing": "Design for manufacturing, cost, CNC, tolerances",
    "hand_calcs": "Engineering hand calculations and quick screens",
    "simulation": "External-solver simulation: CFD, thermal, EM, acoustics, FSI, molding, DEM, optimization",
    "plm": "Design control: part numbers, recipes, lifecycle, change orders, releases",
    "rendering": "Photorealistic rendering (Blender / Render add-on)",
}

# The Claude Desktop extension's toggle defaults: model -> drawing -> analysis.
BUNDLE_DEFAULT = ("core", "drawings", "fem")


def family_of(tool: str) -> str:
    for fam, tools in FAMILIES.items():
        if tool in tools:
            return fam
    raise KeyError(f"tool {tool!r} is in no toolset — add it to a family in ankusdrive/toolsets.py")


def selected(value: str | None) -> tuple:
    """The enabled families for a raw ``ANKUSDRIVE_TOOLSETS`` value, in FAMILIES order.
    Unset / blank / ``all`` -> every family. Raises ValueError on an unknown name."""
    raw = (value or "").strip()
    if not raw or raw.lower() == "all":
        return tuple(FAMILIES)
    names = {n.strip().lower() for n in raw.split(",") if n.strip()}
    unknown = sorted(names - set(FAMILIES) - {"all"})
    if unknown:
        raise ValueError(f"{ENV}: unknown toolset(s) {unknown}; valid: {', '.join(FAMILIES)} (or 'all')")
    if "all" in names:
        return tuple(FAMILIES)
    names.add(CORE)
    return tuple(f for f in FAMILIES if f in names)


def current() -> tuple:
    from ankusdrive import config as _config
    return selected(_config.get(ENV))


def enable_hint(family: str, enabled: tuple) -> str:
    """How to turn ``family`` on, for this install."""
    from ankusdrive import install_kind as _ik
    if _ik.kind() == "mcpb":
        return (f"In Claude Desktop: Settings -> Extensions -> AnkusDrive -> configure, turn on "
                f"'{LABELS[family]}', then restart the extension.")
    want = ",".join(f for f in FAMILIES if f in set(enabled) | {family})
    return (f"Set {ENV}={want} (or 'all') in the MCP server's environment, or "
            f"`toolsets = \"{want}\"` in config.toml, then restart the server.")


def report(enabled: tuple | None = None) -> dict:
    """``{enabled, disabled: {family: {label, tools, enable}}}`` for setup_status / doctor."""
    enabled = current() if enabled is None else enabled
    disabled = {f: {"label": LABELS[f], "tools": len(FAMILIES[f]), "enable": enable_hint(f, enabled)}
                for f in FAMILIES if f not in enabled}
    return {"enabled": list(enabled), "disabled": disabled}


def apply(mcp, enabled: tuple | None = None) -> dict:
    """Unregister every tool outside the enabled families from ``mcp`` (a FastMCP).
    Returns ``report(enabled)``. Raises KeyError for a registered tool in no family."""
    enabled = current() if enabled is None else enabled
    keep = frozenset().union(*(FAMILIES[f] for f in enabled))
    tools = mcp._tool_manager._tools
    for name in list(tools):
        family_of(name)                       # every registered tool must be classified
        if name not in keep:
            del tools[name]
    return report(enabled)
