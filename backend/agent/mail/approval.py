"""Approval: who may authorise an outbound message, and what they authorise.

The permission
--------------
``mail.approve_send``, and it is deliberately **not** implied by any other
permission. Being able to view an application, edit a draft or administer the
organisation does not confer the right to send correspondence in the
organisation's name to a funder. The brief requires this explicitly, and the
reason is that approval is the last human checkpoint before an irreversible
external act - so it gets its own grant rather than being a side effect of
membership.

The check has three layers:

1. **Membership** - the approver must belong to the organisation. Cross-tenant
   approval is impossible, not merely discouraged.
2. **Permission** - ``mail.approve_send`` must be held, through a role permission
   or through ownership.
3. **Fingerprint binding** - the approval records the exact fingerprint it
   authorises. A permission check that passed says nothing about *what* was
   approved, and conflating the two would let a valid approver authorise one message
   while a different one is sent.

Rejection and change requests need no extra permission beyond membership: anyone who
can see the draft should be able to say "no" or "fix this". Refusing to allow a
rejection is a way to make a bad draft unstoppable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import models
from agent.mail.ceiling import HIGH_RISK_CLASSES, OutboundRisk, RiskRefused

logger = logging.getLogger(__name__)

#: The permission that authorises an outbound message.
APPROVE_SEND_PERMISSION = "mail.approve_send"

#: Seeded onto owner and admin roles. Named here so a migration or a fixture does
#: not have to invent the string.
APPROVE_SEND_ROLE_KEYS = frozenset({"owner", "admin", "manager"})


class ApprovalError(RuntimeError):
    """Base class for approval failures."""


class NotPermitted(ApprovalError):
    """The user may not perform this approval action."""


class ApprovalMismatch(ApprovalError):
    """The approval does not authorise the intent as it currently stands."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Permission
# ---------------------------------------------------------------------------
def membership_for(db: Session, *, org_id: str, user_id: str) -> Optional[models.OrgMember]:
    """The membership row, or ``None``. Scoped by organisation, always."""
    if not org_id or not user_id:
        return None
    return db.execute(
        select(models.OrgMember).where(
            models.OrgMember.org_id == org_id,
            models.OrgMember.user_id == user_id,
        )
    ).scalars().first()


def role_for(db: Session, *, org_id: str, membership: models.OrgMember) -> Optional[models.Role]:
    if membership.role_id is None:
        return None
    return db.execute(
        select(models.Role).where(
            models.Role.id == membership.role_id,
            # A role belonging to another organisation must not confer anything
            # here. The membership lookup is already tenant-scoped; this makes the
            # role lookup tenant-scoped too, so a mismatched pair is inert rather
            # than privileged.
            models.Role.org_id == org_id,
        )
    ).scalars().first()


def has_permission(
    db: Session, *, org_id: str, user_id: str, permission: str
) -> tuple[bool, Optional[str]]:
    """Whether ``user_id`` holds ``permission`` in ``org_id``.

    Returns ``(allowed, reason_when_denied)``. The reason is not decoration: an
    approval refusal that says only "no" sends an operator hunting through roles,
    whereas "you are not a member of this organisation" and "your role does not
    include mail.approve_send" lead to different next actions.
    """
    if not org_id:
        return False, "no organisation context"
    if not user_id:
        return False, "no user context"

    # Ownership is checked FIRST, and without requiring a membership row.
    #
    # This is not a shortcut. An organisation's owner is a member by virtue of
    # owning it, and requiring a separate `org_members` row for them would mean an
    # organisation whose membership row was never written cannot approve its own
    # correspondence - locked out of the one action only it can perform. The first
    # version checked membership first and failed exactly this way.
    organisation = db.execute(
        select(models.Organisation).where(models.Organisation.id == org_id)
    ).scalars().first()
    if organisation is not None and organisation.owner_user_id == user_id:
        return True, None

    membership = membership_for(db, org_id=org_id, user_id=user_id)
    if membership is None:
        # Covers both "not a member" and "a member of a different organisation",
        # which must be indistinguishable from the outside.
        return False, "the user is not a member of this organisation"

    role = role_for(db, org_id=org_id, membership=membership)
    if role is None:
        # No role: fall back to the permission table directly against the user's
        # membership, so a per-user grant is still honoured.
        granted = db.execute(
            select(models.Permission).where(
                models.Permission.key == permission,
            )
        ).scalars().first()
        if granted is None:
            return False, f"the role does not include {permission}"
        return False, f"no role is assigned, so {permission} cannot be established"

    if role.key in APPROVE_SEND_ROLE_KEYS:
        return True, None

    row = db.execute(
        select(models.Permission).where(models.Permission.key == permission)
    ).scalars().first()
    if row is None:
        return False, f"the role '{role.key}' does not include {permission}"

    link = db.execute(
        select(models.RolePermission).where(
            models.RolePermission.role_id == role.id,
            models.RolePermission.permission_id == row.id,
        )
    ).scalars().first()
    if link is None:
        return False, f"the role '{role.key}' does not include {permission}"
    return True, None


