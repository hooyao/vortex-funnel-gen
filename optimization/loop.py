"""
Main optimization controller.

Drives a multi-fidelity Bayesian optimisation loop for funnel vane design:

  Stage 1 — Coarse-mesh screening (fast, ~5 min/iter)
            Random + GP-guided exploration over full parameter space.
  Stage 2 — Fine-mesh validation of top N designs (~45 min/iter)
            High-fidelity evaluation of the most promising candidates.
  Stage 3 — Refined search (fine mesh, ~45 min/iter)
            Feed fine-mesh data back into the GP model and run a few
            targeted iterations near the validated optima.

Reward function:
  R = w_d * norm(air_core_diameter) + w_v * norm(throat_velocity)
      + w_a * norm(air_fraction_throat) - penalty(constraints)

Constraints (hard):
  - All manufacturing constraints from FunnelParams.validate_params()
  - Wall thickness >= 1.2 mm, overhang < 50 deg, etc.
  - Constraint violations → large negative penalty, CFD skipped.
"""

import json
import logging
import argparse
import time
from pathlib import Path
from dataclasses import asdict
from typing import Dict, Any, List, Tuple, Optional

import numpy as np
from skopt import Optimizer
from skopt.space import Real, Integer

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from geometry.funnel_generator import FunnelParams, generate, validate_params
from cfd.runner import run_case

logger = logging.getLogger(__name__)

# ── Parameter search space ────────────────────────────────────────────

SEARCH_SPACE = [
    ("num_fins",            Integer(2, 8,          name="num_fins")),
    ("fin_start_z",         Real(24.0, 40.0,       name="fin_start_z")),
    ("fin_end_z",           Real(42.0, 90.0,       name="fin_end_z")),
    ("sweep_angle",         Real(30.0, 180.0,      name="sweep_angle")),
    ("fin_blade_angle",     Real(-30.0, 30.0,      name="fin_blade_angle")),
    ("radial_depth_start",  Real(0.15, 0.60,       name="radial_depth_start")),
    ("radial_depth_end",    Real(0.15, 0.60,       name="radial_depth_end")),
    ("fin_thickness",       Real(1.2, 3.0,         name="fin_thickness")),
    ("profile_k",           Real(0.6, 1.0,         name="profile_k")),
    ("throat_k",            Real(-0.5, 1.0,        name="throat_k")),
]

PARAM_NAMES = [name for name, _ in SEARCH_SPACE]
DIMENSIONS  = [dim  for _, dim  in SEARCH_SPACE]


def params_from_vector(x: list) -> FunnelParams:
    """Convert an optimiser suggestion vector into a FunnelParams object."""
    overrides = dict(zip(PARAM_NAMES, x))
    overrides["num_fins"] = int(overrides["num_fins"])
    return FunnelParams(**overrides)


# ── Reward & constraint functions ─────────────────────────────────────

# Reward weights
W_AIR_CORE = 0.50   # air core diameter — primary goal
W_VELOCITY = 0.30   # flow velocity — faster pour
W_AIR_FRAC = 0.20   # air fraction at throat

# Constraint penalty
CONSTRAINT_PENALTY = -2.0
CFD_FAILURE_PENALTY = -1.0


def compute_reward(metrics: Dict[str, Any]) -> float:
    """Compute scalar reward from CFD metrics. Higher = better.

    Components normalised to ~[0, 1]:
      - air_core_diameter:  0-22mm → 0-1 (15mm = ideal target)
      - throat_velocity:    0-5 m/s → 0-1 (3 m/s = good)
      - air_fraction_throat: 0-1 (already normalised)
    """
    d = metrics.get("air_core_diameter", 0.0)
    v = metrics.get("throat_velocity", 0.0)
    af = metrics.get("air_fraction_throat", 0.0)

    d_norm = min(d / 15.0, 1.0)
    v_norm = min(v / 3.0, 1.0)
    af_norm = af

    return W_AIR_CORE * d_norm + W_VELOCITY * v_norm + W_AIR_FRAC * af_norm


def check_constraints(params: FunnelParams) -> Tuple[bool, List[str]]:
    """Check all manufacturing and geometric constraints.

    Returns (feasible, list_of_violations).
    """
    errors, warnings = validate_params(params)
    feasible = len(errors) == 0
    return feasible, errors + warnings


# ── Single evaluation ─────────────────────────────────────────────────

