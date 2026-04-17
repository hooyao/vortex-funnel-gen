# Vortex Funnel Generator

Automated agentic design optimisation of a 3D-printable fluid funnel (automotive washer fluid). Internal helical vanes induce annular vortex flow, creating a stable central air core that eliminates the "glugging" during pouring.

The pipeline is fully automated: **parametric CadQuery geometry → OpenFOAM CFD simulation → Bayesian optimisation**. Everything runs through a single command (or a single Claude Code conversation).

## What you get

A 3D-printable STL + STEP funnel whose vortex-inducing vanes have been discovered by a Bayesian optimiser, not hand-designed. The optimum is specific to your dimensions — the same pipeline re-runs for any bowl/stem/height combination.

**Reference results on the included 150 mm × 120 mm configuration** (bowl 150 mm outer, stem 22 mm inner bore, height 120 mm, wall 2 mm, PETG/ABS printable):
- 2 helical vanes · sweep 30° · blade −29° (aggressive attack angle)
- radial depth 0.58 → 0.15 (deep near bowl, shallow near stem)
- Air core: 15.6 mm (71 % of stem bore) · throat velocity: 1.49 m/s · air fraction: 50 %
- Reward 0.750 (fine-mesh validated)

See `output/journey_150mm_reveal.png` after running the pipeline — it tells the optimisation story from first attempt to champion.

## Reproduce with Claude Code (recommended path)

This repo ships a skill at `.claude/skills/vortex-funnel-gen/SKILL.md` that teaches Claude Code to drive the full pipeline.

### Prerequisites

| Tool | Why | How |
|------|-----|-----|
| Claude Code | to orchestrate everything | <https://claude.com/claude-code> |
| Docker | runs OpenFOAM in a container | Docker Desktop + WSL2 integration on Windows |
| Python 3.10+ | CadQuery, optimiser, visualisation | system Python is fine |
| 16+ CPU cores | MPI-parallel CFD | machine-dependent; adjust `--nprocs` if less |

### Workflow

```bash
git clone https://github.com/hooyao/vortex-funnel-gen.git
cd vortex-funnel-gen
pip install -r requirements.txt
docker pull opencfd/openfoam-default:2406   # one-time, ~2 GB
claude                                       # open Claude Code in the repo
```

Inside Claude Code, just ask for what you want — the skill triggers automatically:

- **"Reproduce the best funnel design"** — runs the full optimisation pipeline and outputs the winning STL/STEP.
- **"Design a 180 mm funnel with 30 mm spout"** — edits the config and runs a fresh optimisation for your dimensions.
- **"Run a quick coarse-mesh sanity check on the default params"** — single CFD case in ~5 minutes, no optimisation loop.
- **"Render the optimisation journey as images"** — produces the gallery + reveal plots.

Claude will walk through: validating constraints, pulling Docker images if missing, monitoring the multi-hour run, and reporting fitness metrics at each stage.

### Expected timing

| Task | Duration (24 CPU cores) |
|------|------------------------|
| Generate geometry from params | seconds |
| Single coarse-mesh CFD case | ~5 minutes |
| Single fine-mesh CFD case | ~30 minutes |
| Full optimisation (20 coarse + 5 fine-val + 5 refine) | **~5–10 hours** |

The optimiser supports `--resume` and saves after every iteration, so interruptions aren't catastrophic.

## Reproduce without Claude Code (direct CLI)

If you'd rather drive it yourself:

```bash
# 1. Install dependencies
pip install -r requirements.txt
docker pull opencfd/openfoam-default:2406

# 2. Generate geometry from the built-in 150 mm default config
#    (FunnelParams defaults — no JSON file needed)
python geometry/funnel_generator.py --output output

# 3. Single CFD case to verify the pipeline end-to-end (~5 min coarse mesh)
python cfd/runner.py --stl output/funnel.stl --case-dir cfd/run_001 \
                    --nprocs 24 --mesh-level coarse

# 4. Full multi-fidelity optimisation
python optimization/loop.py --coarse 20 --validate 5 --refine 5 --nprocs 24 \
                           --output output/optimisation

# 5. Resume after a crash / pause
python optimization/loop.py --coarse 20 --validate 5 --refine 5 --nprocs 24 \
                           --output output/optimisation --resume

# 6. Regenerate the champion STL at full precision after optimisation
#    (look up the winning iteration in output/optimisation/summary.json)
cp output/optimisation/iter_NNN/params.json output/best_design/params.json
python geometry/funnel_generator.py --config output/best_design/params.json \
                                    --output output/best_design
```