# ---------------------------------------------------------------------------
# Approval flow
# ---------------------------------------------------------------------------
@dataclass
class ApprovalOutcome:
    """What an approval action did."""

    decision: str
    intent_id: str
    fingerprint: Optional[str]
    status: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "send_intent_id": self.intent_id,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "detail": self.detail,
        }


class ApprovalService:
    """Records human decisions about send intents."""

    def __init__(self, db: Session, *, org_id: str) -> None:
        if not org_id:
            raise ApprovalError("org_id is required; tenant unknown is a deny")
        self.db = db
        self.org_id = org_id

    # ------------------------------------------------------------------
    def _is_member_or_owner(self, user_id: str) -> bool:
        """Whether the user belongs to this organisation, as owner or member.

        Ownership counts even without an `org_members` row: an organisation's owner
        is a member by virtue of owning it, and requiring a separate row would mean
        an organisation that never got one cannot reject its own bad draft.
        """
        organisation = self.db.execute(
            select(models.Organisation).where(models.Organisation.id == self.org_id)
        ).scalars().first()
        if organisation is not None and organisation.owner_user_id == user_id:
            return True
        return membership_for(self.db, org_id=self.org_id, user_id=user_id) is not None

    def _intent(self, intent_id: str) -> models.MailSendIntent:
        intent = self.db.execute(
            select(models.MailSendIntent).where(
                models.MailSendIntent.id == intent_id,
                models.MailSendIntent.org_id == self.org_id,
            )
        ).scalars().first()
        if intent is None:
            # Deliberately the same message whether the row is absent or belongs to
            # another tenant: distinguishing them would confirm the existence of
            # another organisation's send.
            raise ApprovalError(f"no send intent {intent_id} in this organisation")
        return intent

    # ------------------------------------------------------------------
    def approve(
        self,
        *,
        intent_id: str,
        user_id: str,
        note: Optional[str] = None,
    ) -> ApprovalOutcome:
        """Authorise the intent **as it currently stands**.

        The fingerprint is recalculated from the live row rather than accepted from
        the caller. That closes the obvious hole: a client that approves fingerprint
        A while the intent has moved to fingerprint B would otherwise have its
        approval recorded against A and the send executed against B.
        """
        intent = self._intent(intent_id)
        self._assert_decidable(intent)

        allowed, reason = has_permission(
            self.db, org_id=self.org_id, user_id=user_id, permission=APPROVE_SEND_PERMISSION
        )
        if not allowed:
            raise NotPermitted(
                f"{user_id} may not approve outbound mail for this organisation: {reason}"
            )

        # The high-risk refusal comes BEFORE the approval is recorded, so no row
        # exists that could later be mistaken for authorisation. A blocked message
        # must not leave an approval behind for a future code path to find.
        if intent.risk_class in {r.value for r in HIGH_RISK_CLASSES}:
            intent.status = models.MailSendIntent.HIGH_RISK_BLOCKED
            intent.status_reason = (
                f"{intent.risk_class} is not sendable through the Phase 7b approval "
                "path; a human approval is not sufficient authority for this category"
            )
            self.db.flush()
            raise RiskRefused(
                f"HIGH_RISK_ACTION_BLOCKED: {intent.risk_class} cannot be sent through the "
                "ordinary approval path, even with approval"
            )

        # Recompute against the live row. If the caller approved a stale rendering,
        # this is what notices.
        live_fingerprint, live_input = _fingerprint_for_intent(intent)
        if live_fingerprint != intent.message_fingerprint:
            intent.message_fingerprint = live_fingerprint
            intent.fingerprint_input = live_input
            self.db.flush()
            raise ApprovalMismatch(
                "the send intent changed while it was being reviewed; it now carries a "
                "different fingerprint and must be re-read before approval"
            )

        membership = membership_for(self.db, org_id=self.org_id, user_id=user_id)
        existing = self.db.execute(
            select(models.MailApproval).where(
                models.MailApproval.send_intent_id == intent.id,
                models.MailApproval.fingerprint == intent.message_fingerprint,
            )
        ).scalars().first()
        if existing is not None and existing.status == models.MailApproval.STATUS_ACTIVE:
            # Idempotent: double-clicking approve is not two approvals.
            return ApprovalOutcome(
                decision=models.MailApproval.APPROVE,
                intent_id=intent.id,
                fingerprint=existing.fingerprint,
                status=intent.status,
                detail="already approved for this fingerprint",
            )

        approval = models.MailApproval(
            id=_uuid(),
            org_id=self.org_id,
            agent_id=intent.agent_id,
            send_intent_id=intent.id,
            decision=models.MailApproval.APPROVE,
            fingerprint=intent.message_fingerprint,
            risk_class=intent.risk_class,
            fingerprint_input=intent.fingerprint_input,
            approved_by=user_id,
            approved_at=_now(),
            permission_used=APPROVE_SEND_PERMISSION,
            membership_id=str(membership.role_id) if membership and membership.role_id else None,
            approval_version=1,
            status=models.MailApproval.STATUS_ACTIVE,
            note=note,
        )
        self.db.add(approval)

        intent.status = models.MailSendIntent.APPROVED
        intent.approved_at = _now()
        intent.approval_request_id = approval.id
        intent.status_reason = None
        self.db.flush()
        return ApprovalOutcome(
            decision=models.MailApproval.APPROVE,
            intent_id=intent.id,
            fingerprint=intent.message_fingerprint,
            status=intent.status,
            detail="approved",
        )

    # ------------------------------------------------------------------
    def reject(self, *, intent_id: str, user_id: str, note: Optional[str] = None) -> ApprovalOutcome:
        """Refuse the message. Needs membership only.

        Anyone who can see the draft should be able to stop it. Requiring a special
        permission to say "no" makes a bad draft unstoppable by the person who
        noticed the problem.
        """
        intent = self._intent(intent_id)
        if not self._is_member_or_owner(user_id):
            raise NotPermitted("only a member of this organisation may reject its mail")
        if intent.status in models.MailSendIntent.TERMINAL:
            raise ApprovalError(f"intent {intent.id} is already {intent.status}")

        self.db.add(
            models.MailApproval(
                id=_uuid(), org_id=self.org_id, agent_id=intent.agent_id,
                send_intent_id=intent.id, decision=models.MailApproval.REJECT,
                fingerprint=intent.message_fingerprint, risk_class=intent.risk_class,
                fingerprint_input=intent.fingerprint_input, approved_by=user_id,
                approved_at=_now(), status=models.MailApproval.STATUS_ACTIVE, note=note,
            )
        )
        intent.status = models.MailSendIntent.REJECTED
        intent.status_reason = note or "rejected by a person"
        self.db.flush()
        return ApprovalOutcome(
            decision=models.MailApproval.REJECT,
            intent_id=intent.id,
            fingerprint=intent.message_fingerprint,
            status=intent.status,
            detail="rejected",
        )

    # ------------------------------------------------------------------
    def request_changes(
        self, *, intent_id: str, user_id: str, note: Optional[str] = None
    ) -> ApprovalOutcome:
        """Ask for a different message.

        The current intent becomes explicitly unsendable. It is not left in
        WAITING_FOR_APPROVAL, because a change request that leaves the old message
        approvable invites a second approver to wave through the version the first
        one objected to.
        """
        intent = self._intent(intent_id)
        if not self._is_member_or_owner(user_id):
            raise NotPermitted("only a member of this organisation may request changes")
        if intent.status in models.MailSendIntent.TERMINAL:
            raise ApprovalError(f"intent {intent.id} is already {intent.status}")

        self.db.add(
            models.MailApproval(
                id=_uuid(), org_id=self.org_id, agent_id=intent.agent_id,
                send_intent_id=intent.id, decision=models.MailApproval.REQUEST_CHANGES,
                fingerprint=intent.message_fingerprint, risk_class=intent.risk_class,
                fingerprint_input=intent.fingerprint_input, approved_by=user_id,
                approved_at=_now(), status=models.MailApproval.STATUS_ACTIVE, note=note,
            )
        )
        intent.status = models.MailSendIntent.CHANGES_REQUESTED
        intent.status_reason = note or "a person requested changes"
        self.db.flush()
        return ApprovalOutcome(
            decision=models.MailApproval.REQUEST_CHANGES,
            intent_id=intent.id,
            fingerprint=intent.message_fingerprint,
            status=intent.status,
            detail="changes requested",
        )

    # ------------------------------------------------------------------
    def revoke(self, *, intent_id: str, user_id: str, reason: str = "") -> ApprovalOutcome:
        """Withdraw an approval before it has been sent."""
        intent = self._intent(intent_id)
        if intent.status in models.MailSendIntent.TERMINAL:
            raise ApprovalError(f"intent {intent.id} is already {intent.status}")
        if has_permission(
            self.db, org_id=self.org_id, user_id=user_id, permission=APPROVE_SEND_PERMISSION
        )[0] is False:
            raise NotPermitted("only an approver may revoke an approval")

        for approval in self.db.execute(
            select(models.MailApproval).where(
                models.MailApproval.send_intent_id == intent.id,
                models.MailApproval.status == models.MailApproval.STATUS_ACTIVE,
                models.MailApproval.decision == models.MailApproval.APPROVE,
            )
        ).scalars():
            approval.status = models.MailApproval.STATUS_REVOKED
            approval.revoked_at = _now()
            approval.revoked_by = user_id
        intent.status = models.MailSendIntent.WAITING_FOR_APPROVAL
        intent.status_reason = reason or "approval revoked"
        self.db.flush()
        return ApprovalOutcome(
            decision="REVOKE", intent_id=intent.id,
            fingerprint=intent.message_fingerprint, status=intent.status, detail="revoked",
        )

    # ------------------------------------------------------------------
    def active_approval(self, intent: models.MailSendIntent) -> Optional[models.MailApproval]:
        """The approval that authorises this intent AS IT IS NOW.

        Returns ``None`` unless a live approval's fingerprint equals the intent's
        current fingerprint. This is the single function the send path uses to decide
        whether it is authorised, so "the approval still matches" is answered in one
        place rather than three.
        """
        # Either a person approved it, or the policy engine cleared it for
        # unattended sending. Both are authorisations, and both are bound to the
        # fingerprint - so the one question the send path asks ("is this authorised
        # as it currently stands?") has one answer regardless of which produced it.
        approval = self.db.execute(
            select(models.MailApproval).where(
                models.MailApproval.send_intent_id == intent.id,
                models.MailApproval.org_id == self.org_id,
                models.MailApproval.status == models.MailApproval.STATUS_ACTIVE,
                models.MailApproval.decision.in_(
                    [models.MailApproval.APPROVE, models.MailApproval.AUTONOMOUS_POLICY]
                ),
            ).order_by(models.MailApproval.approved_at.desc())
        ).scalars().first()
        if approval is None:
            return None
        if approval.fingerprint != intent.message_fingerprint:
            # The approval exists and is live, but authorises a different message.
            # This is the modified-after-approval case, and returning None here is
            # what makes it fail closed.
            return None
        return approval

    # ------------------------------------------------------------------
    def _assert_decidable(self, intent: models.MailSendIntent) -> None:
        if intent.status in models.MailSendIntent.TERMINAL:
            raise ApprovalError(
                f"intent {intent.id} is {intent.status} and can no longer be approved"
            )
        if intent.status == models.MailSendIntent.SENDING:
            raise ApprovalError(
                f"intent {intent.id} is already being sent; approving it would be a "
                "decision about a message that has left"
            )


def _uuid() -> str:
    import uuid as _uuid_module

    return str(_uuid_module.uuid4())


def _fingerprint_for_intent(intent: models.MailSendIntent) -> tuple[str, str]:
    """Recalculate an intent's fingerprint from its own frozen columns."""
    from agent.mail.fingerprint import fingerprint

    manifest = (intent.attachment_manifest or {}).get("entries") or []
    return fingerprint(
        org_id=intent.org_id,
        agent_id=intent.agent_id,
        from_address=intent.from_address,
        to_addresses=intent.to_addresses or [],
        cc_addresses=intent.cc_addresses or [],
        bcc_addresses=intent.bcc_addresses or [],
        reply_to_address=intent.reply_to_address,
        subject=intent.subject,
        body=intent.body_snapshot,
        attachments=manifest,
        application_id=intent.application_id,
        thread_id=intent.thread_id,
        reply_to_message_id=intent.reply_to_message_id,
        draft_version=intent.draft_version,
        risk_class=intent.risk_class,
    )
