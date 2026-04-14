# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Language

- Reply in the language the user is currently using (Chinese -> Chinese, English -> English).
- Code, code comments, documentation files, and CLAUDE.md must always be written in English.

## Project Purpose

Automated agentic optimization loop for designing an FDM 3D-printable fluid funnel (automotive windshield washer fluid). The system generates internal non-linear helical vane topologies that induce annular flow (stable central air core) to eliminate glugging during pouring. The optimization loop iterates: parametric geometry -> CFD simulation -> fitness evaluation -> next parameter set.

## Architecture

The pipeline is a sequential three-stage loop orchestrated by `optimization/loop.py`:

```
optimization/loop.py  (controller)
    |
    +--> geometry/funnel_generator.py   CadQuery: params (JSON) -> STEP + STL
    |
    +--> cfd/runner.py                  Copies base_case/, injects STL,
    |       |                           runs blockMesh -> snappyHexMesh -> interFoam,
    |       +-- cfd/base_case/          extracts air volume fraction & velocity
    |             0/                    OpenFOAM template (time-zero fields)
    |             constant/             (transportProperties, turbulenceProperties, g)
    |             system/               (controlDict, fvSchemes, fvSolution,
    |                                    blockMeshDict, snappyHexMeshDict)
    |
    +--> output/                        Converged STLs + visualization data
```

**Fitness metric:** Stability and diameter of the central air core at the funnel throat, plus volumetric flow velocity. Extracted by parsing OpenFOAM postProcessing output from interFoam (VOF multiphase solver).

## Tech Stack

| Layer | Tool | Notes |
|-------|------|-------|
| Parametric CAD | CadQuery (Python) | Generates solid geometry; outputs STEP and STL |
| CFD | OpenFOAM (`interFoam`) | Transient VOF multiphase (air/water) |
| Meshing | `blockMesh` + `snappyHexMesh` | Background mesh + STL surface snapping |
| Orchestration | Python (subprocess) | Log parsing, parameter injection, optimization |

## Commands

```bash
# Install Python dependencies
pip install -r requirements.txt

# Generate funnel geometry (once implemented)
python geometry/funnel_generator.py --config params.json

# Run a single CFD case (once implemented)
python cfd/runner.py --stl output/funnel.stl --case-dir cfd/run_001

# Run the full optimization loop (once implemented)
python optimization/loop.py
```

OpenFOAM commands (called by `cfd/runner.py` via subprocess):
```bash
blockMesh
snappyHexMesh -overwrite
interFoam
```

## FDM Manufacturing Constraints

These constraints are hard requirements that must be respected in all geometry generation:

- **Material:** PETG / ABS
- **Minimum wall thickness:** 1.2 mm (hydrostatic pressure resistance)
- **Overhang limit:** All internal helical structures must be self-supporting (< 50 degrees from vertical / Z-axis) — no internal support material allowed
- **Parameterized dimensions:** Bowl diameter ~100 mm, stem OD ~35 mm (all dimensions must remain fully parametric)

## Key Design Parameters (Helical Vanes)

The geometry generator must expose these as tunable parameters for the optimization loop:

- Number of fins
- Start / end height along funnel axis
- Sweep angle
- Pitch
- Radial depth (how far fins extend toward center)

## OpenFOAM Case Structure

The `cfd/base_case/` directory follows standard OpenFOAM layout:
- `0/` — Initial/boundary conditions (alpha.water, U, p_rgh)
- `constant/` — Physical properties (transportProperties, turbulenceProperties, g)
- `system/` — Solver and mesh controls (controlDict, fvSchemes, fvSolution, blockMeshDict, snappyHexMeshDict, setFieldsDict)

Boundary conditions: water mass-flow inlet at top, atmospheric open boundaries, gravity vector pointing downward (-Z).

## Execution Phases

### Phase 1 — Geometry
Implement `geometry/funnel_generator.py`: parametric bowl, convergent neck, stem, and internal helical fins. Built-in manufacturing constraint validation (wall thickness >= 1.2 mm, overhang < 50 deg) that rejects or penalizes invalid parameters before any downstream work. Output STEP + STL.

### Phase 1.5 — Mesh Validation
Verify that Phase 1 STL output is watertight with consistent normals and no self-intersections. Run `blockMesh` + `snappyHexMesh` against the generated STL to confirm mesh feasibility. Tune CadQuery export tolerance and triangle density as needed. Configure `surfaceFeatureExtract`.

### Phase 2 — CFD Setup
Scaffold all OpenFOAM dictionary files for transient interFoam multiphase simulation. Validate solver convergence with a simple cylinder geometry first (baseline case), then switch to the actual funnel geometry. Key concerns: Courant number limits, adaptive time-stepping, discretization scheme stability.

### Phase 3 — Pipeline Integration
Wire `cfd/runner.py` end-to-end: copy base_case -> inject STL -> mesh -> solve -> extract fitness metrics (air-core diameter, volume fraction at throat, flow velocity). Confirm one full pass completes successfully.

### Phase 4 — Optimization Loop
Implement `optimization/loop.py`: define parameter space with bounds and constraints, implement optimization algorithm (e.g., Bayesian optimization), convergence criteria, and per-iteration result persistence (parameters + fitness) for comparison and rollback.
