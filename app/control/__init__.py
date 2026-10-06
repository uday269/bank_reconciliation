"""Control layer: permission, separation of duties, state transitions, period lock and
the six-condition completion rule (DOC-05 section 2, ADR-02).

One implementation per control rule, called by services. Each check either returns
or raises `ControlViolation`, which names the rule and its error code.
"""

from app.control.errors import ERROR_CODES, ControlViolation

__all__ = ["ControlViolation", "ERROR_CODES"]