def evaluate(x: list, iteration: int, output_root: Path,
             n_procs: int, mesh_level: str) -> Dict[str, Any]:
    """Evaluate one parameter set end-to-end.

    Returns a result dict with reward, metrics, feasibility, and timing.
    """
    params = params_from_vector(x)
    result = {
        "iteration": iteration,
        "params": asdict(params),
        "mesh_level": mesh_level,
        "feasible": False,
        "reward": CONSTRAINT_PENALTY,
    }

    # 1. Constraint check (free — no compute)
    feasible, violations = check_constraints(params)
    if not feasible:
        result["constraint_violations"] = violations
        logger.warning("Iter %d: INFEASIBLE — %s", iteration, violations)
        _save_result(output_root, iteration, result)
        return result

    if violations:  # warnings
        result["constraint_warnings"] = violations
        for w in violations:
            logger.warning("Iter %d: %s", iteration, w)

    iter_dir = output_root / f"iter_{iteration:03d}"
    iter_dir.mkdir(parents=True, exist_ok=True)

    # 2. Generate geometry
    logger.info("Iter %d: generating geometry ...", iteration)
    geo = generate(params, iter_dir)
    if not geo["success"]:
        result["errors"] = geo.get("errors", [])
        result["reward"] = CONSTRAINT_PENALTY
        logger.error("Iter %d: geometry failed — %s", iteration, result["errors"])
        _save_result(output_root, iteration, result)
        return result

    stl_path = Path(geo["stl"])

    # 3. Run CFD
    case_dir = iter_dir / "cfd_case"
    logger.info("Iter %d: running CFD [%s mesh, %d procs] ...",
                iteration, mesh_level, n_procs)
    t0 = time.time()
    cfd = run_case(stl_path, case_dir, n_procs=n_procs, mesh_level=mesh_level)
    elapsed = time.time() - t0

    if not cfd.get("success"):
        result["errors"] = cfd.get("errors", [])
        result["reward"] = CFD_FAILURE_PENALTY
        result["elapsed_s"] = round(elapsed, 1)
        logger.error("Iter %d: CFD failed in %.0fs — %s",
                     iteration, elapsed, result["errors"])
        _save_result(output_root, iteration, result)
        return result

    # 4. Compute reward
    reward = compute_reward(cfd)
    result.update({
        "feasible": True,
        "reward": round(reward, 4),
        "air_core_diameter": cfd["air_core_diameter"],
        "throat_velocity": cfd["throat_velocity"],
        "air_fraction_throat": cfd["air_fraction_throat"],
        "elapsed_s": round(elapsed, 1),
    })
    logger.info(
        "Iter %d: reward=%.4f  air_core=%.1fmm  velocity=%.3fm/s  "
        "air_frac=%.3f  [%s mesh, %.0fs]",
        iteration, reward, cfd["air_core_diameter"],
        cfd["throat_velocity"], cfd["air_fraction_throat"],
        mesh_level, elapsed)

    _save_result(output_root, iteration, result)
    return result


def _save_result(output_root: Path, iteration: int, result: Dict) -> None:
    """Persist a single iteration result."""
    iter_dir = output_root / f"iter_{iteration:03d}"
    iter_dir.mkdir(parents=True, exist_ok=True)
    with open(iter_dir / "result.json", "w") as f:
        json.dump(result, f, indent=2, default=str)


# ── Multi-fidelity optimisation loop ──────────────────────────────────

