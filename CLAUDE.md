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
optimization/loop.py  (controller — multi-fidelity Bayesian optimisation)
    |
    +--> geometry/funnel_generator.py   CadQuery: params (JSON) -> STEP + STL
    |
    +--> cfd/runner.py                  Docker OpenFOAM: copies base_case/,
    |       |                           injects STL, runs mesh -> solve,
    |       |                           extracts fitness via pyvista
    |       +-- cfd/base_case/          OpenFOAM template
    |             0/                    (alpha.water, U, p_rgh — uniform templates)
    |             constant/             (transportProperties, turbulenceProperties, g)
    |             system/               (controlDict, fvSchemes, fvSolution,
    |                                    blockMeshDict, snappyHexMeshDict,
    |                                    setFieldsDict, decomposeParDict)
    |
    +--> output/                        Converged STLs + visualization data
    +--> output/best_design/            Final optimised design (STL + STEP + CFD)
    +--> output/optimisation/           Per-iteration results + history.json
```

**Fitness metric:** R = 0.50 * norm(air_core_diameter) + 0.30 * norm(throat_velocity) + 0.20 * air_fraction_throat. Extracted at the funnel throat (z=30mm) from interFoam VOF results via pyvista.

## Tech Stack

| Layer | Tool | Notes |
|-------|------|-------|
| Parametric CAD | CadQuery (Python) | Generates solid geometry; outputs STEP and STL |
| CFD | OpenFOAM (`interFoam`) via Docker | Transient VOF multiphase (air/water) |
| Meshing | `blockMesh` + `snappyHexMesh` | Background mesh + STL surface snapping |
| Docker Image | `opencfd/openfoam-default:2406` | All OpenFOAM commands run in container |
| Optimisation | scikit-optimize (Gaussian Process) | Bayesian optimisation with Expected Improvement |
| Visualisation | pyvista + matplotlib | Off-screen rendering (no GPU needed) |
| Orchestration | Python (subprocess) | Docker invocation, log parsing, parameter injection |

## Commands

```bash
# Install Python dependencies
pip install -r requirements.txt

# Pull OpenFOAM Docker image (one-time)
docker pull opencfd/openfoam-default:2406

# Generate funnel geometry
python geometry/funnel_generator.py --config params.json --output output

# Validate parameters only (no geometry generation)
python geometry/funnel_generator.py --config params.json --validate-only

# Run a single CFD case
python cfd/runner.py --stl output/funnel.stl --case-dir cfd/run_001 --nprocs 24 --mesh-level coarse

# Run the full multi-fidelity optimization loop
python optimization/loop.py --coarse 20 --validate 5 --refine 5 --nprocs 24

# Resume optimization from a crash
python optimization/loop.py --coarse 20 --validate 5 --refine 5 --nprocs 24 --resume

