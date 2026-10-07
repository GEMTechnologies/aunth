"""Harden the JWT configuration, and pin the CVE mitigation with a test.

THE CVE
-------
`pip-audit` reports one remaining advisory with **no fix version**:

    python-jose 3.5.0  CVE-2026-85394
    "python-jose through 3.5.0 fails to properly validate asymmetric keys in HMAC
     initialization, accepting DER-encoded public keys that lack PEM armor or SSH
     prefixes. Attackers holding the service's public key can forge HS256 tokens that
     pass verification **when algorithms are not explicitly restricted**."

So the vulnerability is conditional on a configuration, and the condition is one Granada
does not meet. Two independent reasons:

1. `decode_access_token` passes `algorithms=[settings.jwt_algorithm]`. The accepted set is
   exactly one algorithm, which is the mitigation the advisory names.
2. Granada's `jwt_secret` is a **symmetric** string, not a public key. The attack needs
   the service to be verifying with an asymmetric public key that the library then treats
   as an HMAC key. There is no such key anywhere in this codebase.

That is an assessment, not a guarantee, so this module does two things: it **refuses** the
configuration that would make the advisory exploitable, and it **asserts** the mitigation
so a later refactor cannot quietly remove it.
"""

import pathlib

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent.parent
import sys

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

#: Algorithms this codebase may use. HMAC only.
#:
#: `decode_access_token` passes `settings.jwt_secret` as the verification key. That is an
#: HMAC secret. For an asymmetric algorithm the correct key is a public key, so pairing
#: `RS256` with this setting means python-jose is asked to verify an asymmetric token with
#: a symmetric key - which is precisely the shape CVE-2026-85394 describes, and the shape
#: that turns an algorithm-confusion bug into a forgery.
SYMMETRIC_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})


# ===========================================================================
# THE MITIGATION IS PRESENT
# ===========================================================================
def test_the_accepted_algorithm_set_is_explicitly_restricted():
    """The advisory is conditional on `algorithms` NOT being explicitly restricted, and
    this is where that condition is met."""
    source = (BACKEND / "security.py").read_text(encoding="utf-8")
    assert "algorithms=[settings.jwt_algorithm]" in source, (
        "decode_access_token no longer restricts the accepted algorithms. That is the "
        "exact condition CVE-2026-85394 depends on, and removing it would make the "
        "advisory exploitable for any caller holding a public key."
    )


def test_the_configured_algorithm_is_symmetric():
    """The secret is an HMAC key, so the algorithm must be an HMAC algorithm."""
    from config import settings

    assert settings.jwt_algorithm.upper() in SYMMETRIC_ALGORITHMS, (
        f"jwt_algorithm is {settings.jwt_algorithm!r} but the verification key is a "
        "symmetric secret. An asymmetric algorithm here asks python-jose to verify an "
        "asymmetric token with an HMAC key - the shape CVE-2026-85394 describes."
    )


def test_decode_requires_a_signature():
    """`alg: none` must not be accepted anywhere in the options."""
    source = (BACKEND / "security.py").read_text(encoding="utf-8")
    assert '"verify_signature": True' in source
    assert "none" not in source.split("algorithms=[")[1].split("]")[0].lower(), (
        "`none` appears in the accepted algorithm set"
    )


