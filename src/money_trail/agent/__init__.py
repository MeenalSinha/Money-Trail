"""Phase 4: an AI investigation agent over the deterministic graph investigation engine.

The model chooses which registered tool to call next and writes commentary. Everything factual (records, patterns,
evidence, identifiers, amounts, counts) comes from FalkorDB through deterministic code. This package never imports
the evaluation package: no ground truth reaches the agent (enforced by a test).
"""
