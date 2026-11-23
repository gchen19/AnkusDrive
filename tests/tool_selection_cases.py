"""Corpus for the tool-selection eval (#508): requests a user might type, and the
tool(s) that count as the right first call.

Not a test file (no ``test_`` prefix); ``tests/test_tool_selection.py`` imports it.

``CLUSTERS`` groups tools a model can plausibly mix up, found by reading the
``tools/list`` names and descriptions side by side. Every member of a cluster is
exposed to the model whenever any case of that cluster runs, so a confusion inside
the cluster is always possible and therefore measurable. ``CASES`` are the requests.

Writing a case:

* Say what the user wants, the way a user would. Do not name the tool, and do not
  copy distinctive words out of its description: that tests string matching, not
  disambiguation.
* ``accept`` holds every tool that is a correct FIRST call. Keep it tight. If two
  tools would both do the job, list both. If the request is honestly ambiguous,
  rewrite the request rather than widening ``accept`` until anything passes.
* Name objects the request depends on ("the body Body", "page Page") so that the
  correct first call is the operation itself and not a discovery call such as
  ``list_objects``. The system prompt tells the model these objects exist.
* The ``controls`` cluster holds unambiguous requests. A miss there points at the
  harness or the prompt, not at a description.
"""

CLUSTERS = {
    "modal": {
        "members": ["fem_modal", "fem_modal_results", "beam_modal", "random_vibration",
                    "harmonic_response", "harmonic_response_submit"],
        "why": "closed-form modal oracle vs configuring an eigen-solve vs reading its result, "
               "and the two response tools built on f_n",
    },
    "buckling": {
        "members": ["fem_buckling", "fem_buckling_results", "beam_buckling"],
        "why": "Euler/Johnson hand calc vs CalculiX eigen-buckling setup vs its result",
    },
    "section": {
        "members": ["section_view", "add_section_view", "render_view"],
        "why": "measure a cut through the solid vs place a section view on a drawing",
    },
    "render": {
        "members": ["render_view", "render_views", "render_photoreal", "render_photoreal_submit",
                    "render_fem_results", "render_job", "render_capabilities", "job_status"],
        "why": "fast preview vs multi-view vs photoreal (blocking/async) vs FEM colouring vs polling",
    },
    "fillet_chamfer": {
        "members": ["fillet_edges", "partdesign_fillet", "chamfer_edges", "partdesign_chamfer"],
        "why": "Part-workbench vs PartDesign variants of the same edge operation",
    },
    "hollow": {
        "members": ["thickness", "shell_solid", "pocket"],
        "why": "PartDesign Thickness vs Part shell; both 'hollow a solid'",
    },
    "hole_thread": {
        "members": ["hole", "pocket", "add_thread", "list_thread_options", "boolean_op"],
        "why": "parametric hole vs pocket vs modelled helical thread vs thread metadata",
    },
    "catalog": {
        "members": ["catalog_search", "catalog_nearest", "catalog_check",
                    "standard_part_designate", "designation_check"],
        "why": "browse vs snap-to-nearest vs is-this-buyable vs make-a-designation vs BOM gate",
    },
    "measure": {
        "members": ["measure_distance", "min_clearance", "interference_check", "bounding_box",
                    "mass_properties", "envelope_check"],
        "why": "gap between two parts vs overlap volume vs extents vs mass vs keep-out",
    },
    "shape_checks": {
        "members": ["check_shape", "check_airtight_path", "verify_feature", "verify_intent"],
        "why": "topological validity vs flow-path connectivity vs feature volume delta vs invariants",
    },
    "fem_run_results": {
        "members": ["fem_run", "fem_run_submit", "fem_results", "fem_result_probe",
                    "fem_thermal_results", "job_status"],
        "why": "blocking vs async solve; global summary vs probe at a location",
    },
    "drawing_checks": {
        "members": ["add_dimension", "drawing_gate", "drawing_legibility", "fit_page",
                    "export_drawing"],
        "why": "add dims vs completeness gate vs layout-readability gate vs auto-fit",
    },
    "tolerance": {
        "members": ["tolerance_stackup", "fit_check", "fit_class", "tolerance_cost_check",
                    "suggest_loosening", "gdt_check"],
        "why": "stack a chain vs classify a pair vs ISO 286 lookup vs price vs loosen",
    },
    "flow": {
        "members": ["cfd_pipe_flow", "cfd_internal_flow_submit", "cfd_body_drag",
                    "cfd_external_flow_submit", "cfd_mesh_independence_submit", "grid_convergence",
                    "fsi_channel_pressure"],
        "why": "*_screen vs *_submit: analytic flow screens vs OpenFOAM/SU2 solves, GCI vs ladder",
    },
    "thermal": {
        "members": ["thermal_lumped", "thermal_transient_1d", "thermal_transient_submit",
                    "h_estimate", "thermal_composite_wall", "fem_thermal_results"],
        "why": "lumped vs 1-D analytic vs Elmer transient vs convection coefficient vs U-value",
    },
    "molding": {
        "members": ["molding_screen", "moldability_screen", "moldability_check",
                    "molding_fill_submit", "molding_warpage_submit", "dfm_check", "draft"],
        "why": "analytic molding screen vs geometric moldability vs fill/warp solves vs adding draft",
    },
    "impact": {
        "members": ["drop_impact", "bar_impact", "impact_dynamics_submit"],
        "why": "energy-balance drop screen vs St-Venant bar vs transient contact solve",
    },
    "fdm_cnc": {
        "members": ["slice_estimate", "slice_gcode_submit", "cnc_time_estimate",
                    "cnc_machinability_check", "cost_estimate"],
        "why": "analytic slice vs real PrusaSlicer run; CNC time vs setups vs cost rollup",
    },
    "mechanism": {
        "members": ["mechanism_kinematics", "mechanism_simulate_submit"],
        "why": "closed-form four-bar/slider gates vs PyBullet dynamics",
    },
    "em": {
        "members": ["em_skin_depth", "em_induction_submit", "em_induction_heating_submit",
                    "em_dc_resistance", "em_conduction_submit"],
        "why": "closed-form skin depth / resistance vs Elmer harmonic and conduction solves",
    },
    "controls": {
        "members": ["new_document", "export_shape", "add_gear", "sheet_unfold", "bearing_life",
                    "material_get", "add_fastener", "save_document"],
        "why": "unambiguous requests; a miss here means the harness or prompt is wrong",
    },
}

