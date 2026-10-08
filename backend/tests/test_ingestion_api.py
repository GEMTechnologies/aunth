"""The producer doorway: the endpoint that made a crawler possible at all.

Before this, the catalogue had an ingestion adapter and no way in over HTTP - the only route was
direct database access, which means handing a crawler the keys to the tenant database so it can do
the one thing that must be done unscoped.
"""

from __future__ import annotations

import hashlib
import itertools
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

ROOT = BACKEND.parent.parent

KEY = "test-ingest-key-please-rotate"


@pytest.fixture
def db(tmp_path):
    """A hermetic SQLite session.

    `make_sqlite_db` copies a session-built schema rather than running `create_all` per test - a
    measured 3,828 ms versus 3 ms - so this is the same construction every other module uses.
    """
    from conftest import make_sqlite_db

    engine, session = make_sqlite_db(tmp_path, "ingestion.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


#: Distinguishes payloads. `dedupe_fingerprint` is SHA-256 of (title, domain, deadline), so two
#: deliveries with the same title on the same domain ARE the same opportunity however different
#: their URLs - which is the adapter's documented intent, not a bug to work around.
_counter = itertools.count()


def delivery(**overrides) -> dict:
    """One producer delivery, distinct from every other by default.

    The title carries the counter because the title is what the fingerprint uses. Varying only the
    description or the URL path would produce payloads the adapter correctly folds into one row.
    """
    n = next(_counter)
    text = overrides.pop("text", f"Community health grant for Nigerian NGOs (batch {n})")
    payload = {
        "title": overrides.pop("title", f"Community Health Grant {n}"),
        "source_url": f"https://example.org/ng/health-grant-{n}",
        "source_name": "Example Funder",
        "country": "Nigeria",
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "description": text,
        "sector": "health",
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def client(db, monkeypatch):
    """A TestClient with the ingestion key set and the session overridden onto the test database.

    `get_db` is overridden rather than the real `SessionLocal` being used, so the endpoint writes to
    the scratch SQLite database and never to anything real.
    """
    from fastapi.testclient import TestClient

    import config
    import main
    from database import get_db

    monkeypatch.setattr(config.settings, "ingest_bot_key", KEY, raising=False)

    def _db_override():
        yield db

    main.app.dependency_overrides[get_db] = _db_override
    try:
        yield TestClient(main.app)
    finally:
        # Leaving an override behind poisons every test that runs after this module.
        main.app.dependency_overrides.pop(get_db, None)


# ===========================================================================
# AUTHENTICATION
# ===========================================================================
def test_an_unconfigured_deployment_refuses_rather_than_accepting(db, monkeypatch):
    """THE closed-by-default property.

    An endpoint that writes to the shared catalogue every organisation reads must not be open because
    somebody forgot to set a variable. 503, with the variable named - not 401, because the fault is
    the deployment's, not the caller's.
    """
    from fastapi.testclient import TestClient

    import config
    import main

    monkeypatch.setattr(config.settings, "ingest_bot_key", "", raising=False)
    response = TestClient(main.app).post(
        "/api/v1/ingest/opportunities", json={"deliveries": [delivery()]}
    )
    assert response.status_code == 503, response.text
    assert "INGEST_BOT_KEY" in response.text


def test_a_missing_key_is_rejected(client):
    response = client.post("/api/v1/ingest/opportunities", json={"deliveries": [delivery()]})
    assert response.status_code == 401


def test_a_wrong_key_is_rejected(client):
    response = client.post(
        "/api/v1/ingest/opportunities",
        json={"deliveries": [delivery()]},
        headers={"X-Bot-Key": "not-the-key"},
    )
    assert response.status_code == 401


def test_missing_and_wrong_return_the_SAME_message(client):
    """Distinguishing them tells an attacker which half of the problem they have solved."""
    missing = client.post("/api/v1/ingest/opportunities", json={"deliveries": [delivery()]})
    wrong = client.post(
        "/api/v1/ingest/opportunities",
        json={"deliveries": [delivery()]},
        headers={"X-Bot-Key": "wrong"},
    )
    assert missing.json() == wrong.json()


def test_a_prefix_of_the_key_is_rejected(client):
    """Which is what `secrets.compare_digest` is for.

    A byte-by-byte comparison leaks the prefix through response timing. This asserts the behaviour,
    not the timing; the timing property is a consequence of using compare_digest, which is asserted
    separately below.
    """
    response = client.post(
        "/api/v1/ingest/opportunities",
        json={"deliveries": [delivery()]},
        headers={"X-Bot-Key": KEY[: len(KEY) - 1]},
    )
    assert response.status_code == 401


def test_the_key_comparison_is_timing_safe_in_the_source():
    """`==` would leak the key's prefix; the source must use compare_digest.

    Read from the source because a timing property cannot be asserted from a unit test without
    flakiness - what can be asserted is that the safe primitive is the one in use.
    """
    source = (BACKEND / "ingestion_api.py").read_text(encoding="utf-8")
    assert "secrets.compare_digest" in source
    assert "x_bot_key == configured" not in source
    assert "x_bot_key == " not in source


# ===========================================================================
# THE HAPPY PATH
# ===========================================================================
def test_a_valid_delivery_succeeds_with_INFO_logging_ENABLED(client):
    """THE regression test for a production-only 500.

    `Logger._log` checks the level BEFORE building the record:

        if self.isEnabledFor(level):
            record = self.makeRecord(...)

    The root level under pytest is WARNING, so the endpoint's `logger.info(...)` was a no-op in every
    test - and the moment INFO logging was enabled in production, a colliding `extra` key raised
    `KeyError: "Attempt to overwrite 'created' in LogRecord"` INSIDE the handler, after the batch had
    already been ingested. The first real producer delivery got a 500 for work that had succeeded.

    Enabling INFO here is the whole point: without it this test would pass against the broken code.
    """
    import logging

    logger = logging.getLogger("ingestion_api")
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        response = client.post(
            "/api/v1/ingest/opportunities",
            json={"deliveries": [delivery()]},
            headers={"X-Bot-Key": KEY},
        )
    finally:
        logger.setLevel(previous)

    assert response.status_code == 200, (
        f"the endpoint failed while INFO logging was enabled: {response.status_code} {response.text}"
    )
    assert response.json()["created"] == 1


def test_a_valid_delivery_is_created(client):
    response = client.post(
        "/api/v1/ingest/opportunities",
        json={"deliveries": [delivery()]},
        headers={"X-Bot-Key": KEY},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["received"] == 1
    assert body["created"] == 1
    assert body["outcomes"][0]["status"] == "CREATED"
    assert body["outcomes"][0]["opportunity_id"]


def test_a_second_identical_delivery_is_UNCHANGED_not_created(client):
    """THE idempotence property, and the one that matters most operationally.

    A crawl retries. If a retry created a second row, every organisation reading the shared catalogue
    would see the opportunity twice - and the dedupe identity exists precisely to make a producer's
    retry free.
    """
    headers = {"X-Bot-Key": KEY}
    # THE SAME payload both times. A crawl retry re-sends what it already sent; that is the case the
    # dedupe identity exists for.
    payload = {"deliveries": [delivery()]}
    first = client.post("/api/v1/ingest/opportunities", json=payload, headers=headers).json()
    second = client.post("/api/v1/ingest/opportunities", json=payload, headers=headers).json()

    assert first["created"] == 1
    assert second["created"] == 0, "a re-delivery created a second row"
    # The property is IDENTITY, not status. A re-send may be reported UNCHANGED or UPDATED depending
    # on which non-identity fields the adapter refreshes (it stamps `scraped_at`), and pinning the
    # status would be pinning an implementation detail. Pinning the ID is pinning the contract.
    assert second["outcomes"][0]["opportunity_id"] == first["outcomes"][0]["opportunity_id"], (
        "a re-delivery produced a different opportunity, so the dedupe identity is not being used"
    )
    # DUPLICATE_DELIVERY is the strongest of the three, not a failure: the adapter recognised the
    # delivery itself and short-circuited before touching the catalogue at all. UNCHANGED means it
    # compared the content and found nothing to change; UPDATED means it refreshed non-identity
    # fields. All three mean "no second row", which is the contract.
    assert second["outcomes"][0]["status"] in ("DUPLICATE_DELIVERY", "UNCHANGED", "UPDATED"), (
        f"a re-delivery reported {second['outcomes'][0]['status']}, which means it was treated as "
        "new work rather than the same opportunity arriving twice"
    )


def test_a_batch_is_accepted(client):
    response = client.post(
        "/api/v1/ingest/opportunities",
        json={
            "deliveries": [delivery(country="Nigeria") for _ in range(5)]
        },
        headers={"X-Bot-Key": KEY},
    )
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 5


# ===========================================================================
# ISOLATION AND VALIDATION
# ===========================================================================
def test_a_malformed_delivery_does_not_abandon_the_rest_of_the_batch(client):
    """A producer that starts emitting one bad field must lose one opportunity, not a whole run.

    `ingest_many` isolates per-delivery failures; this pins that the endpoint keeps the property
    rather than turning any failure into a 500 for the batch.
    """
    response = client.post(
        "/api/v1/ingest/opportunities",
        json={
            "deliveries": [
                delivery(),
                # Satisfies the schema, violates the CONTRACT - which is the only kind of bad
                # delivery the adapter gets to see. A schema violation is refused earlier and takes
                # the whole request with it (pinned by the test below).
                delivery(title=""),
                delivery(),
            ]
        },
        headers={"X-Bot-Key": KEY},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] >= 1, "the good deliveries must still be ingested"
    assert body["rejected"] >= 1, "the bad delivery must be reported, not swallowed"


def test_a_content_hash_that_is_not_a_sha256_is_refused(client):
    """The adapter refuses this too; the endpoint's job is to refuse it EARLY and say which field.

    Asserted at the HTTP boundary. The wording of the message is asserted separately below against
    the validator itself: going through the HTTP layer to read a validation message couples this test
    to how FastAPI serialises errors, which is not the property under test.
    """
    response = client.post(
        "/api/v1/ingest/opportunities",
        # 64 characters, so `min_length` passes and the digest RULE is what refuses it rather than
        # the length rule. A short string trips pydantic before the validator is reached.
        json={"deliveries": [delivery(content_hash="z" * 64)]},
        headers={"X-Bot-Key": KEY},
    )
    assert response.status_code == 422, response.text

    # AND THE CLIENT MUST BE TOLD WHY. This is the assertion that caught a system-wide defect:
    # pydantic v2 puts the original ValueError OBJECT in the error's `ctx`, `JSONResponse` cannot
    # serialise it, and the handler meant to explain the error raised a TypeError instead - so the
    # caller received 500 "Internal server error" for what was a 422. A producer sending a bad hash
    # would have been told the server was broken.
    assert "SHA-256" in response.text, (
        "the 422 body does not explain the rule; a producer cannot fix the delivery"
    )
    assert "content_hash" in response.text, "the 422 body does not name the offending field"


def test_the_hash_validator_says_which_field_and_why():
    """The message, asserted directly.

    The producer owns normalisation, so a wrong hash is the producer's bug - and it deserves a
    sentence naming the field and the rule rather than a per-item REJECTED it has to go digging for.
    Calling the validator is both a stronger assertion and a more stable one than reading FastAPI's
    serialised error body.
    """
    from ingestion_api import DeliveryIn

    with pytest.raises(Exception) as caught:
        DeliveryIn(
            title="T",
            source_url="https://e.org/a",
            source_name="S",
            country="Nigeria",
            content_hash="z" * 64,
        )
    message = str(caught.value)
    assert "content_hash" in message, "the error must name the field"
    assert "SHA-256" in message, "the error must name the rule the producer has to satisfy"
    assert "producer owns" in message, "the error must say whose job normalisation is"


def test_a_SCHEMA_violation_refuses_the_whole_batch(client):
    """The limit of this endpoint, pinned so it is not discovered by a producer at 3am.

    `ingest_many` isolates per-delivery failures, but only for deliveries that survive validation.
    A null where a string belongs is a request-schema violation, so the whole batch is refused with
    422 and nothing is ingested.

    That is the correct trade for a machine producer: a crawler emitting a null has a bug, and
    failing the delivery loudly is better than silently dropping records from a shared catalogue.
    But it is a real difference from the per-item isolation, and it is stated here rather than left
    for someone to infer.
    """
    response = client.post(
        "/api/v1/ingest/opportunities",
        json={"deliveries": [delivery(), delivery(source_url=None), delivery()]},
        headers={"X-Bot-Key": KEY},
    )
    assert response.status_code == 422, (
        "a schema violation was accepted; producers would then have records silently dropped"
    )


def test_an_empty_batch_is_refused(client):
    response = client.post(
        "/api/v1/ingest/opportunities", json={"deliveries": []}, headers={"X-Bot-Key": KEY}
    )
    assert response.status_code == 422


def test_an_oversized_batch_is_refused(client):
    """An unbounded array is a memory-exhaustion vector regardless of who may call it."""
    response = client.post(
        "/api/v1/ingest/opportunities",
        json={
            "deliveries": [delivery() for _ in range(500)]
        },
        headers={"X-Bot-Key": KEY},
    )
    assert response.status_code == 422


# ===========================================================================
# THE CONTRACT ENDPOINT
# ===========================================================================
def test_the_contract_is_public_and_describes_what_to_send(client):
    """A producer author should not have to read a markdown file to learn the hash rule.

    Public on purpose: the schema is not a secret. The endpoint that ACCEPTS deliveries is the one
    that needs a key.
    """
    response = client.get("/api/v1/ingest/contract")
    assert response.status_code == 200
    body = response.json()
    assert body["endpoint"] == "POST /api/v1/ingest/opportunities"
    assert "content_hash" in body["required_fields"]
    assert "SHA-256" in body["content_hash"]


# ===========================================================================
# THE ADR-0009 RELATIONSHIP
# ===========================================================================
def test_the_endpoint_documents_that_its_write_is_UNSCOPED():
    """The one write in this codebase that must NOT bind a tenant.

    ADR-0009 D2: `opportunities_insert` requires `app.current_org() IS NULL`, so the shared catalogue
    can only be written by an unscoped context. A future reader could easily "fix" this by adding a
    tenant dependency - which would make every producer delivery fail with an RLS violation.

    Read from the source with comments stripped, so the assertion is about the code and not about the
    paragraph that explains it.
    """
    import ast

    source = (BACKEND / "ingestion_api.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body.pop(0)
    executable = ast.unparse(tree)

    assert "get_tenant_db" not in executable, (
        "the ingestion endpoint binds a tenant, but the shared catalogue requires an UNSCOPED write "
        "(ADR-0009 D2) - every producer delivery would fail with an RLS violation"
    )
    assert "require_org_access" not in executable, (
        "the ingestion endpoint checks organisation access, but a producer has no organisation - it "
        "feeds a catalogue that every organisation reads"
    )
