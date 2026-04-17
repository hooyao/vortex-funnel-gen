"""
OpenFOAM simulation runner.

Copies the base_case template, injects the generated STL geometry,
executes blockMesh -> snappyHexMesh -> interFoam, and extracts
quantitative results (air volume fraction, flow velocity at throat).

All OpenFOAM commands run inside Docker (opencfd/openfoam-default:2406).
"""

import subprocess
import shutil
import json
import math
import re
import logging
import argparse
from pathlib import Path
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────

DOCKER_IMAGE = "opencfd/openfoam-default:2406"
DOCKER_SHM = "4g"
DEFAULT_NPROCS = 24
BASE_CASE_DIR = Path(__file__).parent / "base_case"

# Mesh fidelity presets: (blockMesh cells XY, blockMesh cells Z,
#                         snappy surface level min, snappy surface level max,
#                         snappy feature level)
MESH_PRESETS = {
    "coarse": (15, 12, 1, 2, 2),
    "fine":   (30, 24, 2, 3, 3),
}

# Throat Z-coordinate in metres (= stem_height in mm * 0.001)
# This is where we extract fitness metrics
THROAT_Z_M = 0.030
STEM_INNER_RADIUS_M = (25.0 / 2 - 1.5) * 0.001  # (stem_od/2 - wall) in m


# ── Docker helper ─────────────────────────────────────────────────────

def run_openfoam(case_dir: Path, cmd: str,
                 timeout: int = 600, label: str = "") -> subprocess.CompletedProcess:
    """Execute an OpenFOAM command inside a Docker container.

    The *case_dir* is bind-mounted at /work.  Returns the CompletedProcess;
    the caller should check .returncode.
    """
    docker_cmd = [
        "docker", "run", "--rm",
        f"--shm-size={DOCKER_SHM}",
        "-v", f"{case_dir.resolve()}:/work",
        DOCKER_IMAGE,
        "bash", "-c", f"cd /work && {cmd}",
    ]
    tag = label or cmd.split()[0]
    logger.info("[%s] %s", tag, cmd)
    result = subprocess.run(
        docker_cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        logger.error("[%s] FAILED (rc=%d)\n%s",
                     tag, result.returncode, result.stderr[-1000:])
    return result


# ── Pipeline stages ───────────────────────────────────────────────────

def setup_case(case_dir: Path, stl_path: Path) -> None:
    """Create a fresh case directory from the base_case template and inject STL."""
    if case_dir.exists():
        # Use Docker to remove (files may be root-owned from previous runs)
        subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{case_dir.resolve()}:/rm_target",
             DOCKER_IMAGE, "bash", "-c", "rm -rf /rm_target/*"],
            capture_output=True, timeout=60)
        shutil.rmtree(case_dir, ignore_errors=True)

    case_dir.mkdir(parents=True)

    # Copy system/ and 0/ directories
    shutil.copytree(BASE_CASE_DIR / "system", case_dir / "system")
    shutil.copytree(BASE_CASE_DIR / "0", case_dir / "0")

    # Copy constant/ config files only (not polyMesh/ or generated data)
    const_dir = case_dir / "constant"
    const_dir.mkdir()
    for name in ("g", "transportProperties", "turbulenceProperties"):
        src = BASE_CASE_DIR / "constant" / name
        if src.exists():
            shutil.copy2(src, const_dir / name)

    # Inject the STL
    tri_dir = const_dir / "triSurface"
    tri_dir.mkdir()
    shutil.copy2(stl_path, tri_dir / "funnel.stl")

    logger.info("Case set up at %s with STL from %s", case_dir, stl_path)


