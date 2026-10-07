"""Model gateway: provider neutrality, recording, budget, untrusted output.

The load-bearing test here is ``test_model_output_is_untrusted_until_validated``.
The build brief names it directly - *"model output is untrusted until
validated"* - and it is the mechanism that stops a hallucinated field becoming a
submission fact.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import config  # noqa: E402
import models  # noqa: E402
from agent.model_gateway import (  # noqa: E402
    CostBudgetExceeded,
    ModelGateway,
    ModelOutputInvalid,
    ModelRoute,
    NoRouteAvailable,
    NullProvider,
    Price,
    ProviderResponse,
    ScriptedProvider,
    estimate_cost_micros,
)
from agent.redaction import digest, minimize, redact  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path):
    # Schema copied from a session template rather than rebuilt: create_all to a
    # file on this filesystem costs ~3.8s per test because the schema has 38 tables
    # and 203 indexes. See tests/conftest.py::make_sqlite_db.
    engine, session = make_sqlite_db(tmp_path, "gateway.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def org(db):
    user = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(user)
    db.commit()
    row = models.Organisation(
        id=str(uuid.uuid4()), name="Test NGO", slug="test-ngo", owner_user_id=user.id
    )
    db.add(row)
    db.commit()
    return row.id


ROUTES = {
    models.ModelInvocation.CLASSIFICATION: [
        ModelRoute(provider="scripted", model="small-1", tier="CLASSIFICATION")
    ],
    models.ModelInvocation.SYNTHESIS: [
        ModelRoute(provider="scripted", model="big-1", tier="SYNTHESIS")
    ],
}

PRICES = {
    "small-1": Price(input_micros_per_1k=250, output_micros_per_1k=1250),
    "big-1": Price(input_micros_per_1k=3000, output_micros_per_1k=15000),
}


def _gateway(db, responses, **kwargs):
    provider = ScriptedProvider(responses)
    gw = ModelGateway(db, provider, routes=ROUTES, prices=PRICES, **kwargs)
    return gw, provider


def _ok(text="hello", inp=1000, out=500):
    return ProviderResponse(text=text, input_tokens=inp, output_tokens=out)


# ---------------------------------------------------------------------------
# Provider neutrality
# ---------------------------------------------------------------------------
def test_null_provider_refuses_rather_than_calling_a_vendor(db):
    """The default must fail loudly, not reach a network implicitly."""
    gw = ModelGateway(db, NullProvider(), routes=ROUTES, prices=PRICES)
    with pytest.raises(NoRouteAvailable):
        gw.complete(tier="SYNTHESIS", prompt="hi", prompt_version="v1")


def test_an_unconfigured_tier_refuses_rather_than_guessing(db):
    """Picking a model to act on an organisation's behalf is not a default."""
    gw = ModelGateway(db, ScriptedProvider([_ok()]), routes={}, prices=PRICES)
    with pytest.raises(NoRouteAvailable):
        gw.complete(tier="SYNTHESIS", prompt="hi", prompt_version="v1")


def test_tiers_route_to_different_models(db):
    """Cheap model for triage, strong model for synthesis."""
    gw, _ = _gateway(db, [_ok(), _ok()])
    a = gw.complete(tier="CLASSIFICATION", prompt="x", prompt_version="v1")
    b = gw.complete(tier="SYNTHESIS", prompt="x", prompt_version="v1")
    assert a.model == "small-1"
    assert b.model == "big-1"
    assert a.cost_micros < b.cost_micros


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
def test_every_call_is_recorded_with_provider_model_and_prompt_version(db, org):
    gw, _ = _gateway(db, [_ok()])
    result = gw.complete(
        tier="SYNTHESIS", prompt="write a proposal", prompt_version="proposal-v3", org_id=org
    )
    db.commit()

    row = db.execute(select(models.ModelInvocation)).scalar_one()
    assert row.id == result.invocation_id
    assert row.provider == "scripted"
    assert row.model == "big-1"
    assert row.prompt_version == "proposal-v3"
    assert row.status == models.ModelInvocation.SUCCEEDED
    assert row.org_id == org


