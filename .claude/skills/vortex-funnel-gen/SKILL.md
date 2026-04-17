---
name: vortex-funnel-gen
description: Automated design optimization of a 3D-printable vortex funnel with internal helical vanes. Drives a full pipeline — parametric CadQuery geometry, OpenFOAM CFD simulation (interFoam VOF multiphase via Docker), Bayesian optimization loop — to produce an optimal anti-glugging funnel design. Use this skill whenever the user asks to design, optimize, simulate, or 3D-print a fluid funnel; reproduce the best vortex funnel design; iterate on funnel vane parameters; run CFD on funnel geometry; or anything related to funnel flow optimization, air-core formation, or anti-glugging funnel design.
---

# Vortex Funnel Generator

Automated agentic optimization of a 3D-printable fluid funnel (automotive windshield washer fluid). Internal helical vanes induce annular vortex flow, creating a stable central air core that eliminates glugging during pouring.

## Important: what ships vs what's generated

Only source code and OpenFOAM templates are committed. **Generated artefacts (STL, optimisation history, per-run params JSON) are gitignored** — they're all reproduced from the committed code. If the user asks to "reproduce the best design", you have two choices:

- **Run the full optimisation** (below). Takes 5–10 hours on 24 cores.
- **Generate baseline geometry only** using `FunnelParams` defaults from `geometry/funnel_generator.py` (no CFD). The defaults are the 150 mm × 120 mm configuration; this produces a valid printable funnel but with starter vane parameters, not the optimised ones.

Never assume `output/best_design/params.json` or root-level `params.json` exist after a fresh clone — they don't.

## Pipeline Architecture

```
optimization/loop.py  (Bayesian optimization controller)
    +-> geometry/funnel_generator.py   CadQuery: params -> STEP + STL
    +-> cfd/runner.py                  Docker OpenFOAM: STL -> mesh -> solve -> fitness
    |       +-- cfd/base_case/         OpenFOAM template (0/, constant/, system/)
    +-> output/optimisation/           Per-iteration results + history.json
```

## Prerequisites

1. Python 3.10+: `pip install -r requirements.txt`
2. Docker with working Linux integration: `docker pull opencfd/openfoam-default:2406` (~2 GB, one-time)
3. 16+ CPU cores recommended; adjust `--nprocs` downward if fewer.

## Reproduce from a fresh clone

### Step 1 — Generate baseline geometry (no CFD, seconds)

No `--config` flag is needed; the `FunnelParams` dataclass defaults to the 150 mm configuration.

```bash
python geometry/funnel_generator.py --output output
```

Produces `output/funnel.stl`, `output/funnel.step`, `output/params.json` with:
- Bowl diameter 150 mm, stem OD 26 mm (22 mm bore), height 120 mm, wall 2 mm
- Starter vane params (4 fins, sweep 60°, 0° blade, 0.35 depth) — not yet optimised

### Step 2 — Single CFD case as a pipeline smoke test (~5 min, coarse mesh)

```bash
python cfd/runner.py --stl output/funnel.stl --case-dir cfd/run_001 \
                    --nprocs 24 --mesh-level coarse
```

Returns JSON with `air_core_diameter`, `throat_velocity`, `air_fraction_throat`. If this succeeds, your Docker/OpenFOAM setup works and the pipeline is wired end-to-end.

### Step 3 — Full multi-fidelity Bayesian optimisation (~5–10 hours on 24 cores)

```bash
python optimization/loop.py --coarse 20 --validate 5 --refine 5 --nprocs 24 --output output/optimisation
```

Results land in `output/optimisation/iter_NNN/` with full provenance (params + STL + CFD case). Summary is written to `output/optimisation/summary.json` at the end.

Supports `--resume` to continue after crashes/pauses — reads `history.json` and picks up at the next uncompleted iteration.

### Step 4 — Regenerate the winning geometry at full resolution

After the loop finishes, the champion iteration is referenced in `summary.json`. To produce the final printable STL/STEP:

```bash
# Replace iter_NNN with the best iteration number from summary.json
cp output/optimisation/iter_NNN/params.json output/best_design/params.json
python geometry/funnel_generator.py --config output/best_design/params.json --output output/best_design
```

## Three-Stage Strategy

The optimiser runs coarse → fine → refine in one call:

- **Stage 1 — Coarse screening** (20 iters, ~100 K cells, ~5 min/iter): GP + Expected Improvement acquisition. Explores the 10-dim space fast.
- **Stage 2 — Fine validation** (top 5 from Stage 1, ~630 K cells, ~30 min/iter): high-fidelity confirmation. Results feed back into the same GP.
- **Stage 3 — Fine refinement** (5 GP-guided iters, fine mesh): model is now calibrated by real fine-mesh data, so EI picks are more accurate.

