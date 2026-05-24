"""Clean convertible-bond pricing core package.

Phase 1 intentionally keeps the package dependency-light: domain objects,
JSON loading, validation, and pricing use only the Python standard library.
"""

from cb_terminal.pricing.engine import PricingEngine

__all__ = ["PricingEngine"]
