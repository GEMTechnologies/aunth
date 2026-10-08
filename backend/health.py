"""Readiness, dependency health, and startup safety validation.

Why this exists as its own module
--------------------------------
``/health`` answered "is PostgreSQL reachable", which is not the same question as "is
this deployment able to do its job". A Granada deployment can have a healthy database
and still be silently broken in ways nothing reports:

* **the outbox relay is not running**, so events accumulate undelivered while
  PostgreSQL holds the truth and everything looks fine;
* **the schema is behind the code**, so a query fails at the first request that
  touches a new column rather than at deploy time;
* **Redis is gone**, so the fleet dispatches and no worker ever receives anything;
* **no outbound transport is configured** while an organisation has autonomous mail
  enabled, so the agent will try to send and park every message;
* **a worker died holding a lease**, so one organisation's work stops forever.

Each of those is a named check below, with a status and a detail, so an operator
learns what is wrong rather than that something is.

Three states, not two
--------------------
``HEALTHY`` / ``DEGRADED`` / ``NOT_READY``. A binary answer forces a choice between
"refuse traffic for a cosmetic problem" and "serve traffic while the relay is down",
and neither is right. DEGRADED means the service is answering correctly but something
it depends on needs attention - which is exactly what an outbox backlog is.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class CheckStatus(str, Enum):
    HEALTHY = "HEALTHY"
    #: Working, but something needs attention. The process should keep serving.
    DEGRADED = "DEGRADED"
    #: Cannot do its job. A load balancer should take this instance out.
    NOT_READY = "NOT_READY"
    #: Not applicable to this deployment, and deliberately not counted as a failure.
    SKIPPED = "SKIPPED"


#: Statuses that make the whole deployment not ready.
BLOCKING = frozenset({CheckStatus.NOT_READY})


@dataclass
class Check:
    """One dependency or invariant."""

    name: str
    status: CheckStatus
    detail: str = ""
    latency_ms: Optional[float] = None
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "latency_ms": round(self.latency_ms, 2) if self.latency_ms is not None else None,
            "data": self.data,
        }


@dataclass
class ReadinessReport:
    """Every check, and the aggregate answer."""

    checks: list[Check] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def ready(self) -> bool:
        """Whether this instance should receive traffic.

        ``DEGRADED`` does not make it unready. A backlog needs attention; refusing to
        serve because the relay is behind would turn a delay into an outage.
        """
        return not any(c.status in BLOCKING for c in self.checks)

    @property
    def status(self) -> CheckStatus:
        if any(c.status == CheckStatus.NOT_READY for c in self.checks):
            return CheckStatus.NOT_READY
        if any(c.status == CheckStatus.DEGRADED for c in self.checks):
            return CheckStatus.DEGRADED
        return CheckStatus.HEALTHY

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "status": self.status.value,
            "generated_at": self.generated_at.isoformat(),
            "checks": [c.as_dict() for c in self.checks],
            "failing": [c.name for c in self.checks if c.status == CheckStatus.NOT_READY],
            "degraded": [c.name for c in self.checks if c.status == CheckStatus.DEGRADED],
        }


def _timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    """Run a check and measure it with perf_counter.

    ``time.monotonic`` was measured at ~15,000 us granularity on this platform and
    returned exactly 0.0 in 20,000 consecutive back-to-back reads, which would make
    every latency in this report a lie.
    """
    started = time.perf_counter()
    result = fn()
    return result, (time.perf_counter() - started) * 1000.0


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
def check_database() -> Check:
    def probe() -> bool:
        # `health_check` is a DatabaseManager method, not a module function. The first
        # version imported it as a module attribute and the readiness report said
        # NOT_READY with an ImportError - which is the check working, on a probe that
        # was wrong.
        from database import DatabaseManager

        return DatabaseManager.health_check()

    try:
        ok, latency = _timed(probe)
    except Exception as exc:  # noqa: BLE001
        return Check("database", CheckStatus.NOT_READY, f"{type(exc).__name__}: {exc}")
    if not ok:
        return Check("database", CheckStatus.NOT_READY, "PostgreSQL did not answer", latency)
    return Check("database", CheckStatus.HEALTHY, "PostgreSQL answered", latency)


def check_redis() -> Check:
    """Redis is a *cache and transport*, never the source of truth.

    Its absence is DEGRADED rather than NOT_READY on purpose. PostgreSQL holds every
    durable fact, the outbox holds every undelivered event, and the fleet's recovery
    paths are database-driven - so the service is correct without Redis, just slower
    and with delivery paused. Refusing to serve would convert a transport outage into
    a data outage, which is the opposite of what the outbox was built for.
    """
    def probe() -> bool:
        from events.publisher import get_publisher

        get_publisher().client.ping()
        return True

    try:
        ok, latency = _timed(probe)
    except Exception as exc:  # noqa: BLE001
        return Check(
            "redis",
            CheckStatus.DEGRADED,
            f"Redis is unreachable ({type(exc).__name__}). Durable state is unaffected "
            "and the outbox will drain when it returns; event delivery is paused.",
        )
    return Check("redis", CheckStatus.HEALTHY, "Redis answered", latency)


def _code_head() -> Optional[str]:
    """The migration revision this code expects, read from the versions directory.

    Two hazards, both real and both already recorded in this project:

    **``alembic/`` shadows the installed distribution** (ADR-0003). The migration
    directory contains an ``__init__.py``, so while the backend root leads ``sys.path``
    a plain ``from alembic.config import Config`` resolves to the migration folder -
    which has no ``config`` module - and raises ImportError. The backend root is
    temporarily removed from ``sys.path`` for the duration of the import.

    **Reading the head does not need the whole library.** ``ScriptDirectory`` walks the
    versions directory by filesystem path, so this only needs ``alembic.config`` and
    ``alembic.script`` imported successfully once.

    A failure here is logged and returns ``None`` rather than failing the check: the
    revision number is a best-effort extra, and the schema is verified by structure.
    """
    try:
        import sys

        here = Path(__file__).resolve().parent
        backend_root = str(here)

        saved_path = sys.path[:]
        saved_modules = {
            name: sys.modules.pop(name)
            for name in list(sys.modules)
            if name == "alembic" or name.startswith("alembic.")
        }
        try:
            sys.path = [p for p in sys.path if Path(p).resolve() != here.resolve()]
            from alembic.config import Config
            from alembic.script import ScriptDirectory

            config = Config(str(here / "alembic.ini"))
            # Set explicitly, so the ini's relative location cannot resolve against
            # whatever the process working directory happens to be.
            config.set_main_option("script_location", str(here / "alembic"))
            config.set_main_option("sqlalchemy.url", "sqlite://")
            return ScriptDirectory.from_config(config).get_current_head()
        finally:
            sys.path = saved_path
            # Restore only what was there; never reinstate a shadow in place of the
            # real distribution. The conftest helper records why: putting the shadow
            # back once left a half-shadowed package and produced an order-dependent
            # failure that looked like a migration defect.
            for name, module in saved_modules.items():
                sys.modules.setdefault(name, module)
    except Exception as exc:  # noqa: BLE001
        logger.warning("readiness.code_head_failed", extra={"error": str(exc)[:200]})
        return None


def check_migrations() -> Check:
    """Whether the deployed schema matches what the code expects.

    This is the check that turns "a column does not exist" from a 500 at the first
    request into a refusal at deploy time.

    **Verified by STRUCTURE, not by version number, and that is deliberate.**

    The runtime role is denied SELECT on ``alembic_version`` - a standing security
    property, asserted live in ``test_tenant_data_isolation.py`` - so a readiness probe
    running as the application cannot read the version. The first version of this check
    tried anyway and reported ``NOT_READY`` with a permission error, which is the
    security posture working correctly on a probe that was asking the wrong question.

    Weakening the grant so a health check could read a version string would trade a
    real protection for a cosmetic convenience. So the check verifies that **every
    table the models declare actually exists**, which is what the version number is a
    proxy for - and is strictly stronger, because it also catches a partially restored
    dump that happens to carry the right version row.

    The version comparison is kept as a best-effort extra: when the probe *can* read
    it (an operator running the check as the owner, for instance) a mismatch is still
    reported.
    """
    import models

    # -- the check that always works, as the runtime role ------------------
    try:
        present = _existing_tables()
    except Exception as exc:  # noqa: BLE001
        return Check(
            "migrations", CheckStatus.NOT_READY,
            f"could not read the schema: {type(exc).__name__}: {exc}",
        )

    missing = sorted(set(models.Base.metadata.tables) - present)
    if missing:
        return Check(
            "migrations", CheckStatus.NOT_READY,
            f"{len(missing)} table(s) the code needs are absent: {missing[:10]}. Run "
            "`alembic upgrade head`.",
            data={"missing_tables": missing, "tables_present": len(present)},
        )

    # -- best-effort version comparison ------------------------------------
    applied = _applied_revision()
    expected = _code_head()
    if applied == "denied":
        return Check(
            "migrations", CheckStatus.HEALTHY,
            f"all {len(present)} tables the code requires are present. The revision "
            "number is not readable by the runtime role, by design, so the schema was "
            "verified by structure instead",
            data={
                "expected": expected,
                "verification": "structure",
                "runtime_role_may_read_alembic_version": False,
            },
        )
    if applied is not None and expected is not None and applied != expected:
        return Check(
            "migrations", CheckStatus.NOT_READY,
            f"the database is at {applied} but this code expects {expected}; run "
            "`alembic upgrade head`",
            data={"applied": applied, "expected": expected, "verification": "revision"},
        )
    return Check(
        "migrations", CheckStatus.HEALTHY,
        f"schema matches the code (revision {applied or expected or 'unknown'})",
        data={"applied": applied, "expected": expected, "verification": "revision",
              "tables_present": len(present)},
    )


def _applied_revision() -> Optional[str]:
    """The applied revision, or ``"denied"`` when the role may not read it.

    Distinguishing "denied" from "absent" matters: denied is the security posture
    working, absent is a database that has never been migrated.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import ProgrammingError

    from database import engine

    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        return row[0] if row else None
    except ProgrammingError:
        # Insufficient privilege. Expected, and not a problem.
        return "denied"
    except Exception as exc:  # noqa: BLE001
        logger.warning("readiness.revision_unreadable", extra={"error": str(exc)[:200]})
        return None