def test_prompts_are_digested_not_stored_by_default(db, org):
    """Data minimisation: an ops table is not a place for donor text."""
    gw, _ = _gateway(db, [_ok()])
    gw.complete(tier="SYNTHESIS", prompt="Secret donor name Acme", prompt_version="v1", org_id=org)
    db.commit()

    row = db.execute(select(models.ModelInvocation)).scalar_one()
    assert row.prompt_text is None
    assert row.response_text is None
    assert row.prompt_digest == digest("Secret donor name Acme")
    assert row.prompt_digest != "Secret donor name Acme"


def test_storing_prompts_is_opt_in(db, org):
    gw, _ = _gateway(db, [_ok()], store_prompts=True)
    gw.complete(tier="SYNTHESIS", prompt="debug me", prompt_version="v1", org_id=org)
    db.commit()
    row = db.execute(select(models.ModelInvocation)).scalar_one()
    assert row.prompt_text == "debug me"


def test_a_failed_call_is_recorded_too(db, org):
    """An unrecorded failure is invisible cost and invisible breakage."""
    from agent.model_gateway import ModelCallFailed

    gw, _ = _gateway(db, [ModelCallFailed("rate limited (429)")])
    with pytest.raises(ModelCallFailed):
        gw.complete(tier="SYNTHESIS", prompt="x", prompt_version="v1", org_id=org)
    db.commit()

    row = db.execute(select(models.ModelInvocation)).scalar_one()
    assert row.status == models.ModelInvocation.FAILED
    assert row.error_category == "ModelCallFailed"


def test_latency_and_tokens_are_recorded(db, org):
    gw, _ = _gateway(db, [_ok(inp=2000, out=750)])
    result = gw.complete(tier="SYNTHESIS", prompt="x", prompt_version="v1", org_id=org)
    db.commit()
    row = db.execute(select(models.ModelInvocation)).scalar_one()
    assert row.input_tokens == 2000
    assert row.output_tokens == 750
    assert result.input_tokens == 2000


# ---------------------------------------------------------------------------
# The critical test: untrusted output
# ---------------------------------------------------------------------------
def test_model_output_is_untrusted_until_validated(db, org):
    """A schema-required call whose output does not validate must FAIL.

    The brief's rule is that a model response is input, not truth. The failure
    this prevents is specific and expensive: a hallucinated field - a budget
    figure, a co-funding percentage, a legal declaration - silently becoming a
    fact in a submitted application.

    So an unparseable response raises, and no structured value escapes. It is
    also recorded as INVALID_OUTPUT rather than as a success, because an
    unrecorded validation failure is how a caller starts "tolerating" them.
    """
    gw, _ = _gateway(db, [_ok(text="Sure! Here is the JSON you asked for.")])
    schema = {
        "type": "object",
        "required": ["eligible"],
        "properties": {"eligible": {"type": "boolean"}},
        "additionalProperties": False,
    }

    with pytest.raises(ModelOutputInvalid):
        gw.complete(
            tier="CLASSIFICATION", prompt="x", prompt_version="v1",
            org_id=org, response_schema=schema,
        )
    db.commit()

    row = db.execute(select(models.ModelInvocation)).scalar_one()
    assert row.status == models.ModelInvocation.INVALID_OUTPUT
    assert row.error_category == "INVALID_OUTPUT"


def test_valid_structured_output_is_returned(db, org):
    gw, _ = _gateway(db, [_ok(text='{"eligible": true}')])
    result = gw.complete(
        tier="CLASSIFICATION", prompt="x", prompt_version="v1", org_id=org,
        response_schema={
            "type": "object",
            "required": ["eligible"],
            "properties": {"eligible": {"type": "boolean"}},
        },
    )
    assert result.is_structured
    assert result.data == {"eligible": True}


