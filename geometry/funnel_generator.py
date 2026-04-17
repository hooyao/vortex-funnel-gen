"""
Parametric funnel geometry generator using CadQuery.

Reads design parameters from a JSON config, generates a funnel body with
internal helical vanes, and exports STEP + STL files.

Geometry coordinate system:
  - Z axis = funnel axis (vertical), Z=0 at bottom of stem
  - Funnel widens upward: stem at bottom, bowl opening at top
"""

import cadquery as cq
import json
import math
import argparse
import logging
from dataclasses import dataclass, asdict, fields
from pathlib import Path
from typing import List, Dict, Any, Tuple

logger = logging.getLogger(__name__)


# ── Parameter schema ──────────────────────────────────────────────────

@dataclass
class FunnelParams:
    """All tunable funnel geometry parameters.

    Lengths in mm, angles in degrees.  Every field is exposed for the
    optimisation loop; manufacturing limits are enforced by validate_params().
    """

    # Overall dimensions
    bowl_diameter: float = 150.0       # top opening outer diameter
    stem_od: float = 26.0              # stem (spout) outer diameter (22mm bore + 2x2mm wall)
    total_height: float = 120.0        # overall funnel height
    wall_thickness: float = 2.0        # shell wall thickness

    # Profile shape
    bowl_height: float = 11.0          # straight rim section at top
    stem_height: float = 23.0          # straight stem section at bottom
    profile_k: float = 1.0             # convergent curve blend: 0=linear, 1=smoothstep
    throat_k: float = 0.0              # throat contraction: >0 wider, <0 sharper, 0=neutral

    # Helical vane parameters (optimisation targets)
    num_fins: int = 4                  # evenly-spaced fins
    fin_start_z: float = 35.0          # Z where fins begin (from bottom)
    fin_end_z: float = 70.0            # Z where fins end
    sweep_angle: float = 60.0          # angular sweep per fin (degrees)
    fin_blade_angle: float = 0.0       # tip angular offset from radial (degrees)
    radial_depth_start: float = 0.35   # vane depth fraction at fin_start_z
    radial_depth_end: float = 0.35     # vane depth fraction at fin_end_z
    fin_thickness: float = 1.5         # mm

    # Manufacturing constraints (hard limits, not tuned)
    min_wall_thickness: float = 1.2    # mm
    max_overhang_deg: float = 50.0     # degrees from vertical / Z-axis


def load_params(config_path: Path) -> FunnelParams:
    """Load parameters from JSON; missing keys fall back to defaults."""
    with open(config_path) as f:
        data = json.load(f)

    # Migrate deprecated radial_depth → radial_depth_start / _end
    if "radial_depth" in data and "radial_depth_start" not in data:
        logger.warning(
            "Deprecated 'radial_depth' in config; "
            "use 'radial_depth_start'/'radial_depth_end' instead")
        rd = data.pop("radial_depth")
        data.setdefault("radial_depth_start", rd)
        data.setdefault("radial_depth_end", rd)

    valid_keys = {fld.name for fld in fields(FunnelParams)}
    return FunnelParams(**{k: v for k, v in data.items() if k in valid_keys})


# ── Validation ────────────────────────────────────────────────────────

