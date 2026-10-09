"""The privilege guard a browser worker must pass before it is allowed to run.

WHY THIS EXISTS, AND WHY IT IS A REFUSAL RATHER THAN A CONVENTION
-----------------------------------------------------------------
Section 12 and ADR-0011. Measured on production:

    granada_fleet  bypassrls = TRUE    26+ tables carry RLS, including documents, applications,
                                        mail_approvals and jobs
    granada_app    bypassrls = FALSE   the narrow role that already exists

The executor and relay connect as `granada_fleet`, because `jobs` is FORCE ROW LEVEL SECURITY and
discovery must see work across every tenant. That is correct FOR DISCOVERY.

The browser worker is different. It opens a live funder portal, reads an organisation's documents,
holds their credentials and types their data into a form. It is the single component most able to leak
one NGO into another, and it has NO need to see across tenants at all - everything it needs arrives in
its task payload.

So a worker that could connect as `granada_fleet` would be a privilege escalation by configuration:
the widest role in the system, attached to the component with the least need for it. This module makes
that a startup refusal instead of a code review.

WHAT IT CHECKS, AND WHAT IT DELIBERATELY DOES NOT
-------------------------------------------------
It reads the worker's OWN environment. It does not connect to the database to inspect roles, because a
worker that opens a database connection in order to decide whether it should have one has already
opened it. The check is cheap, local, and happens before anything else.

It cannot detect a role granted AFTER startup. That is a known limit and is recorded rather than
implied away - the guard closes the configuration door, not every possible door.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

#: Environment variables that carry a database connection. The worker needs NONE of them: its task
#: arrives as JSON on stdin and its credentials arrive separately.
DATABASE_ENV_VARS: tuple[str, ...] = (
    "FLEET_DATABASE_URL",
    "GRANADA_ADMIN_DATABASE_URL",
    "DATABASE_URL",
)

#: The role whose unfiltered visibility the worker must never hold.
WIDE_ROLE = "granada_fleet"
#: The role the worker may use, if it needs one at all.
NARROW_ROLE = "granada_app"


class WorkerPrivilegeError(RuntimeError):
    """The worker's environment would give it more visibility than its job requires."""


@dataclass(frozen=True)
class PrivilegeDecision:
    permitted: bool
    because: str
    #: Environment variables that had to be refused. Names only - never values, which may contain
    #: credentials, and which have no business in a log line or a report.
    refused: tuple[str, ...] = ()


def check_worker_privileges(environ: Optional[Mapping[str, str]] = None) -> PrivilegeDecision:
    """Whether this process may run as a browser worker.

    Defaults to the real environment. An explicit mapping is accepted so the rule is testable without
    mutating the process's own configuration - which is what a test that sets `FLEET_DATABASE_URL`
    would otherwise have to do, and would then have to undo.
    """
    env = os.environ if environ is None else environ
    present = tuple(name for name in DATABASE_ENV_VARS if (env.get(name) or "").strip())

    if not present:
        return PrivilegeDecision(
            permitted=True,
            because=(
                "the worker holds no database connection, which is the intended shape: its task "
                "arrives as JSON and it needs no cross-tenant visibility at all"
            ),
        )

    refused = tuple(name for name in present if _is_wide(env.get(name) or ""))
    if refused:
        return PrivilegeDecision(
            permitted=False,
            because=(
                f"the worker's environment carries {', '.join(refused)}, which connects as "
                f"{WIDE_ROLE} - the BYPASSRLS role that sees every organisation's documents. The "
                "browser opens a live portal, reads tenant documents and types tenant data into a "
                "form; giving it the widest role in the system is a privilege escalation by "
                "configuration, and ADR-0011 forbids it."
            ),
            refused=refused,
        )

    # A narrow connection is not what the worker needs, but it is not the escalation this guard
    # exists to prevent. Permitted, and named so an operator can see which it was.
    return PrivilegeDecision(
        permitted=True,
        because=(
            f"a database URL is present but none connects as {WIDE_ROLE}; the worker may proceed on "
            f"a narrow role such as {NARROW_ROLE}"
        ),
        refused=(),
    )


def _is_wide(url: str) -> bool:
    """Whether a connection string names the wide role.

    Matches the ROLE, not the host or the database: `postgresql://granada_fleet@db/granada_auth` and
    `postgresql://user:pass@db/x?role=granada_fleet` are both escalations, and a check that only
    looked at the scheme would miss the second.
    """
    lowered = url.lower()
    return f"//{WIDE_ROLE}:" in lowered or f"//{WIDE_ROLE}@" in lowered or f"role={WIDE_ROLE}" in lowered


def assert_worker_privileges(environ: Optional[Mapping[str, str]] = None) -> PrivilegeDecision:
    """`check_worker_privileges`, but refusing rather than reporting.

    Called at worker startup, before a browser is launched. A worker that cannot prove it is narrow
    does not open a page at all.
    """
    decision = check_worker_privileges(environ)
    if not decision.permitted:
        raise WorkerPrivilegeError(decision.because)
    return decision


def describe() -> dict[str, Any]:
    """The rule, stated where a reviewer will find it."""
    return {
        "wide_role": WIDE_ROLE,
        "narrow_role": NARROW_ROLE,
        "refused_env_vars": list(DATABASE_ENV_VARS),
        "rule": (
            "the browser worker must not connect as granada_fleet; it needs no cross-tenant "
            "visibility, and the role that provides it is the widest in the system"
        ),
        "checked_at": "startup, from the worker's own environment, before any browser is launched",
        "known_limit": (
            "it cannot detect a role granted after startup - it closes the configuration door, not "
            "every possible door"
        ),
        "does_not_do": [
            "it does not connect to the database to inspect roles; a worker that opens a connection "
            "to decide whether it should have one has already opened it",
            "it does not log connection strings - names only, since a URL may carry credentials",
        ],
    }