def test_unstructured_calls_return_no_structured_data(db, org):
    """``data`` stays None without a schema, so it cannot be mistaken for one."""
    gw, _ = _gateway(db, [_ok(text='{"eligible": true}')])
    result = gw.complete(tier="SYNTHESIS", prompt="x", prompt_version="v1", org_id=org)
    assert result.data is None
    assert not result.is_structured


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "[1, 2, 3]",                       # an array where an object was required
        '{"eligible": "yes"}',             # wrong type
        '{"eligible": true, "extra": 1}',  # unexpected field
        "{}",                              # missing required field
        '{"eligible": true}',              # valid - control case
    ],
)
def test_validation_matrix(db, payload):
    """Each rejection is distinct, and the last case proves the validator is not vacuous."""
    gw, _ = _gateway(db, [_ok(text=payload)])
    schema = {
        "type": "object",
        "required": ["eligible"],
        "properties": {"eligible": {"type": "boolean"}},
        "additionalProperties": False,
    }
    valid = payload == '{"eligible": true}'
    if valid:
        assert gw.complete(
            tier="CLASSIFICATION", prompt="x", prompt_version="v1", response_schema=schema
        ).data == {"eligible": True}
    else:
        with pytest.raises(ModelOutputInvalid):
            gw.complete(
                tier="CLASSIFICATION", prompt="x", prompt_version="v1", response_schema=schema
            )


def test_markdown_fences_are_tolerated_but_extra_prose_is_not(db):
    """Fences are pedantry; trailing prose means the model did not answer in JSON."""
    schema = {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}}
    gw, _ = _gateway(db, [_ok(text='```json\n{"n": 3}\n```')])
    assert gw.complete(
        tier="CLASSIFICATION", prompt="x", prompt_version="v1", response_schema=schema
    ).data == {"n": 3}

    gw2, _ = _gateway(db, [_ok(text='{"n": 3}\n\nI hope this helps!')])
    with pytest.raises(ModelOutputInvalid):
        gw2.complete(
            tier="CLASSIFICATION", prompt="x", prompt_version="v1", response_schema=schema
        )


def test_enum_is_enforced(db):
    """Intent classification must land on a known intent, not a plausible one."""
    schema = {
        "type": "object",
        "required": ["intent"],
        "properties": {"intent": {"type": "string", "enum": ["AWARD", "REJECTION"]}},
    }
    gw, _ = _gateway(db, [_ok(text='{"intent": "AWARD"}')])
    assert gw.complete(
        tier="CLASSIFICATION", prompt="x", prompt_version="v1", response_schema=schema
    ).data == {"intent": "AWARD"}

    gw2, _ = _gateway(db, [_ok(text='{"intent": "PROBABLY_AWARD"}')])
    with pytest.raises(ModelOutputInvalid):
        gw2.complete(
            tier="CLASSIFICATION", prompt="x", prompt_version="v1", response_schema=schema
        )


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------
def test_cost_is_computed_from_tokens_and_price():
    # 2000 in @ $0.00025/1k + 500 out @ $0.00125/1k
    assert estimate_cost_micros("small-1", 2000, 500, PRICES) == 500 + 625


def test_cost_rounds_up_never_down():
    """Rounding down would let unbounded sub-micro calls run free."""
    prices = {"m": Price(input_micros_per_1k=1, output_micros_per_1k=0)}
    assert estimate_cost_micros("m", 1, 0, prices) == 1


def test_an_unknown_model_is_not_free():
    """A missing price entry must fail toward caution, not toward free."""
    assert estimate_cost_micros("mystery", 1000, 1000, PRICES) == estimate_cost_micros(
        "big-1", 1000, 1000, PRICES
    )


def test_per_call_ceiling_blocks_an_expensive_call(db, org):
    gw, _ = _gateway(db, [_ok(inp=100_000, out=100_000)], per_call_ceiling_micros=1000)
    with pytest.raises(CostBudgetExceeded):
        gw.complete(tier="SYNTHESIS", prompt="x", prompt_version="v1", org_id=org)
    db.commit()
    row = db.execute(select(models.ModelInvocation)).scalar_one()
    assert row.status == models.ModelInvocation.BUDGET_EXCEEDED


def test_daily_ceiling_stops_further_spend(db, org):
    """The ceiling is enforced before the call, so nothing is spent."""
    gw, provider = _gateway(db, [_ok(), _ok(), _ok()], daily_ceiling_micros=100)
    gw.complete(tier="SYNTHESIS", prompt="x", prompt_version="v1", org_id=org)
    db.commit()

    assert gw.spent_micros_today(org) > 100
    calls_before = len(provider.calls)
    with pytest.raises(CostBudgetExceeded):
        gw.complete(tier="SYNTHESIS", prompt="x", prompt_version="v1", org_id=org)
    assert len(provider.calls) == calls_before, "a blocked call still reached the provider"


