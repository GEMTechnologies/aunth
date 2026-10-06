"""Phase 1 security regression tests.

These tests exist because the defects they cover were silent: the application
compiled, imported individual modules cleanly, and failed only at runtime.
Each test is written to fail against the pre-remediation code.

Run from ``Auth/backend`` with::

    .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import ast
import collections
import inspect
import pathlib
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import security
from config import settings
from security import (
    SecurityManager,
    TenantScopeError,
    create_access_token,
    decode_access_token,
    generate_csrf_token,
    hash_password,
    resolve_tenant,
    verify_csrf_token,
    verify_password,
)

BACKEND = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# ADR-0002: one definition per symbol
# ---------------------------------------------------------------------------

def _top_level_names(path: pathlib.Path) -> collections.defaultdict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    seen = collections.defaultdict(list)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            seen[node.name].append(node.lineno)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    seen[target.id].append(node.lineno)
    return seen


@pytest.mark.parametrize(
    "filename", ["security.py", "config.py"]
)
def test_no_duplicate_top_level_definitions(filename):
    """A later duplicate silently wins in Python; assert none remain."""
    seen = _top_level_names(BACKEND / filename)
    duplicates = {name: lines for name, lines in seen.items() if len(lines) > 1}
    assert not duplicates, (
        f"{filename} re-defines {duplicates}; the later definition silently "
        f"replaces the earlier one"
    )


def test_argon2id_uses_configured_cost():
    """The configured Argon2id parameters must actually reach the hash.

    Asserted against the encoded PHC string, which is what an attacker would
    attack -- a handler attribute check alone would not prove the parameters
    were applied at hash time.
    """
    encoded = hash_password("configured-cost-probe")

    assert encoded.startswith("$argon2id$"), "must be Argon2id"
    params = dict(
        part.split("=", 1)
        for part in encoded.split("$")[3].split(",")
    )
    assert int(params["m"]) == settings.argon2_memory
    assert int(params["t"]) == settings.argon2_time
    assert int(params["p"]) == settings.argon2_parallelism


def test_passwords_use_a_random_salt():
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b, "identical passwords must not produce identical hashes"
    assert a.split("$")[4] != b.split("$")[4]


# ---------------------------------------------------------------------------
# Password handling
# ---------------------------------------------------------------------------

def test_password_hash_round_trip():
    hashed = hash_password("correct-horse-battery")
    assert hashed != "correct-horse-battery"
    assert verify_password("correct-horse-battery", hashed)
    assert not verify_password("wrong-password", hashed)


def test_short_password_rejected():
    with pytest.raises(ValueError):
        hash_password("short")


def test_security_manager_still_exposes_hashing():
    """These methods were erased by the duplicate class definition."""
    assert callable(SecurityManager.hash_password)
    assert callable(SecurityManager.verify_password)
    assert callable(SecurityManager.check_password_strength)


# ---------------------------------------------------------------------------
# CSRF: previously called secrets.compare_digest, which does not exist
# ---------------------------------------------------------------------------

def test_csrf_verify_uses_hmac_compare_digest():
    session = "session-token-abc"
    token = generate_csrf_token(session)
    assert verify_csrf_token(token, session) is True
    assert verify_csrf_token("wrong", session) is False
    assert verify_csrf_token(token, "other-session") is False


def test_security_manager_csrf_round_trip():
    token = SecurityManager.generate_csrf_token()
    assert SecurityManager.verify_csrf_token(token, token) is True
    assert SecurityManager.verify_csrf_token(token, "nope") is False


# ---------------------------------------------------------------------------
# Tenant scoping - the core regression
# ---------------------------------------------------------------------------

def test_access_token_carries_tenant_claims():
    """The pre-fix token silently dropped every one of these claims."""
    token = create_access_token(
        subject="user-1",
        org_id="org-abc",
        roles=["owner"],
        permissions=["application.write", "document.read"],
        session_id="sess-1",
    )
    payload = decode_access_token(token)

    assert payload["sub"] == "user-1"
    assert payload["org_id"] == "org-abc"
    assert payload["roles"] == ["owner"]
    assert payload["sid"] == "sess-1"
    assert payload["jti"], "jti must be present for revocation"
    assert payload["ver"] == 1
    assert payload["type"] == "access"
    assert payload["org_ids"] == ["org-abc"]


def test_access_token_requires_subject():
    with pytest.raises(ValueError):
        create_access_token(subject="")


def test_extra_claims_cannot_override_reserved_claims():
    with pytest.raises(ValueError):
        create_access_token(
            subject="user-1",
            org_id="org-abc",
            extra_claims={"org_id": "org-attacker"},
        )


def test_resolve_tenant_returns_org():
    payload = decode_access_token(
        create_access_token(subject="user-1", org_id="org-abc")
    )
    assert resolve_tenant(payload) == "org-abc"


def test_unknown_tenant_is_denied_not_defaulted():
    """'Tenant unknown' must deny. It must never fall back to a default org."""
    payload = decode_access_token(create_access_token(subject="user-1"))

    assert payload["org_id"] is None
    with pytest.raises(TenantScopeError):
        resolve_tenant(payload)
    assert resolve_tenant(payload, required=False) is None


def test_token_of_one_tenant_cannot_resolve_as_another():
    """A token for org-alice must never resolve to org-bob."""
    alice = decode_access_token(
        create_access_token(subject="alice", org_id="org-alice")
    )
    bob = decode_access_token(
        create_access_token(subject="bob", org_id="org-bob")
    )
    assert resolve_tenant(alice) == "org-alice"
    assert resolve_tenant(bob) == "org-bob"
    # A tenant-scoped lookup must not fall through to another org's token.
    with pytest.raises(TenantScopeError):
        resolve_tenant({**alice, "org_id": None})


def test_tampered_token_rejected():
    token = create_access_token(subject="user-1", org_id="org-abc")
    tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    with pytest.raises(ValueError):
        decode_access_token(tampered)


def test_expired_token_rejected():
    from datetime import timedelta
    token = create_access_token(
        subject="user-1", org_id="org-abc", expires_delta=timedelta(seconds=-60)
    )
    with pytest.raises(ValueError):
        decode_access_token(token)


def test_malformed_token_rejected():
    for bad in ["", "not-a-jwt", "a.b.c"]:
        with pytest.raises(ValueError):
            decode_access_token(bad)


# ---------------------------------------------------------------------------
# Refresh tokens
# ---------------------------------------------------------------------------

def test_refresh_tokens_are_opaque_and_unique():
    a = security.create_refresh_token(data={"sub": "user-1"})
    b = security.create_refresh_token(data={"sub": "user-1"})
    assert a != b
    assert "." not in a, "refresh tokens must not be JWTs"
    assert len(a) >= 43


def test_refresh_token_hash_is_stable_and_irreversible():
    token = security.create_refresh_token()
    digest = security.hash_token(token)
    assert digest == security.hash_token(token)
    assert token not in digest
    assert len(digest) == 64


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------

def test_shipped_placeholder_secrets_are_known_and_blocked():
    """The service must refuse to run with repository-published secrets.

    Development keeps the historical placeholders so the stack starts with no
    setup, but any non-development environment must fail closed.
    """
    from config import INSECURE_DEFAULTS, Settings

    assert {
        "your-secret-key-here",
        "your-jwt-secret-here",
        "csrf-secret-key-change-in-production",
    } <= set(INSECURE_DEFAULTS)

    with pytest.raises(ValidationError):
        Settings(app_env="production", jwt_secret="your-jwt-secret-here")

    with pytest.raises(ValidationError):
        Settings(app_env="production", jwt_secret="a-real-looking-value")

    # ...and a genuinely configured secret is accepted.
    ok = Settings(
        app_env="production",
        secret_key="s" * 48,
        jwt_secret="j" * 48,
        csrf_secret="c" * 48,
    )
    assert ok.jwt_secret == "j" * 48


def test_short_jwt_secret_rejected_in_production():
    from config import Settings

    with pytest.raises(ValidationError):
        Settings(
            app_env="production",
            secret_key="s" * 48,
            jwt_secret="too-short",
            csrf_secret="c" * 48,
        )


def test_development_still_boots_with_defaults():
    """Dev must remain frictionless; only production fails closed.

    ``_env_file=None`` keeps the local ``.env`` out of the assertion so the
    test measures the shipped default rather than local configuration.
    """
    from config import Settings

    dev = Settings(app_env="development", _env_file=None)
    assert dev.jwt_secret == "your-jwt-secret-here"
    assert dev.log_level == "INFO"


def test_log_level_is_validated_and_uppercased():
    from config import Settings

    assert Settings(log_level="debug").log_level == "DEBUG"
    with pytest.raises(ValidationError):
        Settings(log_level="chatty")


def test_secret_material_is_not_logged_by_security_module():
    """No logger call in this module may format a secret-bearing argument."""
    source = (BACKEND / "security.py").read_text(encoding="utf-8")
    for needle in ("jwt_secret", "password", "csrf_secret"):
        for line in source.splitlines():
            if "log" in line.lower() and needle in line:
                pytest.fail(f"possible secret in log statement: {line.strip()}")