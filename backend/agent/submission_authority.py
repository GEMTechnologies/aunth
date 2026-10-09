"""Whether Granada is allowed to submit an application externally. Fails closed.

WHY THIS IS SEPARATE FROM THE APPROVAL RECORD
---------------------------------------------
Granada already had almost all of this, and the inspection that found that is the reason this module
is small. `MailApproval` records *which fingerprint* a human authorised, append-only, with revocation.
`Workspace.REQUIRES_APPROVAL` refuses `WAITING_FOR_APPROVAL -> READY_TO_SUBMIT` without a named human
approver. `Workspace.REQUIRES_RECEIPT` refuses `SUBMITTING -> SUBMITTED` without a funder reference.

What none of those express is SCOPE: an organisation authorising document *preparation* has not
thereby authorised an external *submission*. `MailApproval` has no scope column because sending an
email is one action. Submission has at least two, and the directive is explicit:

    "Separate permission to prepare documents from permission to submit an external application."
    "A general account login must not automatically constitute permission to submit every application."

So this module answers one question - may Granada submit, for THIS organisation, THIS package
version, under THIS scope - and answers it from durable records rather than from a boolean.

THE BOOLEAN IT REPLACES
-----------------------
`high_risk_always_refused = True` currently refuses every submission unconditionally. That is safe,
and this module does not weaken it. But a hard-coded `True` is not an authorisation model: it cannot
distinguish "this organisation never granted authority" from "we have not built the feature yet", and
the moment it flips there must be something real behind it. This is that something.

FAILS CLOSED, ON EVERY PATH. An unknown scope, a missing record, an expired grant, a revoked grant,
an ambiguous one, a package whose fingerprint has changed since it was authorised, or an unreadable
clock all resolve to `allowed=False`. There is no default-allow branch, and `_deny` is the only
constructor used for anything the caller did not explicitly prove.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------
#: Assembling and validating documents. This is GRANTED BY DEFAULT: it reads the organisation's own
#: records and writes a package the organisation can inspect. Nothing leaves Granada.
SCOPE_PREPARE = "PREPARE"
#: Handing a package to an external party. GRANTED BY NOBODY BY DEFAULT. Requires an explicit,
#: unexpired, unrevoked grant naming this organisation.
SCOPE_SUBMIT = "SUBMIT"

ALL_SCOPES = frozenset({SCOPE_PREPARE, SCOPE_SUBMIT})

#: Scopes that are safe without an explicit grant, because they cannot cause an external effect.
#: Kept as an explicit allowlist rather than "anything that is not SUBMIT", so a future scope added
#: by someone else is denied until it is deliberately placed here.
DEFAULT_GRANTED_SCOPES = frozenset({SCOPE_PREPARE})

STATUS_ACTIVE = "ACTIVE"
STATUS_REVOKED = "REVOKED"


@dataclass(frozen=True)
class AuthorityGrant:
    """One durable grant of authority, mirroring `MailApproval`'s shape.

    A grant is append-only in the same sense: revoking sets `status`, and a new decision is a new
    row, so the history of who authorised what survives. That property is inherited deliberately -
    it is the reason `MailApproval` can answer "who allowed this?" years later.
    """

    org_id: str
    scope: str
    granted_by: str
    granted_at: datetime
    #: Optional. `None` means no expiry, which is a deliberate choice a human made by not setting one
    #: - it is NOT the same as unknown, and is therefore permitted to authorise.
    expires_at: Optional[datetime] = None
    status: str = STATUS_ACTIVE
    revoked_at: Optional[datetime] = None
    #: The membership, role or permission context at the moment of granting. A permission revoked
    #: later does not retroactively invalidate the grant, but the record must show what it was.
    permission_used: Optional[str] = None
    membership_id: Optional[str] = None
    #: Every gate and its result, so "why did Granada submit this?" is answerable from the record
    #: rather than from the code that happened to be deployed at the time. Same reason
    #: `MailApproval.policy_evidence` exists.
    policy_evidence: dict[str, Any] = field(default_factory=dict)
    grant_version: int = 1


@dataclass(frozen=True)
class AuthorityDecision:
    """The answer, with the evidence that produced it.

    `allowed` is never enough on its own: a refusal an operator cannot act on is a support ticket.
    """

    allowed: bool
    scope: str
    reason: str
    #: The grant that carried the decision, when one did. Absent on every refusal path that had no
    #: usable grant to point at.
    grant: Optional[AuthorityGrant] = None
    evidence: dict[str, Any] = field(default_factory=dict)


def _deny(scope: str, reason: str, **evidence: Any) -> AuthorityDecision:
    return AuthorityDecision(allowed=False, scope=scope, reason=reason, evidence=evidence)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC.

    A grant read back without timezone information must not be compared against an aware `now()`:
    that raises `TypeError`, and a caller that catches broadly would turn a crash into an allow. The
    comparison is made safe here rather than left to the caller.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def evaluate(
    *,
    org_id: str,
    scope: str,
    grants: list[AuthorityGrant],
    package_fingerprint: Optional[str] = None,
    authorised_fingerprint: Optional[str] = None,
    now: Optional[datetime] = None,
) -> AuthorityDecision:
    """May Granada perform `scope` for `org_id`?

    Every argument is keyword-only and there is no default that permits: a caller cannot reach an
    allow by omitting something.
    """
    if scope not in ALL_SCOPES:
        # An unrecognised scope is refused rather than treated as harmless. A typo in a scope string
        # must not become an authorisation.
        return _deny(scope, f"unknown scope {scope!r}; refusing rather than assuming")

    if not org_id:
        return _deny(scope, "no organisation given; authority is per-organisation and cannot be inferred")

    if scope in DEFAULT_GRANTED_SCOPES:
        # PREPARE reads the organisation's own records and produces an artefact they can inspect.
        # It still records why it was allowed, so the audit trail does not have a silent branch.
        return AuthorityDecision(
            allowed=True,
            scope=scope,
            reason=(
                "document preparation is authorised by default: it reads the organisation's own "
                "records and produces a package the organisation can inspect before anything leaves "
                "Granada"
            ),
            evidence={"default_granted": True},
        )

    # -- SCOPE_SUBMIT and anything else: require an explicit, usable grant --------------
    moment = _aware(now) if now else _now()

    matching = [g for g in grants if g.org_id == org_id and g.scope == scope]
    if not matching:
        return _deny(
            scope,
            f"{org_id} has granted no authority for {scope}; submission stays disabled until the "
            "organisation authorises it",
            grants_seen=len(grants),
        )

    usable: list[AuthorityGrant] = []
    refusals: dict[str, str] = {}
    for grant in matching:
        if grant.status != STATUS_ACTIVE:
            refusals[grant.granted_by] = f"grant status is {grant.status}"
            continue
        if not grant.granted_by:
            # An anonymous grant is not a grant. `Workspace.REQUIRES_APPROVAL` refuses an unnamed
            # approver for the same reason: authority has to attach to somebody.
            refusals[""] = "grant names no authorising person"
            continue
        if grant.expires_at is not None and _aware(grant.expires_at) <= moment:
            refusals[grant.granted_by] = f"grant expired at {grant.expires_at.isoformat()}"
            continue
        usable.append(grant)

    if not usable:
        return _deny(
            scope,
            f"{org_id} holds {len(matching)} grant(s) for {scope} and none is usable",
            refusals=refusals,
        )

    # -- the grant must cover THIS package version -------------------------------------
    # The reason `MailApproval` records a fingerprint rather than a boolean: "was the thing this
    # person authorised the thing we are about to send?" An authority granted while the package said
    # one thing must not silently cover a package that has since changed.
    if package_fingerprint is not None:
        if authorised_fingerprint is None:
            return _deny(
                scope,
                "the package version covered by the authority could not be established, so it "
                "cannot be shown that the authority covers the package about to be submitted",
                package_fingerprint=package_fingerprint,
            )
        if package_fingerprint != authorised_fingerprint:
            return _deny(
                scope,
                "the package has changed since authority was granted; the authority covers a "
                "different version of this application",
                expected=authorised_fingerprint,
                found=package_fingerprint,
            )

    oldest = min(usable, key=lambda g: _aware(g.granted_at))
    return AuthorityDecision(
        allowed=True,
        scope=scope,
        reason=f"{org_id} granted {scope} authority, active and unexpired",
        grant=oldest,
        evidence={
            "granted_by": oldest.granted_by,
            "granted_at": _aware(oldest.granted_at).isoformat(),
            "expires_at": _aware(oldest.expires_at).isoformat() if oldest.expires_at else None,
            "permission_used": oldest.permission_used,
            "usable_grants": len(usable),
        },
    )


# ---------------------------------------------------------------------------
# The policy gate a future submission must pass
# ---------------------------------------------------------------------------
#: The feature flag that must be on before any external submission is attempted. Disabled by
#: default, matching `autonomous_mail_enabled = False` - Granada's established way of shipping a
#: capability that is built, tested and switched off.
SUBMISSION_ENABLED_SETTING = "external_submission_enabled"


def submission_policy(
    *,
    org_id: str,
    grants: list[AuthorityGrant],
    package_fingerprint: Optional[str] = None,
    authorised_fingerprint: Optional[str] = None,
    settings: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> AuthorityDecision:
    """The single gate any external submission must pass. Refuses unless every condition holds.

    Deliberately ordered so the *cheapest and most absolute* refusals come first: a live submission
    must not depend on an organisation's paperwork being in order before we notice the capability is
    switched off.
    """
    settings = settings or {}

    # 1. The capability itself. Off by default; `admin.py` already reports `high_risk_always_refused`
    #    to clients, so a refusal here is consistent with what the product already tells people.
    if not settings.get(SUBMISSION_ENABLED_SETTING, False):
        return _deny(
            SCOPE_SUBMIT,
            "external submission is disabled by policy; Granada has not been authorised to submit "
            "to external parties",
            setting=SUBMISSION_ENABLED_SETTING,
        )

    # 2. The organisation's durable authority.
    decision = evaluate(
        org_id=org_id,
        scope=SCOPE_SUBMIT,
        grants=grants,
        package_fingerprint=package_fingerprint,
        authorised_fingerprint=authorised_fingerprint,
        now=now,
    )
    if not decision.allowed:
        return decision

    return AuthorityDecision(
        allowed=True,
        scope=SCOPE_SUBMIT,
        reason="external submission is enabled and the organisation holds unexpired authority",
        grant=decision.grant,
        evidence={**decision.evidence, "setting": SUBMISSION_ENABLED_SETTING},
    )


def describe() -> dict[str, Any]:
    """What this module does and does not do, for an operator or a reviewer.

    States the boundary explicitly because the surrounding system has a receipt requirement
    (`REQUIRES_RECEIPT`) and a readiness requirement that this module does NOT replace. Authority to
    submit is not evidence that a submission happened.
    """
    return {
        "scopes": sorted(ALL_SCOPES),
        "default_granted": sorted(DEFAULT_GRANTED_SCOPES),
        "setting": SUBMISSION_ENABLED_SETTING,
        "replaces": [],
        "does_not_replace": [
            "Workspace.REQUIRES_APPROVAL (a named human approver before READY_TO_SUBMIT)",
            "Workspace.REQUIRES_RECEIPT (an external receipt before SUBMITTED)",
            "readiness (a package that is not ready cannot be submitted)",
        ],
        "fails_closed": True,
        "granting_authority_is_not_submission": (
            "authority permits an attempt; only a confirmed external receipt makes an application "
            "SUBMITTED"
        ),
    }
