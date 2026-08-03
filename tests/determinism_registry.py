"""
Determinism-class registry for the MCP tool surface (issue #123).

Every DriftPin MCP tool falls into one of three determinism classes:

  "exact"
      Closed-form / kernel computation that MUST be bit-for-bit identical across
      worker boots and repeated calls. The analysis-screen family (no solver) and
      the OpenCASCADE geometry kernel live here. A hidden dict-ordering, set
      iteration, or unseeded RNG is a *bug* for these — caught by the bitwise
      sweeps in tests/test_determinism.py.

  "bounded"
      A solver / external-tool / async-job result whose value is reproducible only
      within a documented tolerance envelope (mesh-order, BLAS reduction order,
      iterative-solver residual). The `*_submit` family lives here; their bound is
      asserted the way test_fem_results_within_tolerance does it.

  "nondeterministic-by-design"
      A tool whose output legitimately varies run-to-run (timestamps, random IDs).
      None today — kept as a declared class so a future such tool has a home other
      than the silent gap.

WHY A REGISTRY: the determinism suite only guards the tools it knows about. Without
a forced declaration, every newly-added tool silently re-opens the coverage gap.
`test_every_tool_has_a_determinism_class` (in tests/test_contracts.py) fails when a
new MCP tool appears that is neither classified here nor on the explicit
`NOT_YET_CLASSIFIED` allowlist — so the net is tightenable: move a name off the
allowlist into a class and add it to the sweep.

This module is PURE DATA + stdlib (no FreeCAD, no numpy import at module load) so
both the fast static contract lane and the FreeCAD determinism lane can import it.
"""

# --- determinism class declarations -------------------------------------------

# Closed-form analysis / screen tools: bit-identical by construction.
EXACT_TOOLS = {
    # machine elements
    "bearing_life", "gear_rating", "belt_drive", "bolted_joint_check", "spring_check",
    "chain_drive", "weld_group",
    # fits / tolerance / GD&T
    "fit_class", "fit_check", "tolerance_stackup", "gdt_check",
    # tolerance <-> cost coupling (issue #235). Both are closed-form over a plain
    # chain; suggest_loosening's cpk comes from tolerance_stackup's SEEDED Monte
    # Carlo, so — exactly like the tolerance_stackup sweep entry — an unseeded RNG
    # anywhere in that path is what these catch. Both also accept an optional
    # `handle` to derive the chain off a solid, the same way tolerance_stackup
    # already does; the sweep exercises the closed-form kwargs path.
    "tolerance_cost_check", "suggest_loosening",
    # thermal
    "thermal_lumped", "thermal_transient_1d", "thermal_composite_wall", "h_estimate",
    # structural / vibration screens
    "beam_modal", "beam_buckling", "plate_check", "random_vibration",
    "plastic_collapse", "elastica_deflection", "hertz_contact",
    # fluids
    "cfd_pipe_flow", "cfd_body_drag",
    # impact / acoustics / EM closed-form twins
    "drop_impact", "acoustic_screen", "em_skin_depth", "em_dc_resistance",
    "waveguide_cutoff", "dipole_resonance", "monopole_sphere", "rigid_sphere_scattering",
    # FSI closed-form twins
    "fsi_channel_pressure", "fsi_plate_deflection", "fsi_interface_balance",
    # laminate / composite-stack closed-form
    "laminate_properties",
    # molding / kinematics
    "molding_screen", "moldability_screen", "mechanism_kinematics",
    # dfx / cost / slicing
    "dfm_check", "dfa_check", "pack_check", "cost_estimate", "slice_estimate",
    # durability
    "fatigue_check", "fracture_check", "wear_estimate", "creep_flag",
    # orderable standard parts (issue #234): the designation layer is pure string
    # arithmetic and the catalog layer is exact lookup on a discrete ladder in a
    # checked-in dated corpus, so all four are bit-identical by construction — no
    # wall clock (the capture date comes from the corpus, never from now()), no
    # network, no dict-order or set-iteration dependence.
    "standard_part_designate", "catalog_search", "catalog_nearest", "catalog_check",
}

