"""Live verification of generated API operations against FamilySearch BETA only.

A recipe calls generated methods through ``VerifyApi`` and proves the result by
read-back (with a negative control for writes). Only a recipe that passes, tears
down cleanly and actually called every operation it claims marks those operations
``verified`` in ``registry.json``. Run ``python -m tools.verify --help``.
"""