# Reproduce the best design (no optimisation needed)
python geometry/funnel_generator.py --config output/best_design/params.json --output output/best_design
```

## FDM Manufacturing Constraints

These constraints are hard requirements that must be respected in all geometry generation:

- **Material:** PETG / ABS
- **Minimum wall thickness:** 1.2 mm (hydrostatic pressure resistance)
- **Overhang limit:** All internal helical structures must be self-supporting (< 50 degrees from vertical / Z-axis) — no internal support material allowed
- **Parameterized dimensions:** Bowl diameter ~200 mm, stem OD ~25 mm, height ~160 mm (all dimensions must remain fully parametric)

## Key Design Parameters (Helical Vanes)

The geometry generator exposes these as tunable parameters for the optimization loop:

- Number of fins
- Start / end height along funnel axis
- Sweep angle
- Pitch
- Radial depth start / end (vane depth taper along height)
- Blade angle (tip angular offset from radial — controls tangential deflection)
- Throat curvature (independent contraction control near stem exit)

## OpenFOAM Case Structure

The `cfd/base_case/` directory follows standard OpenFOAM layout:
- `0/` — Initial/boundary conditions (alpha.water, U, p_rgh) — **must remain uniform templates**
- `constant/` — Physical properties (transportProperties, turbulenceProperties, g)
- `system/` — Solver and mesh controls (controlDict, fvSchemes, fvSolution, blockMeshDict, snappyHexMeshDict, setFieldsDict, decomposeParDict, surfaceFeatureExtractDict)

Patches: `top`, `bottom`, `sides` (atmosphere), `funnel` (wall).
Boundary conditions: atmosphere patches use totalPressure/pressureInletOutletVelocity/inletOutlet; funnel wall uses noSlip/fixedFluxPressure/zeroGradient.

## CFD via Docker

All OpenFOAM commands execute inside Docker containers. The runner.py handles this automatically:

```bash
docker run --rm --shm-size=4g -v <case_dir>:/work opencfd/openfoam-default:2406 bash -c "cd /work && <command>"
```

Key gotchas:
- STL is generated in mm but OpenFOAM works in metres — `surfaceTransformPoints` scales in the pipeline
- Docker creates root-owned files — use Docker to clean them
- MPI needs `--allow-run-as-root --oversubscribe --shm-size=4g`

## Multi-Fidelity Optimization Strategy

The optimisation uses a three-stage approach to balance speed and accuracy:

1. **Stage 1 — Coarse screening** (20 iterations): blockMesh 15x15x12, snappyHexMesh refinement (1,2). ~5 min/iter. Fast exploration with GP surrogate.
2. **Stage 2 — Fine validation** (top 5): blockMesh 30x30x24, refinement (2,3). ~60 min/iter. High-fidelity confirmation.
3. **Stage 3 — Fine refinement** (5 iterations): Fine mesh with GP enriched by Stage 2 data.

Constraint violations are detected before CFD (penalty = -2, zero compute cost).

## Best Known Design

Iter 37 from optimisation (fine-mesh validated):
- **8 fins**, 144 deg sweep, -30 deg blade angle
- radial_depth 0.20 -> 0.26 taper, 2.1mm thickness
- Fins at z=32-60mm
- **Air core: 17mm** (77% of 22mm stem bore)
- **Throat velocity: 2.5 m/s**
- **Reward: 0.872** (fine mesh)
- Files: `output/best_design/funnel.stl`, `output/best_design/funnel.step`

## Execution Phases

### Phase 1 — Geometry [COMPLETE]
`geometry/funnel_generator.py`: parametric bowl, convergent neck, stem, and internal helical fins. Manufacturing constraint validation. Output STEP + STL via CadQuery.

### Phase 1.5 — Mesh Validation [COMPLETE]
STL watertight (0 boundary/non-manifold edges). snappyHexMesh generates 629K cells. checkMesh: max non-ortho 54.8 deg, 2 skew faces (acceptable).

### Phase 2 — CFD Setup [COMPLETE]
Full interFoam VOF case: transportProperties (water/air), turbulenceProperties (laminar), g, boundary conditions, adaptive time stepping (maxCo=0.5, maxAlphaCo=0.25). Solver converges stably.

### Phase 2.5 — Asymmetric Air Intake (Planned)
Bowl rim guide structures that break rotational symmetry to improve air return during pouring:
- **Pour spout:** localized rim depression directing the liquid stream, allowing air backflow on the opposite side.
- **Air return channel:** shallow groove or raised guide on inner bowl wall providing a dedicated low-resistance path for air ingress.

New parameters: spout angular position, spout depth, channel width, channel depth. Requires boolean operations on the revolved shell body and possible CFD boundary condition changes (non-axisymmetric inlet). Deferred until Phase 2 CFD results confirm whether symmetric vane designs leave residual glugging.

### Phase 3 — Pipeline Integration [COMPLETE]
`cfd/runner.py`: end-to-end Docker OpenFOAM pipeline. Supports `coarse`/`fine` mesh levels. Returns fitness metrics dict.

### Phase 4 — Optimization Loop [COMPLETE]
`optimization/loop.py`: multi-fidelity Bayesian optimization with GP surrogate. 10-dimension search space, constraint-aware reward function, three-stage coarse/fine strategy. Supports `--resume`.

### Phase 5 — Future Work
- Phase 2.5 asymmetric air intake (if CFD shows residual glugging)
- GPU-accelerated CFD (AmgX/PETSc for pressure solver)
- Longer simulation times (0.5-1.0s) for steady-state flow analysis
- Physical prototype 3D printing and experimental validation