# Solver / external-CLI / async-job submits: reproducible within a documented bound.
BOUNDED_TOOLS = {
    "acoustic_fem_submit", "acoustic_radiation_submit", "async_demo_submit",
    "cfd_external_flow_submit", "cfd_internal_flow_submit", "cht_channel_submit",
    "cht_graetz_submit", "dem_flow_submit", "dem_pack_submit", "em_conduction_submit",
    "em_fullwave_submit", "em_induction_heating_submit", "em_induction_submit",
    "fsi_pressure_plate_submit", "harmonic_response_submit", "mechanism_simulate_submit",
    "molding_fill_submit", "molding_warpage_submit", "render_photoreal_submit",
    "slice_gcode_submit", "thermal_radiation_submit", "thermal_transient_submit",
    "topology_optimize_submit",
}

# No tool is intentionally non-reproducible today, but the class is declared so a
# future one (random IDs, wall-clock stamps) is classified rather than slipping
# through.
NONDETERMINISTIC_TOOLS = set()


def determinism_class(tool_name):
    """Return the declared class for a tool, or None if unclassified."""
    if tool_name in EXACT_TOOLS:
        return "exact"
    if tool_name in BOUNDED_TOOLS:
        return "bounded"
    if tool_name in NONDETERMINISTIC_TOOLS:
        return "nondeterministic-by-design"
    return None


