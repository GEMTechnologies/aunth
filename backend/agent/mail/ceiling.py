"""Phase 7b capability ceiling, outbound risk classes, and approval requirements.

The ceiling changes shape here, and the change is the point of the phase. Phase 7a
had one list of forbidden things. Phase 7b has something subtler to express:

    sending is permitted, but ONLY with a human approval of the exact bytes.

A boolean "may send" cannot express that, so a capability that needs approval
carries the requirement with it. `assert_capability(MAIL_SEND_HUMAN_APPROVED)`
raises unless the caller passes `human_approved=True`, which means the requirement
is checked at the same place every other capability is checked rather than in a
scattered `if approval:` that a future edit could drop.

**`MAIL_SEND` stays forbidden, permanently.** It is not "the one we will enable
later" - it is autonomous sending, and Phase 7c will still require a separate,
deliberate decision to use it. Phase 7b adds a *different* capability
(`MAIL_SEND_HUMAN_APPROVED`) so that "the mailbox may be read" can never quietly
become "the mailbox may speak" through a widened list.
"""

from __future__ import annotations

from enum import Enum


class Capability(str, Enum):
    # -- inbound (Phase 7a) -------------------------------------------------
    MAIL_RECEIVE = "MAIL_RECEIVE"
    MAIL_SYNC = "MAIL_SYNC"
    MAIL_READ = "MAIL_READ"
    MAIL_PARSE = "MAIL_PARSE"
    MAIL_CLASSIFY = "MAIL_CLASSIFY"
    MAIL_LINK = "MAIL_LINK"
    MAIL_ANALYZE = "MAIL_ANALYZE"
    MAIL_EXTRACT_TASKS = "MAIL_EXTRACT_TASKS"
    MAIL_DRAFT = "MAIL_DRAFT"
    MAIL_NOTIFY_INTERNAL = "MAIL_NOTIFY_INTERNAL"

    # -- outbound (Phase 7b) ------------------------------------------------
    MAIL_REQUEST_APPROVAL = "MAIL_REQUEST_APPROVAL"
    #: Sending is permitted ONLY with an approval of the exact fingerprint.
    MAIL_SEND_HUMAN_APPROVED = "MAIL_SEND_HUMAN_APPROVED"
    MAIL_RECONCILE_SEND = "MAIL_RECONCILE_SEND"
    #: Phase 7c: an unattended send, permitted only when an
    #: `AutonomousPolicy.evaluate` has cleared every gate for THIS message.
    #:
    #: Deliberately a different capability from `MAIL_SEND_AUTONOMOUS`, which stays
    #: forbidden forever. The distinction is the whole safety argument: this one
    #: cannot be reached by a caller asserting a boolean, because the flag it needs
    #: is produced by the policy engine evaluating a specific intent, not by a caller
    #: deciding it is allowed. `MAIL_SEND_AUTONOMOUS` remains what it always was - a
    #: name for the thing this platform does not do.
    MAIL_SEND_AUTONOMOUS_LOW_RISK = "MAIL_SEND_AUTONOMOUS_LOW_RISK"

    # -- forbidden, in this phase and by design ----------------------------
    MAIL_SEND = "MAIL_SEND"                       # autonomous sending
    MAIL_SEND_AUTONOMOUS = "MAIL_SEND_AUTONOMOUS"
    MAIL_AUTO_REPLY = "MAIL_AUTO_REPLY"
    MAIL_AUTO_FORWARD = "MAIL_AUTO_FORWARD"
    MAIL_AUTONOMOUS_FOLLOWUP = "MAIL_AUTONOMOUS_FOLLOWUP"
    MAIL_FORWARD_EXTERNAL = "MAIL_FORWARD_EXTERNAL"
    MAIL_AUTO_REPLY_EXTERNAL = "MAIL_AUTO_REPLY_EXTERNAL"

    # -- the standing platform ceiling, unchanged in 7b ---------------------
    APPLICATION_SUBMISSION = "APPLICATION_SUBMISSION"
    SUBMISSION = "SUBMISSION"
    CONTRACT_ACCEPT = "CONTRACT_ACCEPT"
    CONTRACT_ACCEPTANCE = "CONTRACT_ACCEPTANCE"
    AWARD_ACCEPT = "AWARD_ACCEPT"
    AWARD_ACCEPTANCE = "AWARD_ACCEPTANCE"
    FINANCIAL_COMMITMENT = "FINANCIAL_COMMITMENT"
    BANK_DETAILS_SEND = "BANK_DETAILS_SEND"
    BANK_DETAIL_CHANGE = "BANK_DETAIL_CHANGE"
    LEGAL_DECLARATION = "LEGAL_DECLARATION"
    CREDENTIAL_DISCLOSURE = "CREDENTIAL_DISCLOSURE"