Coarse and fine reward values are **not comparable** — coarse tends to overestimate by ~10–15%. Always report the best *fine-mesh* reward as the champion, not the absolute max.

## Reward Function

```
R = 0.50 * norm(air_core_diameter / 15 mm)
  + 0.30 * norm(throat_velocity / 3.0 m/s)
  + 0.20 * air_fraction_throat
```

- Constraint violations (wall overhang, under-thickness, etc.) → reward = −2, CFD is skipped.
- CFD failure (solver diverged, scripting error) → reward = −1.
- Feasible designs give reward ∈ [0, 1].

## Search Space (10 dimensions, current defaults)

For the 150 mm × 120 mm default configuration (edit `optimization/loop.py::SEARCH_SPACE` if dimensions change):

| Parameter | Range | Controls |
|-----------|-------|----------|
| num_fins | 2–8 | Helical vane count |
| fin_start_z | 24–40 mm | Vane start height |
| fin_end_z | 42–90 mm | Vane end height |
| sweep_angle | 30–180° | Angular sweep per vane |
| fin_blade_angle | −30 to +30° | Tip offset from radial |
| radial_depth_start | 0.15–0.60 | Vane depth fraction at start |
| radial_depth_end | 0.15–0.60 | Vane depth fraction at end |
| fin_thickness | 1.2–3.0 mm | Vane thickness |
| profile_k | 0.6–1.0 | Convergent curve shape |
| throat_k | −0.5 to 1.0 | Throat contraction curvature |

## Expected reference results (sanity check)

For the 150 mm × 120 mm default, one complete optimisation run produced:

- Champion parameters: **2 fins**, sweep 30°, blade −29°, radial depth 0.58 → 0.15, fin thickness 2.8 mm
- Fine-mesh reward: **≈ 0.75**
- Air core 15.6 mm (71 % of 22 mm bore), throat velocity 1.49 m/s, air fraction 50 %

If a fresh run lands in a very different region (e.g. 8 fins, very high sweep), that's also plausible — Bayesian optimisation isn't deterministic across reseeds, and the space has multiple local optima. Consider it a different valid design, not a bug.

## Scaling to a different dimension

When the user asks for non-default dimensions (e.g. "design a 180 mm funnel"):

1. Edit `FunnelParams` defaults in `geometry/funnel_generator.py` *or* create a JSON config with `bowl_diameter`, `stem_od`, `total_height`, `wall_thickness`, `bowl_height`, `stem_height`.
2. Update search-space bounds in `optimization/loop.py::SEARCH_SPACE` — `fin_start_z` and `fin_end_z` need to scale with `total_height` (roughly 25 %–75 % of total height).
3. Update `constant/` settings only if fluid or gravity changes (usually not needed).
4. Re-run the full optimisation — the previous 150 mm history doesn't transfer.

## Key Lessons Learned

- **STL scaling**: CadQuery outputs mm, OpenFOAM needs metres — `surfaceTransformPoints -scale '(0.001 0.001 0.001)'` is applied inside `run_mesh`.
- **Template purity**: Keep `cfd/base_case/0/alpha.water` as `uniform 0`. If a previous `setFields` left non-uniform data there, subsequent runs with different meshes crash with a size mismatch.
- **Coarse for screening**: Coarse mesh (~100 K cells) is 10× faster with reliable *ranking*, even though absolute rewards differ from fine.
- **profile_k ≥ 0.6**: lower values produce convergent-wall overhang > 50°, wasting compute on infeasible designs.
- **boxToCell > cylinderToCell** in `setFieldsDict`: more robust for coarse meshes where the water-column cylinder's footprint may not contain enough cells.
- **Docker file ownership**: containers create root-owned files. Clean them via the container: `docker run --rm -v path:/d image bash -c "rm -rf /d/*"`.
- **MPI in Docker**: needs `--shm-size=4g --allow-run-as-root --oversubscribe` on the `docker run` invocation.
- **Resume logic**: `--resume` uses absolute iteration numbers against `n_coarse`/`n_fine_validate`/`n_fine_refine` to decide which stage to enter next. Don't run two optimisation processes on the same `--output` directory — state will corrupt.

## Manufacturing Constraints (hard-enforced before CFD)

- Material: PETG / ABS (FDM 3D printing)
- Min wall thickness: 1.2 mm
- Max overhang: 50° from vertical (no internal supports)
- Default dimensions: bowl 150 mm outer dia, stem 26 mm OD (22 mm bore), height 120 mm, wall 2 mm
- All dimensions are parametric — override via `FunnelParams` or JSON config.