CASES = [
    # --- modal -------------------------------------------------------------
    {"id": "modal_textbook", "cluster": "modal", "accept": ["beam_modal"],
     "request": "What are the first three natural frequencies of a 300 mm long, 20 x 5 mm "
                "steel cantilever? I just want the textbook answer, no meshing."},
    {"id": "modal_setup", "cluster": "modal", "accept": ["fem_modal"],
     "request": "Configure the FEM analysis 'Analysis' so the next solve extracts the first "
                "6 vibration modes between 0 and 2000 Hz."},
    {"id": "modal_read", "cluster": "modal", "accept": ["fem_modal_results"],
     "request": "The frequency solve on analysis 'Analysis' just finished. What natural "
                "frequencies did it find?"},
    {"id": "modal_psd", "cluster": "modal", "accept": ["random_vibration"],
     "request": "The bracket's modes are at 120 Hz and 450 Hz. Under a flat 0.04 g^2/Hz base "
                "input from 20 to 2000 Hz with 3% damping, what RMS acceleration do I get?"},

    # --- buckling ----------------------------------------------------------
    {"id": "buckle_hand", "cluster": "buckling", "accept": ["beam_buckling"],
     "request": "Will a 10 mm diameter, 400 mm long pinned-pinned aluminium strut buckle "
                "under 3 kN? Quick closed-form check is fine."},
    {"id": "buckle_setup", "cluster": "buckling", "accept": ["fem_buckling"],
     "request": "Set analysis 'Analysis' up for a linear eigen-buckling solve of the column "
                "body; I've already put a unit force on the top face."},

    # --- section -----------------------------------------------------------
    {"id": "section_area", "cluster": "section", "accept": ["section_view"],
     "request": "Slice the bracket 'Bracket' with the XZ plane at y = 12 mm and tell me the "
                "cross-sectional area there."},
    {"id": "section_drawing", "cluster": "section", "accept": ["add_section_view"],
     "request": "On drawing page 'Page', add a cut-away view so the shop can see the "
                "counterbore depth in 'Plate'."},

    # --- render ------------------------------------------------------------
    {"id": "render_quick", "cluster": "render", "accept": ["render_view"],
     "request": "Show me a quick isometric picture of 'Bracket' so I can check the fillet "
                "came out right."},
    {"id": "render_multi", "cluster": "render", "accept": ["render_views"],
     "request": "Give me top, front and iso images of 'Housing' in one go."},
    {"id": "render_hero", "cluster": "render", "accept": ["render_photoreal", "render_photoreal_submit"],
     "request": "Make a marketing-quality hero shot of the 'Gearbox' assembly in brushed "
                "aluminium for the product page."},
    {"id": "render_async", "cluster": "render", "accept": ["render_photoreal_submit"],
     "request": "Kick off a final-quality 4K studio render of 'Gearbox'. It will take a "
                "while, so don't block; I'll check back later."},
    {"id": "render_stress", "cluster": "render", "accept": ["render_fem_results"],
     "request": "Show me the part coloured by von Mises stress from the finished analysis "
                "'Analysis'."},
    {"id": "render_poll", "cluster": "render", "accept": ["render_job"],
     "request": "Is the photoreal render I started earlier (job r-12) finished yet? If so I "
                "want the image."},

    # --- fillet / chamfer --------------------------------------------------
    {"id": "fillet_part", "cluster": "fillet_chamfer", "accept": ["fillet_edges"],
     "request": "Round all 12 edges of the plain Part box 'Box' (not in a Body) with a 2 mm "
                "radius."},
    {"id": "fillet_pd", "cluster": "fillet_chamfer", "accept": ["partdesign_fillet"],
     "request": "Inside PartDesign body 'Body', round the four top edges of feature 'Pad' "
                "with r = 1.5 mm."},
    {"id": "chamfer_part", "cluster": "fillet_chamfer", "accept": ["chamfer_edges"],
     "request": "Bevel the outer edges of the boolean result 'Cut001' by 1 mm. It's a "
                "Part-workbench shape, no Body."},
    {"id": "chamfer_pd", "cluster": "fillet_chamfer", "accept": ["partdesign_chamfer"],
     "request": "Break the edges of 'Pocket001' in body 'Body' with a 0.5 mm 45-degree "
                "bevel."},

    # --- hollow ------------------------------------------------------------
    {"id": "hollow_pd", "cluster": "hollow", "accept": ["thickness"],
     "request": "Hollow out PartDesign body 'Body' (tip feature 'Pad') to a 2 mm wall, "
                "leaving the top face open."},
    {"id": "hollow_part", "cluster": "hollow", "accept": ["shell_solid"],
     "request": "Turn the Part-workbench box 'Box' into a 1.5 mm thin-walled tray with the "
                "top face removed."},

    # --- hole / thread -----------------------------------------------------
    {"id": "hole_cbore", "cluster": "hole_thread", "accept": ["hole"],
     "request": "Using the circles in sketch 'Sketch001' on the top face of body 'Body', "
                "drill counterbored clearance holes for M6 socket head screws, through all."},
    {"id": "pocket_recess", "cluster": "hole_thread", "accept": ["pocket"],
     "request": "Cut the rectangle in sketch 'Sketch002' 3 mm down into body 'Body' to make "
                "a recess for the label."},
    {"id": "thread_real", "cluster": "hole_thread", "accept": ["add_thread"],
     "request": "I'm 3D printing an M10 bolt and need actual helical threads on it, not "
                "just a cosmetic thread flag. Make me 30 mm of thread."},

    # --- catalog -----------------------------------------------------------
    {"id": "cat_browse", "cluster": "catalog", "accept": ["catalog_search"],
     "request": "What lengths of ISO 4762 M5 socket head cap screws are commonly stocked?"},
    {"id": "cat_nearest", "cluster": "catalog", "accept": ["catalog_nearest"],
     "request": "My design calls for an M4 socket head screw 13 mm long. What's the closest "
                "length I can actually buy?"},
    {"id": "cat_check", "cluster": "catalog", "accept": ["catalog_check"],
     "request": "Is '608-2RS' an off-the-shelf part I can just order?"},
    {"id": "cat_gate", "cluster": "catalog", "accept": ["designation_check"],
     "request": "Before I send the BOM to purchasing, confirm that every bought-in part in "
                "assembly 'Assembly' has a designation a buyer can quote against."},

    # --- measure -----------------------------------------------------------
    {"id": "meas_gap", "cluster": "measure", "accept": ["measure_distance", "min_clearance"],
     "request": "How big is the gap between 'Lid' and 'Base' at their closest point?"},
    {"id": "meas_overlap", "cluster": "measure", "accept": ["interference_check"],
     "request": "Do any of the parts in assembly 'Assembly' overlap each other, and by how "
                "much volume?"},

    # --- shape checks ------------------------------------------------------
    {"id": "chk_valid", "cluster": "shape_checks", "accept": ["check_shape"],
     "request": "That boolean looked odd. Is 'Cut003' still a valid closed solid before I "
                "build on it?"},
    {"id": "chk_flow", "cluster": "shape_checks", "accept": ["check_airtight_path"],
     "request": "The vacuum adapter 'Adapter' is watertight, but is the internal channel "
                "actually connected from the inlet face to the outlet face?"},

    # --- FEM run / results -------------------------------------------------
    {"id": "fem_big_run", "cluster": "fem_run_results", "accept": ["fem_run_submit"],
     "request": "Solve analysis 'Analysis'. It's a fine mesh with about 400k nodes, so it'll "
                "take several minutes; don't tie up the connection."},
    {"id": "fem_summary", "cluster": "fem_run_results", "accept": ["fem_results"],
     "request": "What's the peak von Mises stress and max displacement from the finished "
                "analysis 'Analysis'?"},
    {"id": "fem_probe", "cluster": "fem_run_results", "accept": ["fem_result_probe"],
     "request": "From the finished analysis 'Analysis', what's the stress right at the point "
                "(10, 0, 25) mm, next to the bolt hole?"},

    # --- drawing checks ----------------------------------------------------
    {"id": "drw_complete", "cluster": "drawing_checks", "accept": ["drawing_gate"],
     "request": "Does drawing 'Page' carry enough dimensions for a machinist to make the part "
                "without guessing, and none twice?"},
    {"id": "drw_legible", "cluster": "drawing_checks", "accept": ["drawing_legibility"],
     "request": "Some dimension text on 'Page' looks like it's colliding. Check the sheet for "
                "overlapping labels or anything hanging off the border."},

    # --- tolerance ---------------------------------------------------------
    {"id": "tol_stack", "cluster": "tolerance", "accept": ["tolerance_stackup"],
     "request": "Three parts stack in a housing: 20 +/-0.1, 15 +/-0.05 and 8 +/-0.05 mm, "
                "inside a 43.5 +/-0.1 bore. What's the worst-case and RSS gap?"},
    {"id": "tol_iso", "cluster": "tolerance", "accept": ["fit_class"],
     "request": "What are the ISO limits for a 25 mm H7/g6 fit?"},
    {"id": "tol_pair", "cluster": "tolerance", "accept": ["fit_check"],
     "request": "Hole is 12.000/+0.018/0, shaft is 12.000/-0.006/-0.017. Is that clearance, "
                "transition or interference?"},

    # --- flow (screen vs submit) -------------------------------------------
    {"id": "flow_pipe_screen", "cluster": "flow", "accept": ["cfd_pipe_flow"],
     "request": "Rough pressure drop for 2 L/min of water through 3 m of 8 mm ID tube?"},
    {"id": "flow_internal_cfd", "cluster": "flow", "accept": ["cfd_internal_flow_submit"],
     "request": "Run a proper CFD simulation of the pressure drop through manifold 'Manifold' "
                "at 5 L/min; the elbows matter, so a correlation won't do."},
    {"id": "flow_drag_screen", "cluster": "flow", "accept": ["cfd_body_drag"],
     "request": "Ballpark the drag on a 40 mm sphere in 15 m/s air."},
    {"id": "flow_external_cfd", "cluster": "flow", "accept": ["cfd_external_flow_submit"],
     "request": "Simulate airflow around the drone arm 'Arm' at 12 m/s with OpenFOAM and give "
                "me the drag and lift."},
    {"id": "flow_gci", "cluster": "flow", "accept": ["grid_convergence"],
     "request": "I got drag of 1.92, 1.95 and 2.04 N on fine, medium and coarse meshes. How "
                "much of that number is mesh error?"},

    # --- thermal -----------------------------------------------------------
    {"id": "th_lumped", "cluster": "thermal", "accept": ["thermal_lumped"],
     "request": "A small aluminium heat-spreader block dissipates 5 W with h = 10 W/m^2K over "
                "40 cm^2. How hot does it settle and how fast does it get there?"},
    {"id": "th_slab", "cluster": "thermal", "accept": ["thermal_transient_1d"],
     "request": "A 20 mm steel plate at 600 C is quenched in water. Analytically, what's the "
                "mid-plane temperature after 30 s?"},
    {"id": "th_fem", "cluster": "thermal", "accept": ["thermal_transient_submit"],
     "request": "Run a transient thermal FEM with Elmer on the heat sink, 60 s of warm-up."},

    # --- molding -----------------------------------------------------------
    {"id": "mold_cool", "cluster": "molding", "accept": ["molding_screen"],
     "request": "Roughly how long will a 2.5 mm ABS wall take to cool in the mould, and will "
                "a 150 mm flow length fill?"},
    {"id": "mold_geom", "cluster": "molding", "accept": ["moldability_check", "dfm_check"],
     "request": "Check the actual CAD part 'Cover' for injection-moulding problems: wall "
                "thickness and draft against a +Z pull."},
    {"id": "mold_fill", "cluster": "molding", "accept": ["molding_fill_submit"],
     "request": "Simulate filling the cavity of 'Cover' with polystyrene and tell me whether "
                "it short-shots."},

    # --- impact ------------------------------------------------------------
    {"id": "imp_drop", "cluster": "impact", "accept": ["drop_impact"],
     "request": "If a 2 kg box drops 1 m onto foam with 25 mm of crush stroke, what "
                "deceleration does it see?"},
    {"id": "imp_fem", "cluster": "impact", "accept": ["impact_dynamics_submit"],
     "request": "Run a transient contact simulation of the 'Housing' body dropped from 1.2 m "
                "onto a rigid floor."},

    # --- FDM / CNC ---------------------------------------------------------
    {"id": "fdm_quick", "cluster": "fdm_cnc", "accept": ["slice_estimate"],
     "request": "Roughly how much filament and print time for 'Bracket' at 20% infill? No "
                "need to run a slicer."},
    {"id": "fdm_real", "cluster": "fdm_cnc", "accept": ["slice_gcode_submit"],
     "request": "Slice 'Bracket' with PrusaSlicer, supports on, and give me the G-code."},
    {"id": "cnc_time", "cluster": "fdm_cnc", "accept": ["cnc_time_estimate"],
     "request": "How long will it take to machine 'Plate' from 6061 bar stock?"},

    # --- mechanism ---------------------------------------------------------
    {"id": "mech_grashof", "cluster": "mechanism", "accept": ["mechanism_kinematics"],
     "request": "Four-bar with crank 30, coupler 80, rocker 60, ground 70 mm: does the crank "
                "fully rotate?"},
    {"id": "mech_dyn", "cluster": "mechanism", "accept": ["mechanism_simulate_submit"],
     "request": "Simulate the dynamics of this linkage over 2 s with gravity and a 1 N·m "
                "motor torque on the crank."},

    # --- EM ----------------------------------------------------------------
    {"id": "em_delta", "cluster": "em", "accept": ["em_skin_depth"],
     "request": "What's the skin depth in copper at 50 kHz?"},
    {"id": "em_heat", "cluster": "em", "accept": ["em_induction_heating_submit"],
     "request": "Simulate induction heating of a steel billet at 10 kHz for 20 s and give me "
                "the temperature rise."},

    # --- controls ----------------------------------------------------------
    {"id": "ctl_newdoc", "cluster": "controls", "accept": ["new_document"],
     "request": "Start a new empty FreeCAD document for me."},
    {"id": "ctl_step", "cluster": "controls", "accept": ["export_shape"],
     "request": "Export 'Bracket' to bracket.step so I can send it to the supplier."},
    {"id": "ctl_gear", "cluster": "controls", "accept": ["add_gear"],
     "request": "Make a 24-tooth spur gear, module 1.5, 8 mm thick."},
    {"id": "ctl_bearing", "cluster": "controls", "accept": ["bearing_life"],
     "request": "What's the L10 life in hours of a 6205 bearing at 1500 rpm under 2 kN "
                "radial?"},
]
