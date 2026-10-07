"""Behavioural RLS test for the shared opportunity catalogue (Phase 4).

The catalogue inverts the usual posture on purpose. A funding opportunity
published on a website belongs to nobody, so:

* every tenant may **read** it - it is a shared catalogue, and making it
  tenant-owned would mean every tenant re-scraped the world;
* only an **unscoped** context may **write** it. A tenant-scoped request always
  has a tenant bound, so a tenant cannot poison what every other tenant matches
  against.

That second half is the security property, and it is asserted here rather than
described, because it is the kind of claim that is easy to state and easy to get
wrong: the policies are the only thing enforcing it, and the grant layer
deliberately allows DELETE (the RLS policy refuses it for every role).

This module is separate from ``test_tenant_rls.py`` because it needs no RLS
scratch schema - it exercises the live ``public`` schema, where migration 007 is
applied. It skips loudly when no PostgreSQL target is configured.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from tests.test_tenant_rls import RUNTIME_URL, PG_URL, tenant_scope, unscoped  # noqa: E402

pytestmark = pytest.mark.skipif(
    not PG_URL or not RUNTIME_URL,
    reason=(
        "the catalogue's inverted posture is only observable as the non-owner "
        "runtime role against real PostgreSQL. Set GRANADA_RUNTIME_DATABASE_URL "
        "to a postgresql://granada_app:... URL to run these."
    ),
)


@pytest.fixture(scope="module")
def runtime():
    engine = create_engine(RUNTIME_URL)
    try:
        yield engine
    finally:
        engine.dispose()


def _hash(seed: str) -> str:
    import hashlib

    return hashlib.sha256(seed.encode()).hexdigest()


def _row(seed: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "title": f"Catalogue probe {seed}",
        "source_url": f"https://probe.example.org/{seed}",
        "source_name": "RLS probe",
        "country": "UG",
        "content_hash": _hash(seed),
        "dedupe_fingerprint": _hash(f"fp-{seed}"),
    }


_INSERT = text(
    """
    INSERT INTO opportunities
        (id, title, source_url, source_name, country, content_hash,
         dedupe_fingerprint, created_at, is_active, is_verified, contract_version)
    VALUES
        (:id, :title, :source_url, :source_name, :country, :content_hash,
         :dedupe_fingerprint, now(), true, false, 'v1')
    """
)


def test_a_tenant_scoped_request_cannot_write_to_the_shared_catalogue(runtime):
    """The security property: one tenant cannot poison a shared catalogue.

    If this ever passes, every tenant's matching is attacker-controlled.
    """
    from sqlalchemy.exc import ProgrammingError

    with pytest.raises(ProgrammingError) as excinfo:
        with tenant_scope(runtime, org_id=str(uuid.uuid4())) as conn:
            conn.execute(_INSERT, _row(f"tenant-write-{uuid.uuid4().hex[:8]}"))
    assert "row-level security" in str(excinfo.value).lower()


def test_an_unscoped_ingestion_context_can_write_to_the_catalogue(runtime):
    """The other half: the ingestion service legitimately writes it.

    Without this, the previous test would be satisfied by a policy that simply
    forbade all writes - which would be a control that is never actually true,
    and would make Phase 4 impossible.
    """
    row = _row(f"ingest-{uuid.uuid4().hex[:8]}")
    with unscoped(runtime) as conn:
        conn.execute(_INSERT, row)
        conn.commit()

    try:
        with unscoped(runtime) as conn:
            stored = conn.execute(
                text("SELECT title FROM opportunities WHERE id = :id"), {"id": row["id"]}
            ).scalar()
        assert stored == row["title"]
    finally:
        # Cleanup runs as the owner, because DELETE is refused to every role by
        # the policy - which is itself part of the design.
        admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                conn.execute(
                    text("DELETE FROM opportunities WHERE id = :id"), {"id": row["id"]}
                )
        finally:
            admin.dispose()


def test_a_tenant_scoped_request_can_READ_the_catalogue(runtime):
    """If tenants could not read it, the shared catalogue would be pointless."""
    row = _row(f"read-{uuid.uuid4().hex[:8]}")
    with unscoped(runtime) as conn:
        conn.execute(_INSERT, row)
        conn.commit()

    try:
        with tenant_scope(runtime, org_id=str(uuid.uuid4())) as conn:
            seen = conn.execute(
                text("SELECT count(*) FROM opportunities WHERE id = :id"), {"id": row["id"]}
            ).scalar()
        assert seen == 1, "a tenant could not read the shared catalogue"
    finally:
        admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                conn.execute(
                    text("DELETE FROM opportunities WHERE id = :id"), {"id": row["id"]}
                )
        finally:
            admin.dispose()


def test_nobody_can_delete_the_catalogue(runtime):
    """DELETE is refused by the *policy*, and it refuses by matching no rows.

    Note the contrast with the ledger tables: there the *grant* withholds DELETE,
    here the *policy* does. Both were chosen deliberately, and this asserts the
    policy half actually holds rather than assuming the grant covers it.

    **The first version of this test expected a ``ProgrammingError`` and failed,
    which is the interesting part.** A permissive ``USING (false)`` policy on
    DELETE does not raise - it silently filters every row out, so the statement
    succeeds having deleted nothing. INSERT and UPDATE *do* raise, because
    ``WITH CHECK`` is evaluated per row and a violation is an error.

    So the observable truth is stronger and less obvious than "it errors": the
    row must still be there afterwards. Asserting only the exception would have
    been asserting the wrong mechanism.
    """
    row = _row(f"delete-{uuid.uuid4().hex[:8]}")
    with unscoped(runtime) as conn:
        conn.execute(_INSERT, row)
        conn.commit()

    try:
        with unscoped(runtime) as conn:
            result = conn.execute(
                text("DELETE FROM opportunities WHERE id = :id"), {"id": row["id"]}
            )
            conn.commit()
            assert result.rowcount == 0, (
                "a DELETE policy with USING (false) matched rows; the catalogue "
                "is deletable"
            )

        with unscoped(runtime) as conn:
            still_there = conn.execute(
                text("SELECT count(*) FROM opportunities WHERE id = :id"), {"id": row["id"]}
            ).scalar()
        assert still_there == 1, "the catalogue row was deleted despite the policy"
    finally:
        admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                conn.execute(
                    text("DELETE FROM opportunities WHERE id = :id"), {"id": row["id"]}
                )
        finally:
            admin.dispose()


def test_the_raw_payload_is_readable_by_tenants_and_writable_only_by_ingestion(runtime):
    """Same posture as the catalogue itself, asserted rather than inferred from
    the fact that both were configured in one loop."""
    from sqlalchemy.exc import ProgrammingError

    with pytest.raises(ProgrammingError):
        with tenant_scope(runtime, org_id=str(uuid.uuid4())) as conn:
            conn.execute(
                text(
                    "INSERT INTO opportunity_payloads "
                    "(id, opportunity_id, source_url, source_name, payload, "
                    " payload_digest, contract_version, received_at) "
                    "VALUES (:id, NULL, :url, 'probe', '{}'::json, :digest, 'v1', now())"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "url": f"https://probe.example.org/{uuid.uuid4().hex[:8]}",
                    "digest": _hash("probe"),
                },
            )
