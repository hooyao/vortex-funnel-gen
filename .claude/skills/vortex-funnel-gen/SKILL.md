---
name: vortex-funnel-gen
description: Automated design optimization of a 3D-printable vortex funnel with internal helical vanes. Drives a full pipeline — parametric CadQuery geometry, OpenFOAM CFD simulation (interFoam VOF multiphase via Docker), Bayesian optimization loop — to produce an optimal anti-glugging funnel design. Use this skill whenever the user asks to design, optimize, simulate, or 3D-print a fluid funnel; reproduce the best vortex funnel design; iterate on funnel vane parameters; run CFD on funnel geometry; or anything related to funnel flow optimization, air-core formation, or anti-glugging funnel design.
---

# Vortex Funnel Generator

Automated agentic optimization of a 3D-printable fluid funnel (automotive windshield washer fluid). Internal helical vanes induce annular vortex flow, creating a stable central air core that eliminates glugging during pouring.

## Quick Reproduce: Best Design

To regenerate the proven optimal funnel without running the full optimization:

```bash
pip install -r requirements.txt
python geometry/funnel_generator.py --config output/best_design/params.json --output output/best_design
```

Outputs `funnel.stl` (3D-printable) and `funnel.step` (CAD) with the proven optimal parameters:
- 8 helical vanes, 144 deg sweep, -30 deg blade angle
- Radial depth 20%->26% taper, 2.1mm fin thickness
- Fins at z=32-60mm (lower convergent section)
- Result: 17mm air core (77% of stem bore), 2.5 m/s throat velocity

## Pipeline Architecture

```
optimization/loop.py  (Bayesian optimization controller)
    +-> geometry/funnel_generator.py   CadQuery: params -> STEP + STL
    +-> cfd/runner.py                  Docker OpenFOAM: STL -> mesh -> solve -> fitness
    |       +-- cfd/base_case/         OpenFOAM template (0/, constant/, system/)
    +-> output/optimisation/           Per-iteration results + history.json
```

## Run Full Optimization

### Prerequisites

1. Python 3.10+: `pip install -r requirements.txt`
2. Docker: `docker pull opencfd/openfoam-default:2406`
3. 16+ CPU cores recommended

### Commands

```bash
# 1. Generate baseline geometry
python geometry/funnel_generator.py --config params.json --output output

# 2. Single CFD case (test pipeline)
python cfd/runner.py --stl output/funnel.stl --case-dir cfd/run_001 --nprocs 24 --mesh-level coarse

# 3. Full multi-fidelity optimization (3 stages, ~10 hours)
python optimization/loop.py --coarse 20 --validate 5 --refine 5 --nprocs 24
```

### Three-Stage Strategy

- **Stage 1** — Coarse mesh screening (20 iters, ~5 min each): GP + EI acquisition, fast exploration
- **Stage 2** — Fine mesh validation (top 5): High-fidelity confirmation, data feeds back to GP
- **Stage 3** — Fine mesh refinement (5 iters): Targeted search near validated optima

### Reward Function

```
R = 0.50 * norm(air_core_diameter / 15mm)
  + 0.30 * norm(throat_velocity / 3.0 m/s)
  + 0.20 * air_fraction_throat
```

Constraint violations -> penalty=-2 (skip CFD). CFD failures -> penalty=-1.

## Search Space (10 dimensions)

| Parameter | Range | Controls |
|-----------|-------|----------|
| num_fins | 2-8 | Helical vane count |
| fin_start_z | 32-50 mm | Vane start height |
| fin_end_z | 60-120 mm | Vane end height |
| sweep_angle | 30-180 deg | Angular sweep per vane |
| fin_blade_angle | -30..30 deg | Tip angular offset |
| radial_depth_start | 0.15-0.60 | Vane depth at start |
| radial_depth_end | 0.15-0.60 | Vane depth at end |
| fin_thickness | 1.2-3.0 mm | Vane thickness |
| profile_k | 0.6-1.0 | Convergent curve shape |
| throat_k | -0.5..1.0 | Throat contraction |

## Key Lessons Learned

- **STL scaling**: CadQuery outputs mm, OpenFOAM needs metres -> `surfaceTransformPoints -scale '(0.001 0.001 0.001)'`
- **Template purity**: Keep `0/alpha.water` as `uniform 0` in base_case. Non-uniform data from a previous setFields run causes size mismatch crashes with different meshes.
- **Coarse for screening**: Coarse mesh (refinement 1,2) is 10x faster with reliable ranking. Fine mesh for final validation only.
- **profile_k >= 0.6**: Lower values produce wall overhang > 50 deg, wasting compute on infeasible designs.
- **boxToCell > cylinderToCell**: More robust for coarse meshes in setFieldsDict.
- **Docker ownership**: Files are root-owned. Clean with: `docker run --rm -v path:/d image bash -c "rm -rf /d/*"`
- **MPI in Docker**: Needs `--shm-size=4g --allow-run-as-root --oversubscribe`

## Manufacturing Constraints

- Material: PETG / ABS (FDM)
- Min wall: 1.2 mm | Max overhang: 50 deg from vertical
- Bowl: 200mm dia | Stem: 25mm OD | Height: 160mm (all parametric)