# Tools not yet given a determinism class. This is the EXPLICIT, tightenable net:
# a newly-added MCP tool that is neither classified above nor listed here trips
# test_every_tool_has_a_determinism_class. The geometry/CAD-mutation kernel tools
# below are deterministic-by-OCC and partially guarded by the bitwise geometry
# tests already; they're parked here pending a representative-kwargs sweep entry.
# BURN THIS DOWN — move names into a class + add a sweep entry; do not grow it.
NOT_YET_CLASSIFIED = {
    # change orders + where-used impact + baselines (issue #142, C3): pure-data,
    # deterministic by construction (where-used over the §9 lockfile graph; a
    # byte-fingerprint baseline) but parked here pending a representative-kwargs
    # sweep entry, exactly like the items_*/lifecycle_*/project_* C-theme tools.
    'where_used', 'change_impact', 'eco_validate', 'eco_create',
    'baseline_create', 'baseline_verify',
    'add_annotation', 'add_bearing', 'add_dimension', 'add_fastener', 'add_feature_note',
    'add_gear',
    'add_part', 'add_primitive', 'add_projection_group', 'add_pulley', 'add_rack',
    'add_rib', 'add_section_view', 'add_sketch_constraint', 'add_sketch_external',
    'add_sketch_geometry', 'add_spring', 'add_sprocket', 'add_thread', 'add_thumbnail',
    'annotate_face', 'assembly_lock', 'assembly_lock_check', 'bom_extract', 'boolean_op',
    'component_contract_check',  # builder-side gate (#169): watertight + envelope + interfaces
    'render_fem_results',        # FEM-field render (#174), host-side raster like render_view*
    'bounding_box', 'chamfer_edges', 'check_airtight_path', 'check_shape',
    'classify_face_sides', 'close_document', 'close_sketch', 'contact_setup', 'copy_shape',
    'declare_intent', 'draft', 'drawing_gate', 'drawing_legibility', 'em_field',
    # inspection artifacts (issue #232): the arithmetic is closed-form and the
    # balloon numbering is deterministic by construction (both proven directly in
    # tests/test_inspection.py), but these read a live TechDraw page rather than
    # plain kwargs, so they park here with the other page-reading drawing tools
    # pending a representative-kwargs sweep entry.
    'add_gdt_callout', 'balloon_drawing', 'inspection_plan', 'fai_report',
    # CNC machinability + machining time (issue #231): the pure cores
    # (analysis/machining.py) are closed-form and unit-tested bitwise in
    # tests/test_machining.py, but BOTH tools read a live solid — a face census,
    # ray casts and a sphere/solid boolean — so they park here with the other
    # geometry-reading screens rather than claiming a kwargs-only sweep entry.
    'cnc_machinability_check', 'cnc_time_estimate',
    # release packages (issue #233): the bundle is DETERMINISTIC by contract — the
    # same item at the same revision produces byte-identical files, proven directly
    # by the checksum-diff gate in tests/test_release_package_worker.py (and the
    # export-header scrubs that make it true in tests/test_release_package.py). It
    # parks here rather than in EXACT_TOOLS because it reads a live document plus an
    # on-disk registry, so it has no representative-kwargs sweep entry.
    'release_package',
    # orderable standard parts (issue #234). designation_check walks a live
    # assembly, so it parks here with the other assembly-reading gates rather than
    # in EXACT_TOOLS with its kwargs-only siblings.
    'designation_check',
    'feature_instantiate', 'feature_list', 'feature_schema', 'feature_validate',
    'engrave_text', 'envelope_check', 'export_drawing', 'export_shape', 'fem_add_constraint',
    'engrave_text', 'envelope_check', 'export_drawing', 'export_shape',
    'family_materialize', 'family_validate', 'fem_add_constraint',
    'fem_buckling', 'fem_buckling_results', 'fem_cantilever_demo', 'fem_mesh',
    'fem_mesh_refinement', 'fem_modal', 'fem_modal_results', 'fem_new_analysis',
    'fem_result_probe', 'fem_results', 'fem_run', 'fem_set_material',
    'fem_set_nonlinear_material', 'fem_set_solver', 'fem_thermal_results', 'fillet_edges',
    'fit_page', 'fluid_props', 'get_interface', 'get_object', 'granular_screen',
    'harmonic_response',
    'helix', 'hole', 'interface_align_check', 'interference_check',
    'items_check_manifest', 'items_new', 'items_resolve', 'items_validate', 'job_list',
    'lifecycle_apply_change', 'lifecycle_classify_change', 'lifecycle_editable',
    'lifecycle_transition',
    'job_result', 'job_status', 'linear_pattern', 'list_assembly_parts', 'list_documents',
    'list_edges', 'list_face_roles', 'list_faces', 'list_objects', 'list_thread_options',
    'loft', 'make_assembly', 'make_body', 'make_datum_plane', 'make_drawing_page',
    'make_sketch', 'mass_properties', 'material_get', 'material_list', 'material_select',
    'measure_angle', 'measure_distance', 'merge_assembly', 'min_clearance', 'mirrored',
    'moldability_check', 'new_document', 'open_document', 'optics_lens_design',
    'optics_lens_optimize', 'optics_moldability_check', 'optics_raytrace',
    'optics_solid_trace', 'oring_groove', 'pad', 'partdesign_chamfer', 'partdesign_fillet',
    'ping', 'pocket', 'polar_pattern', 'press_fit_stress', 'project_check_references',
    'project_resolve_manifest', 'project_validate', 'publish_interface',
    'query_faces', 'recipe', 'recipe_list', 'recipe_schema', 'recipe_validate',
    'scaffold_project',
    'register_handle', 'render_capabilities', 'render_job',
    'setup_status',   # environment probe (issue #202), same nature as *_capabilities
    'render_photoreal', 'render_view', 'render_views', 'resolve_edge', 'resolve_face',
    'restart_worker', 'use_workspace', 'list_workspaces', 'close_workspace',
    'revolve', 'run_script', 'save_document', 'scale_shape', 'seal_check',
    'section_view', 'set_active_document', 'set_property', 'set_title_block',
    'set_visibility', 'shell_solid', 'solve_capabilities', 'substitutability_check',
    # sheet metal (issue #230): the bend arithmetic underneath is closed-form and
    # deterministic — the flat pattern is DEVELOPED from the feature model rather
    # than fitted to geometry, and that is proven directly in
    # tests/test_sheetmetal.py — but every one of these takes a live sheet handle
    # rather than plain kwargs, so they park here with the other geometry tools
    # pending a representative-kwargs sweep entry.
    'sheet_base', 'sheet_flange', 'sheet_tab', 'sheet_hem', 'sheet_unfold',
    'sheet_refold', 'sheet_flat_export', 'sheet_check',
    'sweep', 'thickness',
    'topology_to_solid', 'transaction_abort', 'transaction_commit', 'transaction_open',
    'transform', 'validate_manifest', 'verify_contract', 'verify_feature', 'verify_intent',
    'version',
}