def validate_params(p: FunnelParams) -> Tuple[List[str], List[str]]:
    """Check manufacturing and geometric constraints.

    Returns (errors, warnings).  Errors are hard failures that prevent
    generation; warnings flag potential print-quality issues.
    """
    errors: List[str] = []
    warnings: List[str] = []

    # ---- hard constraints ----
    if p.wall_thickness < p.min_wall_thickness:
        errors.append(
            f"wall_thickness {p.wall_thickness} mm < min {p.min_wall_thickness} mm")
    if p.fin_thickness < p.min_wall_thickness:
        errors.append(
            f"fin_thickness {p.fin_thickness} mm < min {p.min_wall_thickness} mm")
    if p.stem_od / 2 <= p.wall_thickness:
        errors.append("stem too narrow: inner bore would be zero or negative")
    conv_h = p.total_height - p.bowl_height - p.stem_height
    if conv_h <= 0:
        errors.append("bowl_height + stem_height >= total_height (no convergent section)")
    if abs(p.throat_k) > 2.0:
        errors.append("throat_k magnitude exceeds 2.0 (extreme distortion)")

    if p.num_fins > 0:
        if p.fin_start_z >= p.fin_end_z:
            errors.append("fin_start_z must be < fin_end_z")
        if p.fin_start_z < 0 or p.fin_end_z > p.total_height:
            errors.append("fin Z range outside funnel height")
        if not (0 < p.radial_depth_start < 1):
            errors.append("radial_depth_start must be in (0, 1)")
        if not (0 < p.radial_depth_end < 1):
            errors.append("radial_depth_end must be in (0, 1)")
        if p.sweep_angle <= 0:
            errors.append("sweep_angle must be > 0")
        if abs(p.fin_blade_angle) > 45.0:
            errors.append("fin_blade_angle magnitude exceeds 45 degrees")

    # ---- convergent-wall overhang ----
    if conv_h > 0:
        max_slope = math.tan(math.radians(p.max_overhang_deg))
        bowl_r = p.bowl_diameter / 2
        stem_r = p.stem_od / 2
        n = 200
        for i in range(1, n + 1):
            t0 = (i - 1) / n
            t1 = i / n
            r0 = stem_r + (bowl_r - stem_r) * _profile_blend(t0, p.profile_k, p.throat_k)
            r1 = stem_r + (bowl_r - stem_r) * _profile_blend(t1, p.profile_k, p.throat_k)
            dz = conv_h / n
            slope = abs(r1 - r0) / dz
            if slope > max_slope:
                angle = math.degrees(math.atan(slope))
                errors.append(
                    f"convergent wall overhang {angle:.1f} deg > "
                    f"{p.max_overhang_deg} deg at t={t1:.3f}")
                break

    # ---- vane overhang (soft) ----
    if p.num_fins > 0 and p.fin_end_z > p.fin_start_z:
        fin_h = p.fin_end_z - p.fin_start_z
        sweep_rad = math.radians(p.sweep_angle)
        blade_rad = math.radians(p.fin_blade_angle)
        worst_angle = 0.0
        worst_z = p.fin_start_z
        for i in range(21):
            t = i / 20
            z = p.fin_start_z + t * fin_h
            depth = p.radial_depth_start + t * (p.radial_depth_end - p.radial_depth_start)
            r_mid = inner_radius_at_z(z, p) * (1.0 - depth / 2)
            tang = r_mid * (sweep_rad + abs(blade_rad)) / fin_h
            ang = math.degrees(math.atan(tang))
            if ang > worst_angle:
                worst_angle = ang
                worst_z = z
        if worst_angle > p.max_overhang_deg:
            warnings.append(
                f"vane overhang {worst_angle:.1f} deg > {p.max_overhang_deg} deg "
                f"at z={worst_z:.1f} mm — may need support or parameter adjustment")

    return errors, warnings


# ── Profile helpers ───────────────────────────────────────────────────

def _profile_blend(t: float, k: float, throat_k: float = 0.0) -> float:
    """Blend between linear (k=0) and smoothstep (k=1) interpolation.

    The smoothstep variant has zero first-derivative at both endpoints,
    giving a tangent-continuous junction with the straight bowl and stem
    sections.

    *throat_k* applies a power remap to *t* before blending:
      >0 → radius grows faster from the stem (wider throat),
      <0 → radius stays narrow longer (sharper contraction),
       0 → no change.
    """
    t = max(0.0, min(1.0, t))
    if throat_k != 0.0:
        denom = 1.0 + throat_k
        exponent = 1.0 / denom if abs(denom) > 0.1 else (5.0 if denom > 0 else 0.2)
        exponent = max(0.2, min(5.0, exponent))
        t = t ** exponent
    linear = t
    smooth = 3.0 * t * t - 2.0 * t * t * t
    return (1.0 - k) * linear + k * smooth