#: Phase 7b: what a Granada agent may do with mail.
PHASE_7B_ALLOWED: frozenset[Capability] = frozenset({
    Capability.MAIL_RECEIVE,
    Capability.MAIL_SYNC,
    Capability.MAIL_READ,
    Capability.MAIL_PARSE,
    Capability.MAIL_CLASSIFY,
    Capability.MAIL_LINK,
    Capability.MAIL_ANALYZE,
    Capability.MAIL_EXTRACT_TASKS,
    Capability.MAIL_DRAFT,
    Capability.MAIL_NOTIFY_INTERNAL,
    Capability.MAIL_REQUEST_APPROVAL,
    Capability.MAIL_SEND_HUMAN_APPROVED,
    Capability.MAIL_RECONCILE_SEND,
    Capability.MAIL_SEND_AUTONOMOUS_LOW_RISK,
})

#: Requires an approval of the exact fingerprint, checked at the call site.
CAPABILITIES_REQUIRING_APPROVAL: frozenset[Capability] = frozenset({
    Capability.MAIL_SEND_HUMAN_APPROVED,
})

#: Capabilities that are available only when the policy engine has cleared them for
#: this specific message. A caller cannot assert these; it has to hold a decision.
CAPABILITIES_REQUIRING_POLICY: frozenset[Capability] = frozenset({
    Capability.MAIL_SEND_AUTONOMOUS_LOW_RISK,
})

PHASE_7B_FORBIDDEN: frozenset[Capability] = frozenset(
    set(Capability) - PHASE_7B_ALLOWED
)

#: Retained under the Phase 7a name so nothing silently loses its ceiling. The
#: inbound list is the 7a one; outbound capabilities are excluded from it on
#: purpose, because a test asserting "Phase 7a refused to send" must keep passing.
PHASE_7A_ALLOWED: frozenset[Capability] = frozenset({
    Capability.MAIL_RECEIVE, Capability.MAIL_SYNC, Capability.MAIL_READ,
    Capability.MAIL_PARSE, Capability.MAIL_CLASSIFY, Capability.MAIL_LINK,
    Capability.MAIL_ANALYZE, Capability.MAIL_EXTRACT_TASKS, Capability.MAIL_DRAFT,
    Capability.MAIL_NOTIFY_INTERNAL,
})
PHASE_7A_FORBIDDEN: frozenset[Capability] = frozenset(set(Capability) - PHASE_7A_ALLOWED)


class CapabilityRefused(PermissionError):
    """A capability outside the current ceiling was requested."""


class ExternalActionDisabled(CapabilityRefused):
    """An outbound or committing action was attempted without authority."""


class ApprovalRequired(CapabilityRefused):
    """The capability is available, but only with a human approval.

    A distinct type so a caller cannot treat "you need an approval" as "you are not
    allowed to do this at all" - the first is a workflow, the second is a wall.
    """


class PolicyRefused(CapabilityRefused):
    """An unattended send was attempted without a cleared policy decision."""