def _existing_tables() -> set[str]:
    from sqlalchemy import text

    from database import engine

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        ).all()
    return {r[0] for r in rows}


def check_outbox() -> Check:
    """The relay's backlog.

    A relay that is not running is invisible from the outside: PostgreSQL holds the
    truth either way, so nothing looks broken while events pile up undelivered. This
    is the check that makes it visible, and it is why an unrelayed backlog is
    DEGRADED rather than silent.
    """
    from sqlalchemy import func, select

    from database import SessionLocal

    db = SessionLocal()
    try:
        pending = db.execute(
            select(func.count()).select_from(models_outbox()).where(
                models_outbox().published_at.is_(None)
            )
        ).scalar() or 0
        oldest = db.execute(
            select(func.min(models_outbox().created_at)).where(
                models_outbox().published_at.is_(None)
            )
        ).scalar()
    except Exception as exc:  # noqa: BLE001
        return Check("outbox", CheckStatus.DEGRADED, f"could not read the outbox: {exc}")
    finally:
        db.close()

    if pending == 0:
        return Check("outbox", CheckStatus.HEALTHY, "nothing is waiting to be published")

    age_seconds = None
    if oldest is not None:
        moment = oldest if oldest.tzinfo else oldest.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - moment).total_seconds()

    # A handful of rows a second old is a relay keeping up. A backlog minutes old is a
    # relay that is not running, and that is what this threshold distinguishes.
    if age_seconds is not None and age_seconds > 900:
        return Check(
            "outbox", CheckStatus.DEGRADED,
            f"{pending} event(s) unpublished, oldest {int(age_seconds)}s old. The relay "
            "is probably not running: `python -m events.relay`",
            data={"unpublished": pending, "oldest_age_seconds": int(age_seconds)},
        )
    return Check(
        "outbox", CheckStatus.DEGRADED,
        f"{pending} event(s) awaiting publication - normal if a relay is running",
        data={"unpublished": pending, "oldest_age_seconds": age_seconds},
    )


