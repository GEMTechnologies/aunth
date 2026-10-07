"""Phase 8: submission packages, authorisation, handoff and filing.

Nothing in this package files an application. The only providers are fakes, the
handoff path performs no external action, and `APPLICATION_SUBMISSION` remains a
forbidden capability - so the guarantee that production submissions stay at zero is a
property of the code rather than of the configuration.
"""
from agent.submission.contract import (  # noqa: F401
    HandoffBundle,
    HandoffStep,
    SubmissionOutcome,
    SubmissionPayload,
    SubmissionProvider,
    SubmissionResult,
)
from agent.submission.service import (  # noqa: F401
    SubmissionService,
    package_fingerprint,
)