def assert_capability(
    capability: Capability,
    *,
    human_approved: bool = False,
    policy_cleared: bool = False,
) -> None:
    """Refuse anything outside the ceiling.

    **Deny by default.** The check is ``not in PHASE_7B_ALLOWED`` rather than ``in
    PHASE_7B_FORBIDDEN``, so a capability added to the enum later is *unavailable*
    rather than silently available.

    ``human_approved`` is required for every capability in
    ``CAPABILITIES_REQUIRING_APPROVAL``. That check lives here, with the others,
    rather than at each call site - so a new outbound entry point cannot forget it.
    """
    if capability not in PHASE_7B_ALLOWED:
        raise ExternalActionDisabled(
            f"{capability.value} is outside the Phase 7b ceiling. Granada may "
            "receive, understand, link, draft and send a human-approved message. "
            "It may not send autonomously, and it may not accept, commit or "
            "disclose."
        )
    if capability in CAPABILITIES_REQUIRING_APPROVAL and not human_approved:
        raise ApprovalRequired(
            f"{capability.value} requires an approval of the exact message "
            "fingerprint. Platform policy requires human approval for every "
            "outbound message, regardless of organisation autonomy."
        )
    if capability in CAPABILITIES_REQUIRING_POLICY and not policy_cleared:
        # `policy_cleared` is produced by `AutonomousPolicy.evaluate`, which checks
        # the platform switch, the organisation's opt-in, the autonomy level, the risk
        # class, the classification, the recipient's familiarity, the security flags
        # and the daily ceiling. A bare True from a caller is not that, and the only
        # durable proof is the AUTONOMOUS_POLICY approval row written alongside.
        raise PolicyRefused(
            f"{capability.value} requires a cleared autonomous policy decision for "
            "this specific message. Platform policy does not permit unattended "
            "sending on a caller's assertion."
        )


# ---------------------------------------------------------------------------
# Outbound risk
# ---------------------------------------------------------------------------
class OutboundRisk(str, Enum):
    """What kind of message this is, ordered by consequence.

    The classification exists to answer one question: **could sending this move
    money, accept an obligation, or hand over an access credential?** If yes, it is
    outside what a single ordinary approval may authorise, whatever the approver
    intended.
    """

    ROUTINE = "ROUTINE"
    APPLICATION_INFORMATION = "APPLICATION_INFORMATION"
    DOCUMENT_RESPONSE = "DOCUMENT_RESPONSE"
    DEADLINE_RESPONSE = "DEADLINE_RESPONSE"
    INTERVIEW_RESPONSE = "INTERVIEW_RESPONSE"
    AWARD_RELATED = "AWARD_RELATED"
    CONTRACT_RELATED = "CONTRACT_RELATED"
    FINANCIAL = "FINANCIAL"
    BANKING = "BANKING"
    LEGAL = "LEGAL"
    CREDENTIAL_SECURITY = "CREDENTIAL_SECURITY"
    OTHER_HIGH_RISK = "OTHER_HIGH_RISK"


#: **Not sendable through the normal Phase 7b approval path.**
#:
#: Even if a person clicks ordinary APPROVE. This is what makes "human approval" not
#: the platform's highest authority - a human can be mistaken, rushed, or acting on
#: a forged instruction, and the platform must not let one click accept a contract
#: or change banking details. A future phase may define a stronger workflow for
#: these; Phase 7b deliberately does not, because an under-designed high-risk path
#: is worse than none.
HIGH_RISK_CLASSES: frozenset[OutboundRisk] = frozenset({
    OutboundRisk.CONTRACT_RELATED,
    OutboundRisk.FINANCIAL,
    OutboundRisk.BANKING,
    OutboundRisk.LEGAL,
    OutboundRisk.CREDENTIAL_SECURITY,
})

#: Allowed through the ordinary approval path in Phase 7b.
SENDABLE_RISK_CLASSES: frozenset[OutboundRisk] = frozenset(
    set(OutboundRisk) - HIGH_RISK_CLASSES
)


class RiskRefused(PermissionError):
    """A high-risk message was refused even though it carried an approval.

    The refusal code the brief names.
    """

    code = "HIGH_RISK_ACTION_BLOCKED"
