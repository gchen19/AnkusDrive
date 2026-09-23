# AnkusDrive reviewer guide

This guide is for anyone evaluating the AnkusDrive desktop extension, including
Anthropic's directory reviewers. It covers what the extension does, how to install and
check it, three example prompts with expected results, and where to get help.

AnkusDrive is a **local MCP server** that drives [FreeCAD](https://www.freecad.org/),
the open-source CAD package, through its Python API. It lets Claude:
- model mechanical parts and assemblies
- measure and inspect them
- produce technical drawings
- run finite-element (FEM) stress analysis

It runs entirely on your computer. It has **no account, no API key and no network
service**, so there is no login to set up, and each example below creates its own part
from scratch, so no sample files are needed.

- Source: https://github.com/gchen19/AnkusDrive (Apache-2.0)
- Privacy policy: [`PRIVACY.md`](https://github.com/gchen19/AnkusDrive/blob/main/PRIVACY.md). It collects nothing and makes no network requests.
- Support: [GitHub issues](https://github.com/gchen19/AnkusDrive/issues). For security reports, see [`SECURITY.md`](https://github.com/gchen19/AnkusDrive/blob/main/SECURITY.md).

## 1. Install (about 5 minutes)

1. **Install FreeCAD 1.1** from https://www.freecad.org/downloads.php, into its default location:
   - **macOS:** drag `FreeCAD.app` into `/Applications`.
   - **Windows:** run the installer, which uses `C:\Program Files\FreeCAD 1.1\` by default.

   AnkusDrive finds FreeCAD there automatically.
2. **Install the extension.** Download `ankusdrive-<version>.mcpb` from the
   [latest release](https://github.com/gchen19/AnkusDrive/releases/latest) and open it,
   or drag it onto Claude Desktop's **Settings → Extensions** page. Accept the install
   dialog with its defaults, and leave **FreeCAD command path** empty.
3. **Wait for the first start.** Claude Desktop sets up the extension's Python
   environment with `uv` (downloading `uv` itself if needed), which takes 10–30 seconds
   the first time. Later starts are fast.

## 2. Check the install

Start a new chat and send:

> Call the AnkusDrive `setup_status` tool and summarize what it reports.

A working install reports:
- **FreeCAD 1.1.x** found, with its path.
- **`install: mcpb`**, meaning the extension is detected.
- **`toolsets` enabled: `core`, `drawings`, `fem`**. The other families are listed as
  switched off, each with how to turn it on (see §4).
- **`run_script: disabled`** (see §4).
- **A solver-families list** in which only CalculiX-based structural analysis is
  normally available. The external solvers (OpenFOAM, Elmer, openEMS, …) are optional,
  separately installed programs. Their absence is expected and shows as "not
  installed" with an install command, not as an error.

## 3. Three example prompts

Send each prompt in a fresh chat. Claude chooses the tools itself, so the exact
sequence can vary between runs. What should hold is listed under **Expected result**.
The same scenarios are run as an automated test on every change
(`tests/test_reviewer_scenarios.py`), against the real server and FreeCAD in this same
configuration.

### Example 1: model a part and check it

> Create a 60 × 40 × 10 mm mounting plate with four 5.5 mm diameter through-holes (M5
> clearance), each hole centered 5 mm in from the nearest two edges. Confirm the result
> is a single watertight solid, then tell me its volume and its mass in aluminum
> (density 2.70 g/cm³).

**Expected result:**
- one **watertight solid**
- **volume ≈ 23,049.7 mm³** (60 × 40 × 10 minus four Ø5.5 × 10 cylinders)
- **mass ≈ 62.2 g**

**Tools typically used:** `new_document`, `add_primitive` (a box and four cylinders),
`boolean_op` (cut), `check_shape`, `mass_properties`.

### Example 2: structural FEM analysis

> Run a static FEM stress analysis of a steel cantilever beam, 100 mm long with a
> 10 × 10 mm square cross-section (E = 210 GPa, Poisson's ratio 0.3), fixed at one end
> with a 100 N load at the free end perpendicular to the beam. Use second-order
> elements. Report the maximum displacement and maximum von Mises stress, and compare
> them with Euler–Bernoulli beam theory.

**Expected result:**
- **maximum displacement ≈ 0.19 mm** (theory: FL³/3EI = 0.1905 mm)
- **maximum von Mises stress ≈ 60 MPa** (theory: Mc/I = 60 MPa)
- both within a few percent of theory. The automated test measures 0.1907 mm and 59.7 MPa.

**Tools typically used:** `new_document`, `add_primitive`, `query_faces` / `list_edges`
(to pick the fixed face, loaded face and load direction), `fem_new_analysis`,
`fem_set_solver`, `fem_set_material`, `fem_add_constraint` (fixed and force),
`fem_mesh` (second order), `fem_run`, `fem_results`.

If a run uses first-order elements or a very coarse mesh, the displacement comes out
noticeably low. That is expected FEM behaviour (linear tetrahedra are too stiff in
bending), not an AnkusDrive defect. Asking for second-order elements, as the prompt
does, avoids it.

### Example 3: technical drawing with a manufacturability check

> Recreate the mounting plate from example 1 (60 × 40 × 10 mm, four Ø5.5 mm holes 5 mm
> in from the edges). Make a technical drawing with front, top and right views and
> overall dimensions, and fill in the title block (part: Mounting plate, material:
> Aluminum 6061, revision A). Export it as a DXF file to my Desktop. Then check whether
> the drawing is complete enough to manufacture the part from, and tell me what's
> missing.

**Expected result:**
- **a DXF on the Desktop** with the three views and the overall dimensions. The title
  block fields are set on the drawing; they are drawn in PDF/SVG exports, not in DXF.
- **a manufacturability report that fails the drawing, correctly.** The automatic
  dimensions cover the overall size, but not the **holes' diameter and positions**, and
  `drawing_gate` lists each of those as missing. A tool that passed this drawing would
  be wrong: nobody could make the part from it.

**Tools typically used:** the modeling tools from example 1, `make_drawing_page`,
`add_projection_group`, `add_dimension`, `set_title_block`, `export_drawing`,
`drawing_gate`.

DXF is used because it needs nothing beyond FreeCAD. PDF and SVG export additionally
need `reportlab` and `svglib` inside FreeCAD's own Python (see the README); without
them, those two formats fail with a clear error naming the missing package.

## 4. Configuration a reviewer may notice

- **Tool families.** To keep context use low, the extension enables only `core`,
  `drawings` and `fem` (115 tools, about 33k tokens) instead of all 286 tools. Other
  families can be turned on under **Settings → Extensions → AnkusDrive**:
  components, sheet metal, assemblies, design intent, manufacturing, hand
  calculations, external-solver simulation, design control/PLM, and rendering.
  `setup_status` lists every disabled family with its toggle.
- **`run_script` is off by default.** It would let Claude execute Python it writes
  inside FreeCAD, with full file access. It is an opt-in toggle ("Allow run_script"),
  and while off the tool is not offered at all. None of the examples need it.
- **Tool annotations.** Every tool declares `title`, `readOnlyHint` and
  `destructiveHint`. Measurements and checks are read-only. Tools that can overwrite a
  file, edit an existing object or discard unsaved work are marked destructive, so a
  client can ask for confirmation before running them.
- **Files.** AnkusDrive reads and writes only the paths given to it (for example the
  DXF in example 3) plus temporary solver directories. See [`PRIVACY.md`](https://github.com/gchen19/AnkusDrive/blob/main/PRIVACY.md).

## 5. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `setup_status` says FreeCAD was not found | FreeCAD isn't in its default location. Set **FreeCAD command path** in the extension's settings to `freecadcmd` (macOS: `/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd`; Windows: `C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe`) and restart the extension. |
| The extension shows as failed right after install | The first start is still installing Python dependencies. Wait 30 seconds and restart the extension. |
| A tool reports "not installed" for a solver | Expected for optional external solvers (OpenFOAM, Elmer, …). The message includes the install command. None of the examples need them. |
| A request needs a tool Claude says isn't available | Its family is switched off. `setup_status` names the toggle to turn on. |
| Anything else | Extension logs: macOS `~/Library/Logs/Claude/mcp-server-AnkusDrive.log`; Windows `%APPDATA%\Claude\logs\` (the `mcp-server-AnkusDrive.log` file). Please attach the relevant lines to a [GitHub issue](https://github.com/gchen19/AnkusDrive/issues). |