def _apply_mesh_level(case_dir: Path, mesh_level: str) -> None:
    """Patch blockMeshDict and snappyHexMeshDict for the given fidelity level."""
    cells_xy, cells_z, srf_min, srf_max, feat_lvl = MESH_PRESETS[mesh_level]

    # Patch blockMeshDict: replace cell counts
    bmd = case_dir / "system" / "blockMeshDict"
    text = bmd.read_text()
    text = re.sub(
        r'\(\s*\d+\s+\d+\s+\d+\s*\)\s*simpleGrading',
        f'({cells_xy} {cells_xy} {cells_z}) simpleGrading',
        text)
    bmd.write_text(text)

    # Patch snappyHexMeshDict: replace refinement levels
    shd = case_dir / "system" / "snappyHexMeshDict"
    text = shd.read_text()
    # Surface refinement: level (min max)
    text = re.sub(
        r'(refinementSurfaces\s*\{\s*funnel\s*\{\s*level\s*\()\s*\d+\s+\d+\s*(\))',
        rf'\g<1>{srf_min} {srf_max}\2', text)
    # Feature edge level
    text = re.sub(
        r'(file\s+"funnel\.eMesh"\s*;\s*level\s+)\d+',
        rf'\g<1>{feat_lvl}', text)
    shd.write_text(text)

    logger.info("Mesh level '%s': blockMesh %dx%dx%d, snappy surface (%d,%d), "
                "features %d", mesh_level, cells_xy, cells_xy, cells_z,
                srf_min, srf_max, feat_lvl)


def run_mesh(case_dir: Path, mesh_level: str = "fine") -> List[str]:
    """Scale STL → extract features → blockMesh → snappyHexMesh.

    Returns a list of error messages (empty = success).
    """
    _apply_mesh_level(case_dir, mesh_level)
    errors: List[str] = []

    steps = [
        ("scale", "surfaceTransformPoints -scale '(0.001 0.001 0.001)' "
                  "constant/triSurface/funnel.stl constant/triSurface/funnel.stl"),
        ("featureExtract", "surfaceFeatureExtract"),
        ("blockMesh", "blockMesh"),
        ("snappyHexMesh", "snappyHexMesh -overwrite"),
    ]
    for label, cmd in steps:
        r = run_openfoam(case_dir, cmd, timeout=600, label=label)
        if r.returncode != 0:
            errors.append(f"{label} failed (rc={r.returncode})")
            break

    return errors


def run_solver(case_dir: Path, n_procs: int = DEFAULT_NPROCS) -> List[str]:
    """setFields → (decompose →) interFoam (→ reconstruct).

    Returns a list of error messages (empty = success).
    """
    errors: List[str] = []

    r = run_openfoam(case_dir, "setFields", label="setFields")
    if r.returncode != 0:
        return ["setFields failed"]

    if n_procs > 1:
        r = run_openfoam(case_dir, "decomposePar", label="decomposePar")
        if r.returncode != 0:
            return ["decomposePar failed"]

        r = run_openfoam(
            case_dir,
            f"mpirun --allow-run-as-root --oversubscribe -np {n_procs} "
            f"interFoam -parallel > log.interFoam 2>&1",
            timeout=7200, label="interFoam")
        if r.returncode != 0:
            return ["interFoam failed — check log.interFoam"]

        r = run_openfoam(case_dir, "reconstructPar", timeout=600,
                         label="reconstructPar")
        if r.returncode != 0:
            return ["reconstructPar failed"]
    else:
        r = run_openfoam(
            case_dir, "interFoam > log.interFoam 2>&1",
            timeout=7200, label="interFoam")
        if r.returncode != 0:
            return ["interFoam failed — check log.interFoam"]

    return errors


