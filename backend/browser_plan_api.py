"""Container-side planning for browser navigation.

WHY THIS EXISTS. The browser worker runs on the HOST, in a sandbox, and that is correct - it holds
Chromium and nothing else. But the host has no model credentials and no database (measured:
MODEL_PROVIDER=null, MODEL_API_KEY absent, no Postgres route). So the decision chain it could build
was `rules -> local`, and both are evidence engines: their own refusal was "no evidence bears on any
of 1 question(s) for 'browser.next_action'".

Planning therefore belongs HERE, where the model config, the database, the tenant binding and the
policy checks already live. The worker asks; Granada decides. The host never sees an API key or a
database credential, which is the point of the split rather than an accident of it.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from config import settings
from database import FleetSessionLocal
from tenant_context import clear_tenant, set_tenant

logger = logging.getLogger("browser_plan_api")

router = APIRouter(prefix="/internal/browser", tags=["internal-browser"])

#: Terms that mean a consequential commitment.
CONSEQUENTIAL = (
    "submit", "register", "sign up", "signup", "create account", "apply",
    "i agree", "i accept", "declare", "confirm", "pay", "purchase",
)

#: Terms that only mean "go to the form". A LINK carrying one of these NAVIGATES; it commits
#: nothing. The commitment is the submit control ON that form. Blocking the link made the goal
#: unreachable - the planner was forbidden from opening the very page the objective asks about.
NAVIGATIONAL = ("create account", "register", "sign up", "signup", "apply")


def _is_consequential(text: str, tag: str = "") -> bool:
    """Whether activating this control would make a commitment.

    Tag-aware on purpose: `<a>Create account here</a>` opens a registration form, whereas
    `<button>Register</button>` on that form submits it. Treating both as forbidden is not caution,
    it is a policy that prevents the task from being done at all.
    """
    lowered = (text or "").strip().lower()
    if tag.upper() == "A":
        return any(t in lowered for t in CONSEQUENTIAL if t not in NAVIGATIONAL)
    return any(t in lowered for t in CONSEQUENTIAL)

MAX_CONTROLS = 25


def require_plan_key(x_plan_key: str = Header(default="", alias="X-Plan-Key")) -> str:
    """Service credential for the planner.

    Closed by default: with no key configured the endpoint refuses rather than accepting
    unauthenticated planning requests, which would be an open door into the tenant's model budget and
    an oracle for its configuration. Comparison is timing-safe, and "missing" and "wrong" are the
    same message because distinguishing them tells an attacker which half they have solved.
    """
    configured = (getattr(settings, "browser_plan_key", "") or "").strip()
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="browser planning is not configured: set BROWSER_PLAN_KEY to enable it",
        )
    if not x_plan_key or not secrets.compare_digest(x_plan_key.strip(), configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="a valid X-Plan-Key header is required",
        )
    return "planner"


class ControlIn(BaseModel):
    tag: str = ""
    type: str = ""
    name: str = ""
    id: str = ""
    text: str = ""


class PlanIn(BaseModel):
    org_id: str
    goal: str
    url: str = ""
    title: str = ""
    controls: list[ControlIn] = Field(default_factory=list)
    submission_authorised: bool = False


class PlanOut(BaseModel):
    action: str = ""
    confidence: Optional[float] = None
    provider: str = ""
    model: Optional[str] = None
    refused: bool = False
    detail: str = ""


def _build_planner(db: Session) -> Any:
    """The decision chain, with the model actually in it.

    `providers=` is passed explicitly rather than relying on `settings.decision_provider`, because
    the chain's CONTENT is the safety property here: deterministic rules and Granada's own evidence
    engine run first, and the model is consulted only when neither can answer. Setting a string in
    the environment should not be able to remove that ordering.
    """
    from agent.decision.gateway import build_gateway
    from agent.decision.providers.llm import LLMDecisionProvider
    from agent.decision.providers.local import LocalDecisionProvider
    from agent.decision.providers.rules import RulesDecisionProvider, default_rules
    from agent.model_gateway import build_gateway_from_settings

    model_gateway = build_gateway_from_settings(db, settings)
    return build_gateway(
        db=db,
        settings=settings,
        providers=[
            RulesDecisionProvider(default_rules()),
            LocalDecisionProvider(),
            LLMDecisionProvider(model_gateway),
        ],
    )


@router.post("/plan", response_model=PlanOut, summary="Choose the next browser action")
def plan(
    payload: PlanIn,
    _: str = Depends(require_plan_key),
) -> PlanOut:
    """Decide the next action from what the browser actually observed.

    The caller supplies the goal and the controls it can see; it never supplies a selector, so a
    decision can only name a control that genuinely exists on the page. The chosen action is
    validated here as well as in the worker.
    """
    from agent.decision.models import DecisionQuestion, DecisionRequest

    if not payload.org_id:
        raise HTTPException(status_code=400, detail="org_id is required; tenant unknown is a deny")
    if not payload.goal.strip():
        raise HTTPException(status_code=400, detail="goal is required")

    # Only offer controls that could actually be activated, and only ones policy permits.
    offered: list[dict[str, str]] = []
    for control in payload.controls[:MAX_CONTROLS]:
        label = (control.text or control.name or control.id or "").strip()
        if not label:
            continue
        if not payload.submission_authorised and _is_consequential(label, control.tag):
            logger.info("planner refused a consequential control: %s", label[:60])
            continue
        offered.append({"tag": control.tag, "id": control.id, "text": label})

    if not offered:
        return PlanOut(
            action="",
            refused=True,
            detail="no control may be acted on: every observed control was blank or consequential",
        )

    # OPTION LABELS ARE UPPERCASED, AND THIS IS NOT COSMETIC.
    # `DecisionQuestion.validate` computes `value.strip().upper()` and then compares it against
    # `self.options` VERBATIM. Mixed-case options can therefore never validate - the model does answer
    # correctly and is rejected anyway ("'Contact us' is not one of [... 'Contact us' ...]"). Every
    # existing CHOICE question happens to use already-uppercase options (HIGH/MEDIUM/LOW), which is
    # why the shared validator has never had to face this. Control labels are NOT uppercase, so the
    # mapping is made here rather than by loosening a validator that other decisions depend on.
    option_for: dict[str, str] = {}
    for control in offered:
        option_for.setdefault(control["text"].upper(), control["text"])
    options = list(option_for.keys()) + ["STOP"]

    db = FleetSessionLocal()
    try:
        # TENANT BINDING: the plan is made inside the organisation's own scope, so a decision can
        # never be informed by, or recorded against, another tenant.
        set_tenant(db, payload.org_id, None)

        gateway = _build_planner(db)
        request = DecisionRequest(
            decision_type="browser.next_action",
            questions=(
                DecisionQuestion(
                    key="next_action",
                    type="CHOICE",
                    instructions=(
                        f"Objective: {payload.goal}\n"
                        f"Current page: {payload.title} ({payload.url})\n"
                        "Choose the single control that best advances the objective, by its exact "
                        "text. Choose 'stop' if the objective is already met, or if no offered "
                        "control can advance it."
                    ),
                    options=tuple(options),
                ),
            ),
            state={
                "goal": payload.goal,
                "url": payload.url,
                "title": payload.title,
                "controls": offered,
            },
            organisation_id=payload.org_id,
        )

        result = gateway.decide(request)
        answer = result.answers.get("next_action")
        raw = str(getattr(answer, "value", "") or "").strip()

        # Map the uppercased option back to the control's REAL visible text, because the worker
        # activates a control by the text it observed, not by a normalised label.
        if raw.upper() == "STOP":
            chosen = "stop"
        else:
            chosen = option_for.get(raw.upper(), "")

        # A chosen control that was not offered is refused rather than passed through: the model may
        # only select from what the browser actually saw.
        if not chosen:
            logger.warning("planner chose %r which was not among the observed controls", raw[:80])
            return PlanOut(
                action="", refused=True, provider=getattr(result, "provider", "") or "",
                detail="the chosen control was not among the observed controls; refusing",
            )

        return PlanOut(
            action=chosen,
            confidence=getattr(answer, "confidence", None),
            provider=getattr(result, "provider", "") or "",
            model=getattr(result, "model", None),
        )
    except Exception as exc:  # noqa: BLE001 - the caller needs a reason, not a stack
        logger.warning("planning failed: %s: %s", type(exc).__name__, exc)
        return PlanOut(action="", refused=True, detail=f"{type(exc).__name__}: {str(exc)[:300]}")
    finally:
        clear_tenant(db)
        db.close()