def run_optimisation(
    n_coarse: int = 20,
    n_fine_validate: int = 5,
    n_fine_refine: int = 5,
    n_initial: int = 5,
    n_procs: int = 24,
    output_root: Path = Path("output/optimisation"),
    resume: bool = False,
) -> Dict[str, Any]:
    """Three-stage multi-fidelity Bayesian optimisation.

    Stage 1: n_coarse iterations with coarse mesh (fast screening)
    Stage 2: re-evaluate top n_fine_validate with fine mesh (validation)
    Stage 3: n_fine_refine iterations with fine mesh (targeted refinement)

    All data feeds back into the same GP model, so fine-mesh results
    improve the surrogate's accuracy for Stage 3.
    """
    output_root.mkdir(parents=True, exist_ok=True)

    opt = Optimizer(
        dimensions=DIMENSIONS,
        base_estimator="GP",
        n_initial_points=n_initial,
        acq_func="EI",
        random_state=42,
    )

    all_results: List[Dict] = []
    history: List[Dict] = []
    iteration = 0

    # Resume support
    if resume:
        history, iteration = _load_history(output_root, opt)
        # Rebuild all_results from saved files
        for entry in history:
            rfile = output_root / f"iter_{entry['iteration']:03d}" / "result.json"
            if rfile.exists():
                with open(rfile) as f:
                    all_results.append(json.load(f))
        logger.info("Resumed: %d previous evaluations loaded (iter=%d)",
                     len(history), iteration)

    # Use absolute iteration boundaries so resume skips completed stages
    stage1_end = n_coarse
    stage2_end = n_coarse + n_fine_validate
    stage3_end = n_coarse + n_fine_validate + n_fine_refine

    # ── Stage 1: Coarse-mesh screening ────────────────────────────────
    if iteration < stage1_end:
        logger.info("=" * 60)
        logger.info("STAGE 1: Coarse-mesh screening (%d iterations)", n_coarse)
        logger.info("=" * 60)
    while iteration < stage1_end:
        x = opt.ask()
        result = evaluate(x, iteration, output_root, n_procs,
                          mesh_level="coarse")
        opt.tell(x, -result["reward"])  # skopt minimises

        history.append({"iteration": iteration, "x": [_serialise(v) for v in x],
                        "reward": result["reward"], "stage": 1,
                        "mesh_level": "coarse"})
        all_results.append(result)
        _save_history(output_root, history)
        iteration += 1

    # ── Stage 2: Fine-mesh validation of top N ────────────────────────
    if iteration < stage2_end:
        logger.info("=" * 60)
        logger.info("STAGE 2: Fine-mesh validation (top %d designs)",
                    n_fine_validate)
        logger.info("=" * 60)

        # Select top N feasible coarse-mesh results
        feasible = [r for r in all_results if r.get("feasible")
                     and r.get("mesh_level") == "coarse"]
        feasible.sort(key=lambda r: r["reward"], reverse=True)
        # Skip already-validated designs on resume
        n_s2_done = len([e for e in history if e.get("stage") == 2])
        top_n = feasible[:n_fine_validate]

        for rank, coarse_result in enumerate(top_n):
            if iteration >= stage2_end:
                break
            if rank < n_s2_done:
                continue  # already validated in previous run
            x = [coarse_result["params"][name] for name in PARAM_NAMES]
            logger.info("Validating rank #%d (coarse reward=%.4f)",
                        rank + 1, coarse_result["reward"])

            result = evaluate(x, iteration, output_root, n_procs,
                              mesh_level="fine")

            opt.tell(x, -result["reward"])

            history.append({"iteration": iteration,
                            "x": [_serialise(v) for v in x],
                            "reward": result["reward"], "stage": 2,
                            "mesh_level": "fine",
                            "validated_from": coarse_result["iteration"]})
            all_results.append(result)
            _save_history(output_root, history)
            iteration += 1

    # ── Stage 3: Refined search with enriched model ───────────────────
    if iteration < stage3_end:
        logger.info("=" * 60)
        logger.info("STAGE 3: Fine-mesh refined search (%d iterations)",
                    n_fine_refine)
        logger.info("=" * 60)
    while iteration < stage3_end:
        x = opt.ask()
        result = evaluate(x, iteration, output_root, n_procs,
                          mesh_level="fine")
        opt.tell(x, -result["reward"])

        history.append({"iteration": iteration, "x": [_serialise(v) for v in x],
                        "reward": result["reward"], "stage": 3,
                        "mesh_level": "fine"})
        all_results.append(result)
        _save_history(output_root, history)
        iteration += 1

    # ── Summary ───────────────────────────────────────────────────────
    best = max(all_results, key=lambda r: r["reward"])
    summary = {
        "best_iteration": best["iteration"],
        "best_reward": best["reward"],
        "best_params": best["params"],
        "best_metrics": {
            k: best.get(k) for k in
            ("air_core_diameter", "throat_velocity", "air_fraction_throat")
        },
        "best_mesh_level": best.get("mesh_level"),
        "total_iterations": len(all_results),
        "feasible_count": sum(1 for r in all_results if r.get("feasible")),
        "stages": {
            "coarse_screening": n_coarse,
            "fine_validation": n_fine_validate,
            "fine_refinement": n_fine_refine,
        },
    }
    with open(output_root / "summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)

    logger.info("=" * 60)
    logger.info("OPTIMISATION COMPLETE")
    logger.info("Best reward: %.4f at iteration %d (%s mesh)",
                best["reward"], best["iteration"], best.get("mesh_level"))
    logger.info("Best params: %s", json.dumps(best["params"], indent=2))
    logger.info("=" * 60)

    return summary


# ── Persistence ───────────────────────────────────────────────────────

def _serialise(v):
    """Make numpy types JSON-serialisable."""
    if hasattr(v, 'item'):
        return v.item()
    return v


def _load_history(output_root: Path, opt: Optimizer
                  ) -> Tuple[List[Dict], int]:
    """Load previous results and replay them into the optimiser."""
    history_file = output_root / "history.json"
    if not history_file.exists():
        return [], 0

    with open(history_file) as f:
        history = json.load(f)

    for entry in history:
        opt.tell(entry["x"], -entry["reward"])

    next_iter = max(e["iteration"] for e in history) + 1 if history else 0
    return history, next_iter


def _save_history(output_root: Path, history: List[Dict]) -> None:
    with open(output_root / "history.json", "w") as f:
        json.dump(history, f, indent=2, default=str)


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Multi-fidelity Bayesian optimisation of funnel vane geometry")
    parser.add_argument("--coarse", type=int, default=20,
                        help="Stage 1: coarse-mesh iterations (default: 20)")
    parser.add_argument("--validate", type=int, default=5,
                        help="Stage 2: fine-mesh validations of top N (default: 5)")
    parser.add_argument("--refine", type=int, default=5,
                        help="Stage 3: fine-mesh refined iterations (default: 5)")
    parser.add_argument("--initial", type=int, default=5,
                        help="Random initial samples (default: 5)")
    parser.add_argument("--nprocs", type=int, default=24,
                        help="MPI processes per CFD run (default: 24)")
    parser.add_argument("--output", type=Path,
                        default=Path("output/optimisation"),
                        help="Output directory")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from previous run")
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(args.output / "optimisation.log", mode="a"),
        ],
    )

    summary = run_optimisation(
        n_coarse=args.coarse,
        n_fine_validate=args.validate,
        n_fine_refine=args.refine,
        n_initial=args.initial,
        n_procs=args.nprocs,
        output_root=args.output,
        resume=args.resume,
    )

    print("\n" + json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