def extract_fitness(case_dir: Path) -> Dict[str, Any]:
    """Extract fitness metrics from the final time step.

    Converts results to VTK, slices at the throat, and computes:
      - air_core_diameter   (mm)
      - throat_velocity     (m/s, mean of water phase)
      - air_fraction_throat (0-1)
    """
    import numpy as np

    # Convert to VTK
    r = run_openfoam(case_dir, "foamToVTK -latestTime", timeout=300,
                     label="foamToVTK")
    if r.returncode != 0:
        return {"success": False, "errors": ["foamToVTK failed"]}

    # Find the VTK internal mesh file
    vtk_dir = case_dir / "VTK"
    vtu_files = sorted(vtk_dir.rglob("internal.vtu"))
    if not vtu_files:
        return {"success": False, "errors": ["No VTK output found"]}

    try:
        import os
        os.environ['PYVISTA_OFF_SCREEN'] = 'true'
        import pyvista as pv
        pv.OFF_SCREEN = True

        mesh = pv.read(str(vtu_files[-1]))
        mesh = mesh.cell_data_to_point_data()

        # Slice at the throat
        throat = mesh.slice(normal='z', origin=(0, 0, THROAT_Z_M))
        if throat.n_points == 0:
            return {"success": False,
                    "errors": [f"Empty slice at z={THROAT_Z_M}"]}

        pts = throat.points
        alpha = throat['alpha.water']
        U = throat['U']

        # Filter to points inside the stem (within inner radius)
        r_xy = np.sqrt(pts[:, 0]**2 + pts[:, 1]**2)
        inside = r_xy <= STEM_INNER_RADIUS_M * 1.5  # slight margin
        if inside.sum() == 0:
            return {"success": False,
                    "errors": ["No points inside stem at throat"]}

        alpha_in = alpha[inside]
        U_in = U[inside]
        r_in = r_xy[inside]

        # Air fraction at throat
        air_mask = alpha_in < 0.5
        air_fraction = air_mask.sum() / max(len(alpha_in), 1)

        # Air-core diameter: approximate from the area fraction
        stem_area = math.pi * STEM_INNER_RADIUS_M**2
        air_area = air_fraction * stem_area
        air_core_diameter_m = 2.0 * math.sqrt(air_area / math.pi) if air_area > 0 else 0.0
        air_core_diameter_mm = air_core_diameter_m * 1000

        # Mean water velocity at throat
        water_mask = ~air_mask
        if water_mask.sum() > 0:
            U_water = U_in[water_mask]
            U_mag = np.linalg.norm(U_water, axis=1)
            throat_velocity = float(U_mag.mean())
        else:
            throat_velocity = 0.0

        return {
            "success": True,
            "air_core_diameter": round(air_core_diameter_mm, 2),
            "throat_velocity": round(throat_velocity, 4),
            "air_fraction_throat": round(float(air_fraction), 4),
        }

    except Exception as e:
        return {"success": False, "errors": [f"Fitness extraction: {e}"]}


# ── Top-level API ─────────────────────────────────────────────────────

def run_case(stl_path: Path, case_dir: Path,
             n_procs: int = DEFAULT_NPROCS,
             mesh_level: str = "fine") -> Dict[str, Any]:
    """Run the full CFD pipeline for one parameter set.

    1. Copy base_case template + inject STL
    2. Mesh: scale STL → feature extract → blockMesh → snappyHexMesh
    3. Solve: setFields → interFoam (parallel)
    4. Extract fitness metrics at the throat

    Args:
        mesh_level: "coarse" (~100K cells, fast) or "fine" (~630K cells, accurate)

    Returns a dict with 'success', fitness metrics, and any errors.
    """
    result: Dict[str, Any] = {"success": False, "case_dir": str(case_dir),
                               "mesh_level": mesh_level}

    # Stage 1: Setup
    try:
        setup_case(case_dir, stl_path)
    except Exception as e:
        result["errors"] = [f"Case setup failed: {e}"]
        return result

    # Stage 2: Mesh
    mesh_errors = run_mesh(case_dir, mesh_level=mesh_level)
    if mesh_errors:
        result["errors"] = mesh_errors
        return result

    # Stage 3: Solve
    solve_errors = run_solver(case_dir, n_procs)
    if solve_errors:
        result["errors"] = solve_errors
        return result

    # Stage 4: Extract fitness
    fitness = extract_fitness(case_dir)
    result.update(fitness)
    return result


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Run a single OpenFOAM CFD case for a funnel geometry")
    parser.add_argument("--stl", type=Path, required=True,
                        help="Path to funnel STL file (in mm)")
    parser.add_argument("--case-dir", type=Path, required=True,
                        help="Directory to set up and run the case in")
    parser.add_argument("--nprocs", type=int, default=DEFAULT_NPROCS,
                        help=f"Number of MPI processes (default: {DEFAULT_NPROCS})")
    parser.add_argument("--mesh-level", choices=("coarse", "fine"),
                        default="fine",
                        help="Mesh fidelity: coarse (~100K) or fine (~630K)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s: %(message)s")

    result = run_case(args.stl, args.case_dir, args.nprocs,
                      mesh_level=args.mesh_level)
    print(json.dumps(result, indent=2))

    if not result.get("success"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