def outer_radius_at_z(z: float, p: FunnelParams) -> float:
    """Outer radius of funnel wall at height *z* (measured from bottom)."""
    bowl_r = p.bowl_diameter / 2
    stem_r = p.stem_od / 2
    conv_start = p.stem_height
    conv_end = p.total_height - p.bowl_height

    if z <= conv_start:
        return stem_r
    if z >= conv_end:
        return bowl_r
    t = (z - conv_start) / (conv_end - conv_start)
    return stem_r + (bowl_r - stem_r) * _profile_blend(t, p.profile_k, p.throat_k)


def inner_radius_at_z(z: float, p: FunnelParams) -> float:
    """Inner radius of funnel wall at height *z*."""
    return outer_radius_at_z(z, p) - p.wall_thickness


# ── Funnel shell ──────────────────────────────────────────────────────

def make_funnel_shell(p: FunnelParams) -> cq.Workplane:
    """Create the hollow funnel body by revolving a wall cross-section.

    The 2D profile lives on the XZ workplane (X = radius, local-Y = Z height).
    It is revolved 360 deg around the Z-axis (local-Y axis on XZ plane).
    """
    bowl_r = p.bowl_diameter / 2
    stem_r = p.stem_od / 2
    wt = p.wall_thickness
    conv_start = p.stem_height
    conv_end = p.total_height - p.bowl_height
    n_conv = 60  # sample points for convergent curve smoothness

    # ---- collect all profile vertices (clockwise closed polygon) ----
    pts: list[tuple[float, float]] = []

    # outer stem wall (up)
    pts.append((stem_r, 0.0))
    pts.append((stem_r, conv_start))

    # outer convergent (up, sampled)
    for i in range(1, n_conv + 1):
        t = i / n_conv
        z = conv_start + t * (conv_end - conv_start)
        pts.append((outer_radius_at_z(z, p), z))

    # outer bowl wall (up)
    pts.append((bowl_r, p.total_height))

    # top rim (outer → inner)
    pts.append((bowl_r - wt, p.total_height))

    # inner bowl wall (down)
    pts.append((bowl_r - wt, conv_end))

    # inner convergent (down, sampled)
    for i in range(n_conv - 1, -1, -1):
        t = i / n_conv
        z = conv_start + t * (conv_end - conv_start)
        pts.append((inner_radius_at_z(z, p), z))

    # inner stem wall (down)
    pts.append((stem_r - wt, 0.0))

    # bottom rim closes back to pts[0] via .close()

    # ---- build wire and revolve ----
    wp = cq.Workplane("XZ").moveTo(*pts[0])
    for pt in pts[1:]:
        wp = wp.lineTo(*pt)
    wp = wp.close()

    # revolve around the Z-axis (= local-Y axis on the XZ workplane)
    return wp.revolve(360, (0, 0), (0, 1))


# ── Helical vanes ─────────────────────────────────────────────────────

def make_single_vane(p: FunnelParams, vane_idx: int) -> cq.Shape:
    """Build one helical vane by lofting blade cross-sections (OCC ThruSections).

    Each cross-section is a thin quadrilateral at the correct height and
    angular position.  The outer edge penetrates 0.5 mm into the shell wall
    so that the subsequent boolean union fuses cleanly.
    """
    from OCP.gp import gp_Pnt
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeWire, BRepBuilderAPI_MakeEdge
    from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections

    n_sections = 30
    wall_overlap = 0.5  # mm — embed outer edge into shell wall
    angle_offset = vane_idx * 360.0 / p.num_fins
    fin_h = p.fin_end_z - p.fin_start_z

    lofter = BRepOffsetAPI_ThruSections(True, False)  # solid, not ruled

    for i in range(n_sections + 1):
        t = i / n_sections
        z = p.fin_start_z + t * fin_h
        theta = math.radians(angle_offset + t * p.sweep_angle)

        r_wall = inner_radius_at_z(z, p) + wall_overlap
        depth = p.radial_depth_start + t * (p.radial_depth_end - p.radial_depth_start)
        r_tip = inner_radius_at_z(z, p) * (1.0 - depth)

        # angular half-widths (preserve fin_thickness in mm at both radii)
        half_a_wall = p.fin_thickness / (2.0 * r_wall)
        half_a_tip = p.fin_thickness / (2.0 * r_tip)

        # wall edge at theta; tip edge offset by blade angle
        theta_tip = theta + math.radians(p.fin_blade_angle)

        cos_wp = math.cos(theta + half_a_wall)
        sin_wp = math.sin(theta + half_a_wall)
        cos_wm = math.cos(theta - half_a_wall)
        sin_wm = math.sin(theta - half_a_wall)
        cos_tp = math.cos(theta_tip + half_a_tip)
        sin_tp = math.sin(theta_tip + half_a_tip)
        cos_tm = math.cos(theta_tip - half_a_tip)
        sin_tm = math.sin(theta_tip - half_a_tip)

        corners = [
            gp_Pnt(r_wall * cos_wm, r_wall * sin_wm, z),   # wall trailing
            gp_Pnt(r_wall * cos_wp, r_wall * sin_wp, z),   # wall leading
            gp_Pnt(r_tip  * cos_tp, r_tip  * sin_tp, z),   # tip leading
            gp_Pnt(r_tip  * cos_tm, r_tip  * sin_tm, z),   # tip trailing
        ]

        wire = BRepBuilderAPI_MakeWire()
        for j in range(4):
            edge = BRepBuilderAPI_MakeEdge(
                corners[j], corners[(j + 1) % 4]).Edge()
            wire.Add(edge)

        lofter.AddWire(wire.Wire())

    lofter.Build()
    if not lofter.IsDone():
        raise RuntimeError(f"OCC ThruSections failed for vane {vane_idx}")

    return cq.Shape.cast(lofter.Shape())