# --- the exact-tool determinism sweep -----------------------------------------
#
# (tool_name, representative_kwargs) for the closed-form analysis tools. Every
# entry is asserted bit-for-bit identical across two independent workers AND
# across repeated calls in one worker (tests/test_determinism.py). Kwargs are
# plain numbers/strings/lists — no FreeCAD geometry handle needed.
#
# tolerance_stackup intentionally uses method="montecarlo": it exercises the
# SEED CONTRACT (seed=12345 default) — an unseeded RNG would make this entry the
# one that fails, which is exactly the regression we want to catch.

ANALYSIS_SWEEP = [
    # --- machine elements ---
    ("bearing_life", dict(dynamic_load_c_n=30000, equivalent_load_p_n=3000, speed_rpm=1500)),
    ("gear_rating", dict(module_mm=4.0, teeth=20, face_width_mm=40.0, power_w=5000,
                         pinion_speed_rpm=1000)),
    ("belt_drive", dict(power_w=5000, small_pulley_dia_mm=100, large_pulley_dia_mm=300,
                        center_distance_mm=600, small_pulley_rpm=1500)),
    ("bolted_joint_check", dict(bolt_dia_mm=10.0, torque_nm=50.0, k_factor=0.2)),
    ("spring_check", dict(wire_dia_mm=2.0, coil_mean_dia_mm=16.0, active_coils=10,
                          force_n=50.0)),
    ("chain_drive", dict(teeth_small=17, speed_rpm=500.0, chain_number="40")),
    ("weld_group", dict(segments=[((0, 0), (0, 200)), ((100, 0), (100, 200))],
                        force_n=[0, -50000.0], load_point_mm=[200, 100], leg_mm=8.0)),
    # --- laminate / composite-stack closed-form ---
    ("laminate_properties", dict(
        layers=[{"material": "Steel-A36", "thickness": 1.0},
                {"material": "ABS", "thickness": 2.0}],
        width_mm=20.0, delta_T=60.0, moment_nmm=500.0)),
    # --- fits / tolerance / GD&T ---
    ("fit_class", dict(basic_size=25, fit="H7/g6")),
    ("fit_check", dict(hole={"nominal": 20.0, "plus": 0.021, "minus": 0.0},
                       shaft={"nominal": 20.0, "plus": -0.007, "minus": -0.020})),
    ("tolerance_stackup", dict(
        chain=[{"nominal": 10, "plus": 0.1, "minus": 0.1},
               {"nominal": 5, "plus": 0.05, "minus": 0.05}],
        method="montecarlo", samples=5000)),
    ("gdt_check", dict(control="position", zone=0.25, offset={"x": 0.1, "y": 0.0})),
    # --- tolerance <-> cost coupling ---
    ("tolerance_cost_check", dict(
        chain=[{"name": "bore", "nominal": 25.0, "plus": 0.0065, "minus": -0.0065},
               {"name": "slot", "nominal": 40.0, "tol": 0.1}],
        process="cnc")),
    # like the tolerance_stackup entry, this one leans on the SEED CONTRACT: every
    # greedy step re-runs the Monte-Carlo cpk, so an unseeded RNG would surface here.
    ("suggest_loosening", dict(
        chain=[{"name": "shaft", "nominal": 50.0, "tol": 0.008},
               {"name": "spacer", "nominal": 10.0, "tol": 0.01}],
        spec_min=59.7, spec_max=60.3, samples=2000, max_steps=4)),
    # --- thermal ---
    ("thermal_lumped", dict(mass_g=120, power_w=15, h_conv=12, area_mm2=20000, c_p=900,
                            t_ambient_c=25, duration_s=300)),
    ("thermal_transient_1d", dict(half_thickness_mm=10, h_conv=50, duration_s=120,
                                  k=200.0, rho=2700.0, cp=900.0, t_initial_c=100.0)),
    ("thermal_composite_wall", dict(
        layers=[{"thickness_mm": 10, "k": 0.04}, {"thickness_mm": 100, "k": 0.5}],
        t_in_c=20, t_out_c=-10, h_in=8, h_out=25)),
    ("h_estimate", dict(geometry="vertical_plate", characteristic_mm=100, t_surface_c=65,
                        t_ambient_c=25)),
    # --- structural / vibration ---
    ("beam_modal", dict(length_mm=300, width_mm=30, height_mm=10, boundary="cantilever",
                        n_modes=3, youngs_gpa=200, density_kg_m3=7850)),
    ("beam_buckling", dict(length_mm=1000, diameter_mm=20, youngs_gpa=200, yield_mpa=350)),
    ("plate_check", dict(shape="rectangular", thickness_mm=2, pressure_kpa=10, a_mm=200,
                         b_mm=150, material="Steel-1045")),
    ("random_vibration", dict(frequencies_hz=[312],
                              psd_profile=[{"hz": 20, "g2_hz": 0.01},
                                           {"hz": 2000, "g2_hz": 0.01}], q=10)),
    ("plastic_collapse", dict(length_mm=200, width_mm=20, height_mm=10, yield_mpa=250)),
    ("elastica_deflection", dict(load_n=50.0, length_mm=300, youngs_gpa=210, width_mm=20,
                                 height_mm=4)),
    ("hertz_contact", dict(load_n=100, radius_mm=10, youngs1_gpa=210, poisson1=0.3)),
    # --- fluids ---
    ("cfd_pipe_flow", dict(diameter_mm=10, length_mm=1000, flow_rate_lpm=0.5)),
    ("cfd_body_drag", dict(shape="sphere", diameter_mm=50, velocity_m_s=10.0)),
    # --- impact / acoustics / EM twins ---
    ("drop_impact", dict(drop_height_mm=1000, crush_distance_mm=10)),
    ("acoustic_screen", dict(kind="cavity_modes", lx_mm=4000, ly_mm=3000, lz_mm=2500)),
    ("em_skin_depth", dict(frequency_hz=50.0, conductor="copper")),
    ("em_dc_resistance", dict(length_mm=1000.0, area_mm2=1.0, conductor="copper",
                              voltage_v=1.0)),
    ("waveguide_cutoff", dict(a_mm=22.86, freq_ghz=10.0)),
    ("dipole_resonance", dict(length_mm=150.0)),
    ("monopole_sphere", dict(a_m=0.05, freq_hz=1000.0, u_amp=1.0, r_m=1.0)),
    ("rigid_sphere_scattering", dict(ka=1.0, theta_deg=180.0)),
    # --- FSI twins ---
    ("fsi_channel_pressure", dict(velocity_m_s=0.5, length_mm=100, gap_mm=2)),
    ("fsi_plate_deflection", dict(pressure_pa=5000, length_mm=100, width_mm=20,
                                  thickness_mm=2, youngs_gpa=210)),
    ("fsi_interface_balance", dict(pressure_pa=5000, length_mm=100, width_mm=20,
                                   solid_reaction_n=10.0)),
    # --- molding / kinematics ---
    ("molding_screen", dict(wall_thickness_mm=2.0, material="ABS", flow_length_mm=200)),
    ("moldability_screen", dict(wall_samples=[2.0, 2.05, 1.95], nominal_mm=2.0,
                                material="ABS")),
    ("mechanism_kinematics", dict(mechanism="fourbar", ground=10, crank=4, coupler=10,
                                  rocker=4)),
    # --- dfx / cost / slicing ---
    ("dfm_check", dict(faces=[{"name": "a", "draft_deg": 2.0, "wall_mm": 2.0},
                              {"name": "b", "draft_deg": 0.0, "wall_mm": 1.5}],
                       pull_axis="+z", process="injection")),
    ("dfa_check", dict(part_count=12, fastener_count=8)),
    ("pack_check", dict(part_bbox_mm=[300, 200, 150], carton_mm=[310, 210, 160],
                        mass_g=1500)),
    ("cost_estimate", dict(volume_mm3=1e6, material="AL6061-T6")),
    ("slice_estimate", dict(volume_mm3=20000.0, bbox_mm=[40, 25, 20], material="PLA")),
    # --- durability ---
    ("fatigue_check", dict(stress_range_mpa=180, mean_stress_mpa=40, material="Steel-1045")),
    ("fracture_check", dict(stress_mpa=150, crack_len_mm=2.0, material="AL6061-T6")),
    ("wear_estimate", dict(load_n=200, sliding_dist_m=5000, wear_coef=1e-4,
                           hardness_mpa=300, apparent_area_mm2=100)),
    ("creep_flag", dict(stress_mpa=50, temp_c=150, material="AL6061-T6")),
    # --- orderable standard parts (issue #234) ---
    # The designation entry uses the family+spec form (kwargs only, no handle). The
    # catalog entries deliberately include a MISS: catalog_nearest on an unstocked
    # length returns generated prose listing the ladder and the bracketing rungs, and
    # catalog_search returns a size-keyed sweep — precisely the fields a dict- or
    # set-iteration order bug would scramble.
    ("standard_part_designate", dict(
        family="fastener",
        spec={"kind": "socket_head_cap_screw", "size": "M4", "length": 12,
              "grade": "A2"})),
    ("catalog_search", dict(family="screw", size="M6", min_length=10,
                            max_length=30)),
    ("catalog_nearest", dict(standard="ISO 4762", size="M4", length=13,
                             grade="A2")),
    ("catalog_check", dict(designation="ISO 4762 M4×13 A2")),
]