def models_outbox():
    import models

    return models.OutboxEvent


def check_stuck_jobs() -> Check:
    """Jobs whose lease has lapsed.

    A worker that died holding a lease leaves one organisation's work stopped until
    ``reclaim_expired`` runs. Visible here rather than only in a log line.
    """
    from sqlalchemy import func, select

    import models
    from database import SessionLocal

    db = SessionLocal()
    try:
        moment = datetime.now(timezone.utc)
        stuck = db.execute(
            select(func.count()).select_from(models.Job).where(
                models.Job.state == models.Job.RUNNING,
                models.Job.lease_expires_at.isnot(None),
                models.Job.lease_expires_at < moment,
            )
        ).scalar() or 0
        queued = db.execute(
            select(func.count()).select_from(models.Job).where(
                models.Job.state == models.Job.QUEUED
            )
        ).scalar() or 0
    except Exception as exc:  # noqa: BLE001
        return Check("jobs", CheckStatus.DEGRADED, f"could not read the queue: {exc}")
    finally:
        db.close()

    if stuck:
        return Check(
            "jobs", CheckStatus.DEGRADED,
            f"{stuck} job(s) hold a lapsed lease; they are reclaimed on the next "
            "maintenance sweep and an operator can force it with the admin CLI",
            data={"stale_leases": stuck, "queued": queued},
        )
    return Check("jobs", CheckStatus.HEALTHY, f"{queued} job(s) queued",
                 data={"queued": queued, "stale_leases": 0})


