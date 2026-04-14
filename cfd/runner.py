"""
OpenFOAM simulation runner.

Copies the base_case template, injects the generated STL geometry,
executes blockMesh -> snappyHexMesh -> interFoam, and extracts
quantitative results (air volume fraction, flow velocity at throat).
"""