Results land in `output/optimisation/iter_NNN/` (per-iteration params + STL + CFD case). The overall winner is in `output/optimisation/summary.json`.

**To design for different dimensions** (e.g. 180 mm bowl): edit the defaults in `FunnelParams` (geometry/funnel_generator.py) or pass `--config your-params.json`. Also adjust `SEARCH_SPACE` in `optimization/loop.py` so `fin_start_z`/`fin_end_z` scale with the new `total_height`.

## What's in this repo

```
geometry/funnel_generator.py   CadQuery parametric generator → STEP + STL
cfd/
  runner.py                    Docker/OpenFOAM pipeline: mesh → solve → extract fitness
  base_case/                   interFoam VOF case template (0/, constant/, system/)
optimization/loop.py           Multi-fidelity Bayesian optimisation (scikit-optimize)
.claude/skills/                Skill that drives the pipeline from Claude Code
CLAUDE.md                      Architecture + tech stack + phase notes
```

Generated artefacts (STLs, CFD meshes, optimisation history) are **not** committed — they're reproduced from the code.

## How the optimisation works

**Reward** (higher is better, max 1.0):
```
R = 0.50 · norm(air_core_diameter / 15 mm)
  + 0.30 · norm(throat_velocity / 3.0 m/s)
  + 0.20 · air_fraction_throat
```

**Three-stage multi-fidelity strategy** — coarse mesh screens fast, fine mesh validates slowly, the same Gaussian Process model threads through all three stages so fine-mesh data sharpens the next round of suggestions:

1. **Stage 1 · Coarse screening** (20 iterations · ~5 min each) — random initial samples, then GP + Expected Improvement on a ~100 K-cell mesh. Infeasible designs (wall overhang > 50°, under-thickness, etc.) get penalty = −2 and skip CFD entirely.
2. **Stage 2 · Fine validation** (top 5 designs · ~30 min each) — re-run the best coarse candidates on a ~630 K-cell mesh; feed the accurate rewards back into the GP.
3. **Stage 3 · GP refine** (5 iterations · ~30 min each) — GP has been recalibrated by Stage 2, so new Expected-Improvement picks are based on high-fidelity data.

**Search space** (10 dimensions — the vane + throat knobs that actually matter):

| Parameter | Range | Controls |
|-----------|-------|----------|
| `num_fins` | 2–8 | Helical vane count |
| `fin_start_z` / `fin_end_z` | 24–40 / 42–90 mm | Vane extent |
| `sweep_angle` | 30–180° | Angular sweep per vane |
| `fin_blade_angle` | −30° to +30° | Tip offset from radial — how much the flow gets deflected tangentially |
| `radial_depth_start/end` | 0.15–0.60 | Vane depth fraction at start / end |
| `fin_thickness` | 1.2–3.0 mm | Vane thickness |
| `profile_k` | 0.6–1.0 | Convergent curve shape (linear → smoothstep) |
| `throat_k` | −0.5 to 1.0 | Throat contraction curvature |

**Manufacturing constraints** are enforced before CFD — parameters that can't be 3D-printed (walls < 1.2 mm, overhang > 50° without support, etc.) never waste compute:

- Material: PETG / ABS (FDM)
- Min wall: 1.2 mm · Max overhang: 50° from vertical
- Default dimensions: bowl 150 mm, stem 22 mm bore, height 120 mm (all parametric)

## Extending / modifying

- **Different dimensions** — edit `bowl_diameter`, `stem_od`, `total_height`, `wall_thickness` in `FunnelParams`; the search-space bounds for `fin_start_z`/`fin_end_z` in `optimization/loop.py` may need to scale with `total_height`.
- **Different reward** — `optimization/loop.py::compute_reward` is the only place to change the objective.
- **Additional constraints** — `geometry/funnel_generator.py::validate_params` is the single choke-point; any design that fails here costs zero CFD time.
- **Asymmetric bowl rim features** (pour spout, air return channel) — planned as Phase 2.5; see `CLAUDE.md`.