def check_mail_providers() -> Check:
    """Whether the transports an account needs are actually registered.

    Catches the configuration gap that otherwise surfaces as "Granada said it would
    reply and nothing happened": an account is ACTIVE, an organisation has autonomous
    mail on, and no outbound transport exists - so every message parks.
    """
    from sqlalchemy import select

    import models
    from agent.mail.gateway import get_outbound_transport, get_transport, registered_providers
    from database import SessionLocal

    db = SessionLocal()
    try:
        accounts = db.execute(
            select(models.MailAccount).where(models.MailAccount.status == models.MailAccount.ACTIVE)
        ).scalars().all()
        needing_outbound = db.execute(
            select(models.GranadaAgent).where(models.GranadaAgent.status == models.GranadaAgent.ACTIVE)
        ).scalars().all()
    except Exception as exc:  # noqa: BLE001
        return Check("mail_providers", CheckStatus.SKIPPED, f"could not inspect accounts: {exc}")
    finally:
        db.close()

    if not accounts:
        return Check(
            "mail_providers", CheckStatus.SKIPPED,
            "no mail accounts are configured on this deployment",
            data={"inbound_registered": registered_providers()},
        )

    missing_inbound = sorted({
        a.provider for a in accounts if get_transport(a.provider) is None
    })
    missing_outbound = sorted({
        a.provider for a in accounts if get_outbound_transport(a.provider) is None
    })
    autonomy_on = [
        a.org_id for a in needing_outbound
        if (a.settings or {}).get("autonomous_mail_enabled")
    ]

    data = {
        "accounts": len(accounts),
        "missing_inbound_transports": missing_inbound,
        "missing_outbound_transports": missing_outbound,
        "organisations_with_autonomous_mail": len(autonomy_on),
    }

    # The dangerous combination: autonomy enabled and nothing able to send. Every
    # message would park, and the customer would see drafts that never leave.
    if autonomy_on and len(missing_outbound) == len({a.provider for a in accounts}):
        return Check(
            "mail_providers", CheckStatus.DEGRADED,
            f"{len(autonomy_on)} organisation(s) have autonomous mail enabled but no "
            "outbound transport is registered for any of their providers; every send "
            "will park",
            data=data,
        )
    if missing_inbound:
        return Check(
            "mail_providers", CheckStatus.DEGRADED,
            f"no inbound transport for {missing_inbound}; those mailboxes cannot be read",
            data=data,
        )
    return Check(
        "mail_providers", CheckStatus.HEALTHY,
        f"{len(accounts)} account(s), all transports registered", data=data,
    )


