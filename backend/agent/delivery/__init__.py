"""Phase 9: award-to-delivery.

Converts an award into a grant, a project and its obligations - deriving every figure
from the authorised submission package rather than re-entering it, because the brief's
exit criterion for this phase is that no data already approved in the application is
keyed in twice.
"""
from agent.delivery.service import (  # noqa: F401
    Deadline,
    DeliveryError,
    DeliveryService,
    HandoverResult,
    NotDeliverable,
)