# ── Assembly & export ─────────────────────────────────────────────────

def generate(params: FunnelParams, output_dir: Path) -> Dict[str, Any]:
    """Build complete funnel geometry and export STEP + STL.

    Returns a result dict with success status, file paths, and diagnostics.
    """
    errors, warnings = validate_params(params)
    if errors:
        return {"success": False, "errors": errors, "warnings": warnings}
    for w in warnings:
        logger.warning(w)

    # Funnel shell
    logger.info("Building funnel shell ...")
    result = make_funnel_shell(params)

    # Helical vanes
    if params.num_fins > 0:
        logger.info("Building %d helical vanes ...", params.num_fins)
        for i in range(params.num_fins):
            logger.info("  vane %d/%d", i + 1, params.num_fins)
            vane = make_single_vane(params, i)
            result = result.union(cq.Workplane("XY").newObject([vane]))

    # Export
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    step_path = output_dir / "funnel.step"
    stl_path = output_dir / "funnel.stl"
    params_path = output_dir / "params.json"

    logger.info("Exporting STEP -> %s", step_path)
    cq.exporters.export(result, str(step_path))

    logger.info("Exporting STL  -> %s", stl_path)
    cq.exporters.export(result, str(stl_path), exportType="STL",
                        tolerance=0.05, angularTolerance=0.1)

    with open(params_path, "w") as f:
        json.dump(asdict(params), f, indent=2)

    return {
        "success": True,
        "step": str(step_path),
        "stl": str(stl_path),
        "params": str(params_path),
        "warnings": warnings,
    }


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate parametric funnel geometry with helical vanes")
    parser.add_argument("--config", type=Path, default=None,
                        help="JSON parameter file (defaults used if omitted)")
    parser.add_argument("--output", type=Path, default=Path("output"),
                        help="Output directory (default: output/)")
    parser.add_argument("--validate-only", action="store_true",
                        help="Validate parameters without generating geometry")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s: %(message)s")

    params = load_params(args.config) if args.config else FunnelParams()
    logger.info("Parameters: %s",
                json.dumps(asdict(params), indent=2))

    errors, warnings = validate_params(params)
    for e in errors:
        logger.error("CONSTRAINT VIOLATION: %s", e)
    for w in warnings:
        logger.warning(w)

    if args.validate_only:
        raise SystemExit(1 if errors else 0)

    if errors:
        logger.error("Fix constraint violations before generating geometry")
        raise SystemExit(1)

    out = generate(params, args.output)
    if out["success"]:
        logger.info("STEP: %s", out["step"])
        logger.info("STL:  %s", out["stl"])
        logger.info("Done.")
    else:
        for e in out["errors"]:
            logger.error(e)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