def test_one_tenants_spend_does_not_count_against_another(db, org):
    """Per-tenant budgets, not a global one."""
    user = models.User(id=str(uuid.uuid4()), display_name="Other")
    db.add(user)
    db.commit()
    other = models.Organisation(
        id=str(uuid.uuid4()), name="Other NGO", slug="other-ngo", owner_user_id=user.id
    )
    db.add(other)
    db.commit()

    gw, _ = _gateway(db, [_ok(), _ok()])
    gw.complete(tier="SYNTHESIS", prompt="x", prompt_version="v1", org_id=org)
    db.commit()

    assert gw.spent_micros_today(org) > 0
    assert gw.spent_micros_today(other.id) == 0


def test_the_rolling_window_excludes_old_spend(db, org):
    """A calendar reset would let a tenant spend its budget twice at midnight."""
    from datetime import datetime, timedelta, timezone

    gw, _ = _gateway(db, [_ok()])
    gw.complete(tier="SYNTHESIS", prompt="x", prompt_version="v1", org_id=org)
    db.commit()

    row = db.execute(select(models.ModelInvocation)).scalar_one()
    row.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
    db.commit()

    assert gw.spent_micros_today(org) == 0


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
def test_redaction_removes_emails_phones_and_secrets():
    text = "Write to jane.doe@example.org or +256 700 123456. Key sk-abcdefghijklmnopqrst."
    result = redact(text)
    assert "jane.doe@example.org" not in result.text
    assert "+256 700 123456" not in result.text
    assert "sk-abcdefghijklmnopqrst" not in result.text
    assert result.counts["EMAIL"] == 1


def test_redaction_placeholders_are_stable_within_one_call():
    """Relationships inside a prompt must survive redaction."""
    result = redact("From a@example.org to a@example.org and b@example.org")
    assert result.text.count("[EMAIL_1]") == 2
    assert "[EMAIL_2]" in result.text


def test_redaction_is_idempotent():
    """A prompt passing through two code paths must not be progressively mangled."""
    text = "Contact jane@example.org or call +256 700 123456 about SS82 ABCD 1234."
    once = redact(text).text
    twice = redact(once).text
    assert once == twice


def test_redaction_is_recorded_on_the_result(db, org):
    gw, _ = _gateway(db, [_ok()])
    result = gw.complete(
        tier="SYNTHESIS", prompt="email jane@example.org", prompt_version="v1", org_id=org
    )
    assert result.redactions.get("EMAIL") == 1


def test_minimize_truncates_after_redacting_and_says_so():
    """A silently truncated prompt produces a confidently wrong answer.

    The email is placed *first* on purpose. With it at the end, truncation
    alone would remove it and this test would pass even if redaction never ran
    - a test that cannot fail under the mutation it targets.
    """
    long_text = "jane@example.org " + ("word " * 100)
    out = minimize(long_text, max_chars=50)
    assert "[TRUNCATED:" in out
    assert "jane@example.org" not in out, "redaction did not run before truncation"
    assert len(out) < len(long_text)


# ---------------------------------------------------------------------------
# The settings invariant
# ---------------------------------------------------------------------------
def test_no_setting_shadows_a_pydantic_attribute():
    """``model_`` is a Pydantic-reserved prefix; this proves the collision is absent.

    ``Settings.model_config`` sets ``protected_namespaces=("settings_",)`` so
    the model-gateway settings can use the domain's own words. That is only safe
    while no setting is named after a real ``BaseModel`` attribute, so the
    invariant is asserted rather than assumed.
    """
    reserved = {
        name for name in dir(BaseModel) if not name.startswith("__")
    }
    collisions = sorted(
        name for name in config.Settings.model_fields
        if name in reserved and name not in {"model_config"}
    )
    # ``model_config`` is Pydantic's own, not ours, so it is excluded above.
    assert collisions == [], (
        f"settings fields shadow Pydantic's own API: {collisions}. "
        "Rename them - a setting named model_validate would break serialisation."
    )


def test_settings_protected_namespace_is_narrowed_deliberately():
    assert config.Settings.model_config.get("protected_namespaces") == ("settings_",)