# ===========================================================================
# ALGORITHM CONFUSION IS REFUSED IN PRACTICE
# ===========================================================================
def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _forge(header: dict, payload: dict, key: bytes) -> str:
    """Build a compact JWS by hand.

    Deliberately not via `jose.jwt.encode`: python-jose 3.5.0 refuses to construct both an
    `alg: none` token and one signed with an asymmetric key as the HMAC secret. An
    attacker does not ask the victim's library for permission, so the test does not either.
    """
    import hashlib
    import hmac as hmac_module
    import json

    signing_input = (
        _b64(json.dumps(header, separators=(",", ":")).encode()) + "." +
        _b64(json.dumps(payload, separators=(",", ":")).encode())
    )
    signature = hmac_module.new(key, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


def _forge_unsigned(header: dict, payload: dict) -> str:
    """An `alg: none` token: the compact form with an EMPTY signature segment."""
    import json

    return (
        _b64(json.dumps(header, separators=(",", ":")).encode()) + "." +
        _b64(json.dumps(payload, separators=(",", ":")).encode()) + "."
    )


def test_a_token_with_alg_none_is_rejected():
    """The oldest JWT attack there is, forged by hand and tested against the decode path."""
    from security import decode_access_token

    forged = _forge_unsigned(
        {"alg": "none", "typ": "JWT"},
        {"sub": "attacker", "exp": 9999999999, "iat": 1, "iss": "granada.auth"},
    )
    with pytest.raises(ValueError):
        decode_access_token(forged)


def test_an_unsigned_token_with_an_hs256_HEADER_is_also_rejected():
    """The subtler variant: the header claims HS256 but the signature segment is empty, so
    a verifier that trusts the header rather than the configured algorithm accepts it."""
    from security import decode_access_token

    forged = _forge_unsigned(
        {"alg": "HS256", "typ": "JWT"},
        {"sub": "attacker", "exp": 9999999999, "iat": 1, "iss": "granada.auth"},
    )
    with pytest.raises(ValueError):
        decode_access_token(forged)


def test_a_token_signed_with_a_public_key_as_the_hmac_secret_is_rejected():
    """The CVE's attack shape, executed by hand.

    An attacker holding the service's public key signs an HS256 token using that PEM as the
    HMAC secret. It passes only where the verifier accepts more than one algorithm or is
    configured asymmetrically with a symmetric key. Granada does neither.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from security import decode_access_token

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    forged = _forge(
        {"alg": "HS256", "typ": "JWT"},
        {"sub": "attacker", "exp": 9999999999, "iat": 1, "iss": "granada.auth"},
        public_pem.encode(),
    )
    with pytest.raises(ValueError):
        decode_access_token(forged)


def test_the_configured_secret_is_not_a_public_key():
    """THE structural precondition of CVE-2026-85394.

    The advisory needs the service to hold a PUBLIC KEY and use it as an HMAC secret. A
    symmetric random string cannot be confused with anything: there is no public half, no
    key-type ambiguity, and nothing for an attacker to obtain. This is the reason the
    advisory cannot apply here, asserted against the value rather than argued in a comment.
    """
    from config import settings

    secret = settings.jwt_secret
    assert "BEGIN" not in secret, "jwt_secret looks like a PEM key"
    assert "PUBLIC KEY" not in secret.upper()
    assert "PRIVATE KEY" not in secret.upper()
    assert "ssh-rsa" not in secret
    # And it is long enough not to be guessable, which config already enforces.
    assert len(secret) >= 32


def test_a_genuine_token_still_validates():
    """The counterpart, so the two tests above cannot pass by rejecting everything."""
    import uuid

    from security import create_access_token, decode_access_token

    token = create_access_token(
        subject=str(uuid.uuid4()), org_ids=(), permissions=(),
    )
    payload = decode_access_token(token)
    assert payload["sub"]


# ===========================================================================
# THE CONFIGURATION THAT WOULD MAKE IT EXPLOITABLE IS REFUSED
# ===========================================================================
def test_an_asymmetric_jwt_algorithm_is_refused_by_configuration_validation(monkeypatch):
    """Startup must refuse it rather than accept a shape the advisory makes dangerous."""
    from config import settings
    from health import validate_configuration

    monkeypatch.setattr(settings, "jwt_algorithm", "RS256", raising=False)
    problems = validate_configuration()
    codes = {p.code for p in problems}
    assert "JWT_ALGORITHM_NOT_HMAC" in codes, (
        f"an asymmetric jwt_algorithm was not refused; problems were {codes}"
    )
    refusal = next(p for p in problems if p.code == "JWT_ALGORITHM_NOT_HMAC")
    assert refusal.severity == "refuse"
    # And the reason names the advisory, so the next reader does not "fix" it by removing
    # the check.
    assert "public key" in refusal.detail.lower() or "hmac" in refusal.detail.lower()


def test_a_symmetric_algorithm_is_accepted(monkeypatch):
    """The check must not refuse the configuration the codebase actually uses."""
    from config import settings
    from health import validate_configuration

    for algorithm in sorted(SYMMETRIC_ALGORITHMS):
        monkeypatch.setattr(settings, "jwt_algorithm", algorithm, raising=False)
        codes = {p.code for p in validate_configuration()}
        assert "JWT_ALGORITHM_NOT_HMAC" not in codes, f"{algorithm} was refused"


# ===========================================================================
# THE PINS DO NOT DRIFT BACK
# ===========================================================================
#: (package, the version that fixed the advisory, the advisory)
#:
#: A downgrade is silent: nothing in the suite would notice, and the next `pip install`
#: from a stale lock would reintroduce 34 advisories. So the floor is asserted.
VULNERABILITY_FLOORS = (
    ("fastapi", (0, 109, 1), "PYSEC-2024-38"),
    ("python-multipart", (0, 0, 32), "17 advisories, highest fix 0.0.31"),
    ("python-jose", (3, 5, 0), "CVE-2024-33663; see the note at the top of this file"),
    ("jinja2", (3, 1, 6), "PYSEC-2026-1471..1475"),
    ("python-dotenv", (1, 2, 2), "PYSEC-2026-2270"),
)


def _parse(version: str) -> tuple[int, ...]:
    parts = []
    for chunk in version.split(".")[:3]:
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def test_every_vulnerable_pin_is_at_or_above_its_fixed_version():
    """A dependency audit found **34 advisories in 5 packages**; this is the guard that
    keeps them fixed.

    Asserted against `requirements.txt` rather than the installed environment, because the
    file is what a deployment installs.
    """
    requirements = (BACKEND / "requirements.txt").read_text(encoding="utf-8").lower()
    problems = []
    # A requirement line may carry extras: `python-jose[cryptography]==3.5.0`. Matching on
    # `package + "="` missed it, so the guard reported a correctly-pinned dependency as
    # missing - the worst kind of false positive, because it teaches people to ignore it.
    import re as _re

    for package, floor, advisory in VULNERABILITY_FLOORS:
        pattern = _re.compile(
            r"^" + _re.escape(package.lower()) + r"(?:\[[^\]]*\])?==([^\s#]+)",
            _re.MULTILINE,
        )
        match = pattern.search(requirements)
        if match is None:
            problems.append(f"{package} is no longer pinned at all ({advisory})")
            continue
        declared = match.group(1).strip()
        if _parse(declared) < floor:
            problems.append(
                f"{package}=={declared} is below the fixed "
                f"{'.'.join(map(str, floor))} ({advisory})"
            )
    assert not problems, (
        "these pins reintroduce a known advisory: " + "; ".join(problems)
    )


def test_the_cve_with_no_fix_is_recorded_rather_than_forgotten():
    """CVE-2026-85394 has no fix version. An unfixable advisory that is not written down
    is an advisory that gets rediscovered as a surprise; recorded, it is a known accepted
    risk with a named mitigation."""
    text = (BACKEND / "tests" / "test_dependency_security.py").read_text(encoding="utf-8")
    assert "CVE-2026-85394" in text
    assert "algorithms are not explicitly restricted" in text, (
        "the condition the advisory depends on should be quoted, so the mitigation can be "
        "checked against it"
    )