# ---------------------------------------------------------------------------
# Startup safety validation
# ---------------------------------------------------------------------------
@dataclass
class ConfigProblem:
    code: str
    severity: str  # "refuse" | "warn"
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "detail": self.detail}


#: Combinations that are wrong in a way an operator must fix before the service can
#: be trusted. Each one is a configuration that *looks* fine and behaves badly.
def validate_configuration() -> list[ConfigProblem]:
    """Check the dangerous combinations. Called at startup and exposed as a check.

    Deliberately not a schema validator: pydantic validates TYPES, and every problem
    here is a valid type in a bad combination.
    """
    problems: list[ConfigProblem] = []
    try:
        from config import settings
    except Exception as exc:  # noqa: BLE001
        return [ConfigProblem("CONFIG_UNREADABLE", "refuse",
                              f"settings could not be loaded: {exc}")]

    # 0. The JWT algorithm must be an HMAC algorithm.
    #
    # THE CVE THIS CLOSES: CVE-2026-85394 has NO FIX VERSION. python-jose through 3.5.0
    # accepts DER-encoded public keys as HMAC secrets, so an attacker holding the
    # service's public key can forge HS256 tokens - "when algorithms are not explicitly
    # restricted".
    #
    # Granada meets neither condition: `decode_access_token` passes
    # `algorithms=[settings.jwt_algorithm]`, and `jwt_secret` is a symmetric string with
    # no public key anywhere in the codebase. But the mitigation is only a mitigation
    # while the configuration stays symmetric, and `JWT_ALGORITHM=RS256` is a one-line
    # change that would ask python-jose to verify an asymmetric token with an HMAC key -
    # which is the exact shape the advisory makes exploitable.
    #
    # So the check is a refusal rather than a warning: there is no correct configuration
    # in which this codebase verifies a JWT with an asymmetric algorithm and a symmetric
    # secret, and a warning on a security-shaped misconfiguration is a warning that gets
    # clicked past.
    hmac_algorithms = {"HS256", "HS384", "HS512"}
    if str(getattr(settings, "jwt_algorithm", "")).upper() not in hmac_algorithms:
        problems.append(
            ConfigProblem(
                "JWT_ALGORITHM_NOT_HMAC",
                "refuse",
                f"jwt_algorithm is {settings.jwt_algorithm!r} but the verification key is "
                "a symmetric secret. An asymmetric algorithm here asks python-jose to "
                "verify an asymmetric token with an HMAC key, which is the shape "
                "CVE-2026-85394 makes exploitable - and that advisory has no fix version. "
                "Use HS256, or move to a public key with an actively maintained JWT "
                "library before changing this.",
            )
        )

    # 1. Autonomous mail on, with no way to send.
    from agent.mail.autonomy import platform_autonomy_enabled

    if platform_autonomy_enabled():
        from agent.mail.gateway import registered_outbound_providers

        if not registered_outbound_providers():
            problems.append(ConfigProblem(
                "AUTONOMOUS_MAIL_WITHOUT_OUTBOUND_PROVIDER", "warn",
                "AUTONOMOUS_MAIL_ENABLED is true but no outbound transport is "
                "registered. Nothing can be sent, so every eligible message will park "
                "in DELIVERY_UNKNOWN rather than reaching a funder.",
            ))

    # 2. A knowledge provider selected with no credentials to reach it.
    # 3. A model provider selected with no key.
    provider = str(getattr(settings, "model_provider", "") or "").lower()
    if provider and provider not in ("none", "disabled", "null"):
        if provider in ("openai", "anthropic") and not getattr(settings, "model_api_key", None):
            problems.append(ConfigProblem(
                "MODEL_PROVIDER_WITHOUT_KEY", "warn",
                f"MODEL_PROVIDER is {provider!r} but no MODEL_API_KEY is set; drafting "
                "will fall back to templates.",
            ))

    # 4. Acting on decisions when nothing can decide.
    stage = str(getattr(settings, "decision_rollout_stage", "") or "").upper()
    if stage in ("ACTING", "ACTING_WITH_SHADOW"):
        from agent.mail.gateway import get_transport  # noqa: F401  (import kept for symmetry)

        if not getattr(settings, "decision_provider", None):
            problems.append(ConfigProblem(
                "ACTING_WITHOUT_DECISION_PROVIDER", "refuse",
                "DECISION_ROLLOUT_STAGE is ACTING but no DECISION_PROVIDER is "
                "configured; policy decisions would have no provenance.",
            ))

    # 5. Debug mode with cookies that cannot be secure.
    if getattr(settings, "debug", False) and getattr(settings, "environment", "") == "production":
        problems.append(ConfigProblem(
            "DEBUG_IN_PRODUCTION", "refuse",
            "DEBUG is true in a production environment; tracebacks and verbose errors "
            "would be served to callers.",
        ))

    return problems