# --- bounded solver-submit envelopes ------------------------------------------
#
# Documented per-solver determinism bounds for the async `*_submit` family. Same
# pattern as test_fem_results_within_tolerance: two runs of identical input must
# agree within the stated relative tolerance on the named scalar fields.
#
# `run` marks the entries the determinism suite actually executes — kept to the
# CHEAP representative(s) so CI doesn't spend minutes on heavy CFD/EM/FEM solves
# on a shared host. The rest are declared (bound documented) but skipped; flip
# `run=True` (or wire a nightly lane) to exercise them.

BOUNDED_SUBMITS = [
    {
        "tool": "async_demo_submit",
        "kwargs": dict(duration_s=0.1, value=3.0),
        "fields": ["value", "squared", "duration_s"],
        "rtol": 0.0,   # exact by construction — guards the submit→job_result async path
        "run": True,
        "note": "reference async job: content-keyed, FreeCAD-free, sub-second.",
    },
    # fem_cantilever_demo is exercised directly by test_fem_results_within_tolerance
    # (8% disp / 15% stress). The heavy real solvers below are DECLARED with their
    # documented bound but not auto-run on the shared host.
    {
        "tool": "cfd_internal_flow_submit",
        "kwargs": None,
        "fields": ["pressure_drop_pa"],
        "rtol": 0.10,  # OpenFOAM simpleFoam: mesh-order + residual convergence band
        "run": False,
        "note": "OpenFOAM pipe solve — minutes on a shared host; declared only.",
    },
    {
        "tool": "acoustic_fem_submit",
        "kwargs": None,
        "fields": ["f_resonance_hz"],
        "rtol": 0.05,  # Elmer HelmholtzSolve: eigen-extraction band
        "run": False,
        "note": "Elmer Helmholtz solve — gated/heavy; declared only.",
    },
]
