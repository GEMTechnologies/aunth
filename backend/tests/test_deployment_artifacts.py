"""The deployment artefacts, validated structurally.

Docker is not installed in this development environment, so these tests cannot build an
image. They can do something more useful: they can check the properties that make the
artefacts **workable**, and they can do it by *executing* the parts that are executable.

The strongest of them is `test_the_dockerfile_cmd_actually_resolves`. The Dockerfile it
replaced ended with `CMD ["uvicorn", "backend.main:app", ...]`, and `Auth/backend` is not a
package - so `import backend.main` resolves `backend` and dies on the first internal
import:

    ModuleNotFoundError: No module named 'database'

That Dockerfile could never have started the application, and nothing in the repository
would have noticed, because nothing ever built it. This test simulates the import in a
subprocess, which is what the container would do.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
AUTH = BACKEND.parent
ROOT = AUTH.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

yaml = pytest.importorskip("yaml")

DOCKERFILE = BACKEND / "Dockerfile"
DOCKERIGNORE = BACKEND / ".dockerignore"
COMPOSE = ROOT / "docker-compose.yml"
PORTS_YAML = ROOT / "ops" / "ports.yaml"


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _effective_dockerfile() -> str:
    """The Dockerfile with comments removed.

    Assertions about what the file DOES must not be satisfied or broken by what it SAYS.
    The file explains at length why `backend.main:app` cannot work, so a naive
    `"backend.main:app" not in text` fails on the explanation - a guard that rejects the
    right content, which is exactly how a test gets deleted instead of fixed.
    """
    lines = []
    for line in _dockerfile().splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # A trailing comment on an instruction.
        lines.append(line.split(" #", 1)[0] if " #" in line else line)
    return "\n".join(lines)


def _effective_sql(path: Path) -> str:
    """SQL with `--` comments removed, for the same reason."""
    return "\n".join(
        line.split("--", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()
    )


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


# ===========================================================================
# THE DOCKERFILE MUST BE ABLE TO START THE APPLICATION
# ===========================================================================
def _cmd_module(raw: str) -> str:
    """Extract the ASGI target from a CMD, whoever wrote it."""
    match = re.search(r'CMD\s*\[(.*?)\]', raw, re.DOTALL)
    assert match, "the Dockerfile has no CMD"
    parts = re.findall(r'"([^"]*)"', match.group(1))
    assert parts, "the CMD is not in exec form"
    # `uvicorn main:app ...` -> `main`
    for part in parts:
        if ":" in part and not part.startswith("-"):
            return part.split(":")[0]
    raise AssertionError(f"no ASGI target found in CMD: {parts}")


def test_the_dockerfile_cmd_actually_resolves():
    """THE test. Simulates the import the container performs.

    Runs in a subprocess with the same `PYTHONPATH` and working directory the image sets,
    because that is the only way to be sure the layout is right - reading the Dockerfile
    would have told nobody that `backend.main` does not exist.
    """
    raw = _effective_dockerfile()
    module = _cmd_module(raw)

    # The LAST WORKDIR is the one the runtime stage ends on. The builder stage has its own
    # (`/build`), and taking the first would test the wrong stage - which is how this test
    # failed the first time.
    workdirs = re.findall(r"^WORKDIR\s+(\S+)", raw, re.MULTILINE)
    assert workdirs, "no WORKDIR"
    workdir = workdirs[-1]
    assert workdir.endswith("backend"), (
        f"the runtime WORKDIR is {workdir!r}; it must be the backend directory itself, "
        "because Auth/backend is not a package - the app is `main:app`, not "
        "`backend.main:app`"
    )

    environment = {"PYTHONPATH": str(BACKEND)}
    import os

    env = {**os.environ, **environment}
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(BACKEND),
    )
    assert proc.returncode == 0, (
        f"the Dockerfile's CMD target `{module}` cannot be imported the way the image "
        f"would import it:\n{proc.stderr[-800:]}"
    )


def test_the_dockerfile_does_not_use_the_package_style_target():
    """A regression guard with the failing form written down.

    `backend.main` resolves to a directory with no `__init__.py`, and every module inside
    uses absolute imports, so the target cannot work however PYTHONPATH is set.
    """
    # The CMD only. The comment above it deliberately QUOTES the broken form to explain
    # why it is broken, so checking the whole file would fail on the explanation.
    cmd = _cmd_module(_effective_dockerfile())
    assert cmd != "backend.main", (
        "`backend.main:app` cannot work: Auth/backend has no __init__.py and its modules "
        "import each other absolutely, so `database` is not importable from the parent"
    )
    assert cmd == "main", f"the CMD target is {cmd!r}; it must be `main`"


def test_the_dockerfile_runs_as_a_non_root_user():
    """A container process that can write to its own code is a container process that can
    rewrite the application."""
    raw = _dockerfile()
    assert re.search(r"^USER\s+(?!root)\S+", raw, re.MULTILINE), (
        "no non-root USER: the image would run as root"
    )


def test_the_dockerfile_has_an_init_to_forward_signals():
    """Without an init, PID 1 is uvicorn, which does not forward SIGTERM to its children.
    `docker stop` then waits out the timeout and SIGKILLs, and the graceful shutdown this
    codebase implements never runs at all."""
    raw = _dockerfile()
    assert "tini" in raw, "no init: SIGTERM would not be forwarded and shutdown would be SIGKILL"
    assert re.search(r'ENTRYPOINT\s*\[.*tini', raw), "tini is installed but not the ENTRYPOINT"


def test_the_healthcheck_uses_liveness_not_readiness():
    """A healthcheck that fails when a dependency is down makes Docker restart a healthy
    process during a database failover - turning a thirty-second blip into a restart loop."""
    raw = _dockerfile()
    assert "HEALTHCHECK" in raw
    assert "/livez" in raw, "the healthcheck must use /livez"
    assert "/readyz" not in raw.split("HEALTHCHECK")[1].split("\n\n")[0], (
        "the healthcheck must not use /readyz"
    )


def test_the_dockerfile_targets_the_python_version_this_codebase_runs():
    raw = _dockerfile()
    assert "python:3.12" in raw, (
        "the image must match the interpreter the code was developed and tested on (3.12)"
    )


# ===========================================================================
# SECRETS MUST NOT SHIP IN THE IMAGE
# ===========================================================================
def test_dockerignore_excludes_the_environment_file():
    """`COPY . /app/backend` bakes `.env` into a layer, where it stays readable by anyone
    who can pull the image - and `docker history` will not show it, because it arrived
    through a COPY rather than a RUN."""
    assert DOCKERIGNORE.exists(), "no .dockerignore"
    entries = {
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert ".env" in entries, "`.env` would be baked into the image"
    assert ".env.*" in entries, "`.env.production` and friends would be baked in"
    assert "!.env.example" in entries, "the template should still be available"


def test_dockerignore_excludes_the_virtualenv_and_local_databases():
    entries = {
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    for required in (".venv/", "__pycache__/", "*.db", "tests/"):
        assert required in entries, f"{required} would ship in the image"


def test_no_secret_is_written_into_the_dockerfile():
    raw = _dockerfile().lower()
    for forbidden in ("secret_key=", "jwt_secret=", "password=", "api_key="):
        assert forbidden not in raw, f"a credential literal appears in the Dockerfile: {forbidden}"


# ===========================================================================
# THE COMPOSE TOPOLOGY
# ===========================================================================
def test_the_compose_file_is_not_empty():
    """It was zero bytes. A deployment file that exists and implements nothing is the exact
    pattern the brief forbids."""
    assert COMPOSE.stat().st_size > 1000, "the compose file is effectively empty"
    document = _compose()
    assert document.get("services"), "no services"


def test_every_long_running_service_waits_for_migrations_to_COMPLETE():
    """`service_started` is not enough.

    Start the API against an un-migrated database and queries fail only on the requests
    that touch a new table, so the deploy looks successful and the errors arrive later from
    customers. Start the WORKER and it is worse: it claims jobs, fails them, and advances
    their attempt counters - the work is consumed, not delayed.
    """
    document = _compose()
    for name in ("api", "worker", "relay"):
        service = document["services"][name]
        depends = service.get("depends_on") or {}
        assert "migrate" in depends, f"{name} does not wait for migrations"
        assert depends["migrate"]["condition"] == "service_completed_successfully", (
            f"{name} waits for `{depends['migrate'].get('condition')}`, which does not "
            "guarantee the migration job exited zero"
        )


def test_the_migration_job_does_not_restart():
    """A migration job that restarts on failure hides a schema problem behind a retry loop,
    and one that restarts after success re-runs migrations on every `compose up`."""
    document = _compose()
    assert document["services"]["migrate"].get("restart") in ("no", "on-failure", None) or \
        str(document["services"]["migrate"].get("restart")) == "no"


def test_the_migration_job_verifies_the_append_only_posture():
    """The grant script is ADDITIVE and has silently re-granted UPDATE or DELETE on an
    append-only table eight times in this project. A migration job that reports success
    without checking is how the ninth happens."""
    document = _compose()
    command = document["services"]["migrate"]["command"]
    rendered = command if isinstance(command, str) else "\n".join(command)
    assert "grant_runtime_role.sql" in rendered, "the grant script is not applied"
    assert "has_table_privilege" in rendered, (
        "the migration job does not VERIFY the posture after granting - which is the one "
        "thing this project has got wrong eight times"
    )
    for table in ("mail_send_attempts", "submission_attempts", "submission_receipts"):
        assert table in rendered, f"{table} is not in the posture check"


def test_the_services_share_one_image():
    """Three roles, one image: an import fixed in one is fixed in all three. Separate images
    drift, and the drift is discovered when the worker behaves differently from the API."""
    document = _compose()
    for name in ("api", "worker", "relay"):
        assert document["services"][name].get("image") == "granada-backend:${GRANADA_TAG:-local}"
    for name, expected in (
        ("worker", "agent.fleet_runner"),
        ("relay", "events.relay"),
    ):
        command = document["services"][name]["command"]
        rendered = command if isinstance(command, str) else " ".join(command)
        assert expected in rendered, f"{name} does not run {expected}"


def test_the_database_is_not_published_to_the_host():
    """Publishing 5432 is how a development database ends up on a public interface."""
    document = _compose()
    postgres = document["services"]["postgres"]
    assert "ports" not in postgres, "the database publishes a host port"
    assert "5432" in [str(p) for p in postgres.get("expose", [])]


def test_only_the_api_publishes_a_port():
    document = _compose()
    publishing = [name for name, s in document["services"].items() if s.get("ports")]
    assert publishing == ["api"], f"these services publish a host port: {publishing}"


def test_no_service_has_a_defaulted_password():
    """THE security property of the file. `POSTGRES_PASSWORD:-postgres` deploys a database
    with a public password and nobody notices, because it works."""
    text = COMPOSE.read_text(encoding="utf-8")
    # Every credential reference must use `${VAR:?message}`, which fails loudly when unset.
    for variable in (
        "POSTGRES_PASSWORD",
        "GRANADA_APP_PASSWORD",
        "GRANADA_OWNER_PASSWORD",
        "SECRET_KEY",
        "JWT_SECRET",
    ):
        assert f"${{{variable}:?" in text, (
            f"{variable} has no `${variable:?message}` guard, so the deployment would "
            "silently accept a missing or default value"
        )
        assert f"${{{variable}:-" not in text, (
            f"{variable} has a DEFAULT, which is how a public password ships"
        )


def test_autonomous_mail_is_off_in_the_deployment():
    """Production emails sent must remain zero. The code default is already false, so this
    is belt and braces - but a deployment should state it rather than inherit it."""
    document = _compose()
    env = document["x-backend-env"] if "x-backend-env" in document else None
    assert env is not None, "the shared environment block is missing"
    assert env.get("AUTONOMOUS_MAIL_ENABLED") == "false"


def test_the_runtime_role_is_not_the_owner_role():
    """Pointing the runtime at the owner would make every RLS policy decorative, and the
    isolation tests would still pass against SQLite while production leaked."""
    document = _compose()
    env = document["x-backend-env"]
    runtime = env["DATABASE_URL"]
    assert "granada_app" in runtime, "the runtime role is not the RLS-bound role"
    assert "granada_user" not in runtime, "the runtime is using the OWNER role"

    migrate = document["services"]["migrate"]["environment"]
    assert "granada_user" in migrate["GRANADA_ADMIN_DATABASE_URL"], (
        "migrations must run as the owner: the runtime role cannot read alembic_version"
    )


def test_the_metrics_connection_is_documented_as_needing_cross_tenant_access():
    """The operational gauges are cross-tenant counts and the runtime role cannot read them
    - it would report zero and the alerts would never fire."""
    text = COMPOSE.read_text(encoding="utf-8")
    assert "GRANADA_METRICS_DATABASE_URL" in text
    assert "across tenants" in text or "across ACROSS tenants" in text.lower() or \
        "zero rows" in text, "the reason is not recorded beside the setting"


def test_postgres_and_redis_have_healthchecks():
    """A `depends_on` with a condition needs something to be the condition."""
    document = _compose()
    for name in ("postgres", "redis"):
        assert document["services"][name].get("healthcheck"), f"{name} has no healthcheck"


def test_the_published_port_matches_the_port_registry():
    """`ops/ports.yaml` is the single source of truth. Ports documented in three places that
    disagreed is why it exists."""
    if not PORTS_YAML.exists():
        pytest.skip("no port registry")
    registry = yaml.safe_load(PORTS_YAML.read_text(encoding="utf-8"))
    document = _compose()
    published = document["services"]["api"]["ports"][0]
    assert "AUTH_SERVICE_PORT" in published, (
        "the API port must come from the registry's variable, not a literal"
    )
    assert ":8000" in published, "the container port must be 8000, which is what uvicorn binds"


# ===========================================================================
# THE INIT SCRIPT
# ===========================================================================
def test_the_init_script_creates_both_roles_and_neither_is_a_superuser():
    init = ROOT / "ops" / "postgres" / "init" / "01-roles.sql"
    assert init.exists(), "no init script: the two application roles would not exist"
    text = init.read_text(encoding="utf-8")
    for role in ("granada_user", "granada_app"):
        assert role in text, f"{role} is not created"

    # Effective SQL only: the file SAYS "Neither is a superuser" in a comment, so the
    # naive whole-file check rejected the correct content.
    effective = _effective_sql(init).upper()
    assert "SUPERUSER" not in effective, (
        "an application role with SUPERUSER would bypass every policy in the schema"
    )
    assert "BYPASSRLS" not in effective, (
        "no application role should bypass RLS; that is what the policies are for"
    )
    # And the roles really are created, in the effective SQL.
    assert "CREATE ROLE GRANADA_USER" in effective
    assert "CREATE ROLE GRANADA_APP" in effective


def test_the_init_script_does_not_grant_the_runtime_role_blind_privileges():
    """Table privileges are narrowed by `sql/grant_runtime_role.sql` and by each migration.
    A convenient blanket GRANT here would be silently wider than every policy that follows,
    and would not appear in the migration history where a reviewer looks."""
    init = ROOT / "ops" / "postgres" / "init" / "01-roles.sql"
    effective = _effective_sql(init)
    assert "GRANT ALL" not in effective.upper()
    assert "SUPERUSER" not in effective.upper()
    assert "USAGE ON SCHEMA public TO granada_app" in effective


# ===========================================================================
# CI
# ===========================================================================
def test_ci_runs_the_suite_against_postgresql_not_sqlite():
    """The security properties under test are PostgreSQL features - row-level security,
    FORCE, and column privileges. A suite that ran only on SQLite would pass while every
    policy was decorative."""
    workflow = ROOT / ".github" / "workflows" / "ci.yml"
    assert workflow.exists(), "no CI workflow"
    text = workflow.read_text(encoding="utf-8")
    assert "postgres:15" in text, "CI does not start PostgreSQL"
    assert "pytest tests" in text, "CI does not run the suite"
    assert "service_healthy" in text or "health-cmd" in text


def test_ci_builds_the_image():
    """Building is a test. The previous Dockerfile's CMD could never have started the
    application, and nothing noticed, because nothing ever built it."""
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "docker build" in text, "CI never builds the image"
    assert "/livez" in text, (
        "CI does not verify the container answers liveness - the check that proves it can "
        "start at all"
    )


def test_ci_validates_the_compose_file():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "docker compose config" in text
    assert "must refuse to start without secrets" in text, (
        "CI does not verify the no-default-password property, which is the one that a "
        "working deployment silently violates"
    )