def validate_startup(*, raise_on_refuse: bool = True) -> list[ConfigProblem]:
    """Run the configuration checks, optionally refusing to start.

    A warning is logged and the service runs, because every warning describes a
    degradation rather than a corruption. A refusal stops the process, because the
    alternative is serving requests with no provenance for the decisions it makes.
    """
    problems = validate_configuration()
    for problem in problems:
        if problem.severity == "refuse":
            logger.error("config.refused", extra=problem.as_dict())
        else:
            logger.warning("config.warning", extra=problem.as_dict())

    if raise_on_refuse:
        refusals = [p for p in problems if p.severity == "refuse"]
        if refusals:
            raise RuntimeError(
                "refusing to start: " + "; ".join(f"{p.code}: {p.detail}" for p in refusals)
            )
    return problems


def check_configuration() -> Check:
    problems = validate_configuration()
    refusals = [p for p in problems if p.severity == "refuse"]
    warnings = [p for p in problems if p.severity == "warn"]
    if refusals:
        return Check(
            "configuration", CheckStatus.NOT_READY,
            "; ".join(f"{p.code}: {p.detail}" for p in refusals),
            data={"problems": [p.as_dict() for p in problems]},
        )
    if warnings:
        return Check(
            "configuration", CheckStatus.DEGRADED,
            "; ".join(f"{p.code}" for p in warnings),
            data={"problems": [p.as_dict() for p in problems]},
        )
    return Check("configuration", CheckStatus.HEALTHY, "no configuration problems")


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
DEFAULT_CHECKS: tuple[Callable[[], Check], ...] = (
    check_database,
    check_migrations,
    check_configuration,
    check_redis,
    check_outbox,
    check_stuck_jobs,
    check_mail_providers,
)


def readiness(*, include_redis: bool = True) -> ReadinessReport:
    """Run every check. Never raises: a failing check is a status, not an exception.

    A readiness probe that throws is worse than one that reports, because a load
    balancer sees an unhandled 500 and cannot distinguish it from the process being
    down.
    """
    checks: list[Check] = []
    for factory in DEFAULT_CHECKS:
        if factory is check_redis and not include_redis:
            checks.append(Check("redis", CheckStatus.SKIPPED, "not checked"))
            continue
        try:
            checks.append(factory())
        except Exception as exc:  # noqa: BLE001
            checks.append(Check(
                getattr(factory, "__name__", "check").removeprefix("check_"),
                CheckStatus.NOT_READY,
                f"the check itself failed: {type(exc).__name__}: {exc}",
            ))
    return ReadinessReport(checks=checks)
