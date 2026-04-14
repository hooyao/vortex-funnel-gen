"""
Main optimization controller.

Drives the parametric design loop:
  1. Generate geometry parameters
  2. Build funnel via CadQuery
  3. Run CFD simulation via OpenFOAM
  4. Parse fitness metrics (air-core diameter, flow velocity)
  5. Select next parameter set and repeat
"""
