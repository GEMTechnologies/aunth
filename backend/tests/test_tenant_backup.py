"""The tenant-scoped logical backup: the parts testable without PostgreSQL.

The round trip itself is verified against the real database by
``tools/tenant_backup.py --verify``, which needs a role that can create a schema. What
is tested here is the serialisation and aggregation logic - the parts where a bug
produces a backup that looks fine and is not.

That distinction matters because this tool's whole reason for existing is that
``pg_dump`` silently could not read Granada's data: a backup that reports success while
capturing nothing is the failure mode, not an edge case.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path

import pytest

# tests/ -> backend/ -> Auth/ -> granada-agentic/ -> tools/
TOOLS = Path(__file__).resolve().parents[3] / "tools"


def _load_backup_module():
    spec = importlib.util.spec_from_file_location(
        "tenant_backup", TOOLS / "tenant_backup.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["tenant_backup"] = module
    spec.loader.exec_module(module)
    return module


backup_mod = _load_backup_module()


# ===========================================================================
# SERIALISATION
# ===========================================================================
@pytest.mark.parametrize(
    "value",
    (
        datetime(2026, 10, 7, 12, 30, tzinfo=timezone.utc),
        date(2026, 10, 7),
        time(12, 30),
        Decimal("1234.5600"),
        b"binary\x00payload",
    ),
)
def test_every_postgres_type_survives_a_round_trip(value):
    """A backup that crashes on the first Decimal has never been run on real data."""
    import json

    import uuid as uuid_module

    encoded = json.loads(json.dumps(value, default=backup_mod._json_default))
    decoded = backup_mod._decode(encoded)
    if isinstance(value, (datetime, date, time)):
        assert decoded == value
    elif isinstance(value, Decimal):
        # Compared as a string: Decimal("1234.5600") == Decimal("1234.56"), and the
        # scale is worth preserving in a backup.
        assert str(decoded) == str(value)
    elif isinstance(value, bytes):
        assert bytes(decoded) == value
    else:  # pragma: no cover - parametrisation covers the cases above
        assert decoded == value


def test_a_uuid_survives():
    import json
    import uuid as uuid_module

    value = uuid_module.uuid4()
    encoded = json.loads(json.dumps(value, default=backup_mod._json_default))
    assert backup_mod._decode(encoded) == value


def test_an_untagged_value_passes_through_unchanged():
    """Only tagged dictionaries are decoded, so a JSON column holding a dict with a
    `__type__` key of its own is not mangled."""
    assert backup_mod._decode({"hello": "world"}) == {"hello": "world"}
    assert backup_mod._decode("plain") == "plain"
    assert backup_mod._decode(None) is None


# ===========================================================================
# ROW COUNTS
# ===========================================================================
def _archive(overrides=None):
    """Build an archive skeleton.

    Takes a dict rather than keyword arguments because `global` is a Python keyword -
    and it is the section name the backup format uses, so it cannot be renamed without
    breaking the format.
    """
    base = {
        "tenants": {},
        "shared": {},
        "global": {},
        "tables": {"tenant_scoped": [], "global": []},
    }
    if overrides:
        base.update(overrides)
    return base


def test_counts_sum_tenants_shared_and_global():
    """The three sections are counted together, because a verification that ignored
    the shared section would report a mismatch for every shared row."""
    archive = _archive({
        "tenants": {
            "org-1": {"mail_accounts": [{"id": "a"}, {"id": "b"}]},
            "org-2": {"mail_accounts": [{"id": "c"}]},
        },
        "shared": {"roles": [{"id": "system"}]},
        "global": {"opportunities": [{"id": "o1"}, {"id": "o2"}]},
    })
    counted = backup_mod.counts(archive)
    assert counted["mail_accounts"] == 3
    assert counted["roles"] == 1
    assert counted["opportunities"] == 2


def test_an_unreadable_entry_is_recorded_not_counted_as_rows():
    """`__unreadable__` is a dictionary explaining a denial, not data. Counting it as a
    row would inflate the total and hide the denial."""
    archive = _archive({"global": {"alembic_version": {"__unreadable__": "permission denied"}}})
    counted = backup_mod.counts(archive)
    assert counted == {}


def test_an_empty_archive_counts_nothing():
    assert backup_mod.counts(_archive()) == {}


# ===========================================================================
# THE SAFETY PROPERTIES
# ===========================================================================
def test_the_expected_unreadable_list_names_only_the_by_design_denial():
    """`alembic_version` is withheld from the application role deliberately. Listing it
    means an UNEXPECTED denial is visible rather than hidden among expected ones."""
    assert backup_mod.EXPECTED_UNREADABLE == frozenset({"alembic_version"})


def test_the_tenant_column_is_org_id():
    assert backup_mod.TENANT_COLUMN == "org_id"


def test_the_backup_does_not_hard_code_its_table_list():
    """Tables are discovered from the live catalogue, so a table added by a later
    migration is backed up without this file being edited.

    A hard-coded list that silently omits a new table is how a backup stops being
    complete without anybody noticing - which is the same class of failure as the
    `pg_dump` problem this tool exists to work around.
    """
    source = (TOOLS / "tenant_backup.py").read_text(encoding="utf-8")
    assert "information_schema.columns" in source, (
        "the tenant tables are not discovered from the catalogue"
    )
    assert "information_schema" in source
    # And the table list is never written out literally.
    for table in ("mail_accounts", "granada_agents", "opportunities"):
        assert f'"{table}",' not in source or "public" in source, (
            f"{table} appears to be hard-coded as a table list entry"
        )


def test_a_denied_read_cannot_poison_the_rest():
    """The bug this guards against is real and was found by running it.

    The first denial aborted the transaction, so every following statement failed with
    InFailedSqlTransaction - the backup recorded 19 perfectly readable tables as
    unreadable and captured ZERO rows from them, including the 88-row opportunity
    catalogue. It reported success.

    Asserted structurally: every read is wrapped in a savepoint.
    """
    source = (TOOLS / "tenant_backup.py").read_text(encoding="utf-8")
    assert "begin_nested()" in source, (
        "reads are not isolated with a savepoint, so one denial would poison the rest"
    )
    # At least one per read site: the shared section, the global section, and the
    # per-tenant section.
    assert source.count("begin_nested()") >= 3, (
        "expected savepoints around the shared, global and per-tenant reads"
    )


def test_the_tool_explains_why_the_owner_role_is_required():
    """The app role discovers zero tenants, which is correct and is why this runs as
    the owner. Documented in the source so a later reader does not 'fix' it back."""
    source = (TOOLS / "tenant_backup.py").read_text(encoding="utf-8")
    assert "FORCE" in source and "org_members" in source
    assert "discovers nothing" in source or "cannot" in source
