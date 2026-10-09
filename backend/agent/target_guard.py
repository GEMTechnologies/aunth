"""Where the browser is allowed to point, and where it never is.

THE GAP THIS CLOSES
-------------------
§8 requires "protection against SSRF and access to private/internal network resources". Inspected
before assuming: `ActionScope.allowed_hosts` is exact-match, which is good, and there was **no check on
what a host resolves to at all.**

So a task whose allow-list named any of these would be dispatched, and the executor is a browser
running with a live network position inside Granada's own infrastructure:

    169.254.169.254     cloud instance metadata - the credential endpoint, on every major provider
    127.0.0.1 / ::1     the loopback - where Granada's OWN API listens
    10.x · 172.16-31.x · 192.168.x    the private ranges, where the database and services live
    *.internal · *.local              name-based routes to the same places

Exact-match alone is not a defence here, because the allow-list is CONFIGURATION and the local portal
genuinely needs `127.0.0.1` to be tested. A rule that says "127.0.0.1 may be allowed" and a rule that
says "127.0.0.1 is fine by default" look identical in a config file and are completely different
security postures.

WHAT IS CHECKED, AND WHY IT IS NOT JUST A STRING MATCH

A hostname is RESOLVED and every address it resolves to is classified. `internal.example.com` pointing
at `10.0.0.5` would pass any name-based check and is exactly the attack this exists for.

DNS rebinding - a name that resolves to a public address when checked and a private one when the
browser connects - is **not** solved here. It cannot be, from this layer: the check and the connection
are separate events. The honest mitigation is to pin the resolution, which belongs to the browser
launch and is recorded below as an outstanding limit rather than implied away.

THE LOOPBACK EXCEPTION IS EXPLICIT AND NAMED

The controlled test portal runs on loopback, and a guard that made it untestable would be deleted
within a week. So loopback is refused **unless the caller names it** - an explicit opt-in per task, not
a default, so a production task cannot inherit the test portal's permission.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit

#: Schemes the browser may be sent to. Anything else - file:, ftp:, data: - is refused outright,
#: because a navigation to `file:///etc/passwd` is not a web request and has no business in a portal
#: workflow.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: The cloud metadata address. Singled out because it is not "the internal network" in general: it is
#: a credential endpoint, reachable from any instance, and used in a large share of real SSRF
#: incidents.
METADATA_HOSTS = frozenset({"169.254.169.254", "metadata.google.internal", "fd00:ec2::254"})

#: Suffixes that route to internal infrastructure by convention.
INTERNAL_SUFFIXES = (".internal", ".local", ".localhost", ".cluster.local", ".svc")


class TargetRefused(RuntimeError):
    """The browser was told to go somewhere it must not. Refused, never silently skipped."""


@dataclass(frozen=True)
class TargetDecision:
    permitted: bool
    because: str
    host: str = ""
    scheme: str = ""
    #: Every address the host resolved to, so a decision can be reviewed rather than trusted.
    addresses: tuple[str, ...] = ()


def _classify(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> Optional[str]:
    """Why an address is not publicly routable, or None if it is.

    Checked in order of severity, so the reported reason is the most specific true one - an operator
    reading "cloud metadata address" acts differently from one reading "private range".
    """
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        # 169.254.0.0/16 - which contains the metadata endpoint.
        return "link-local"
    if ip.is_private:
        return "private range"
    if ip.is_reserved:
        return "reserved range"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified address"
    return None


def check_target(
    url: str,
    *,
    allow_loopback: bool = False,
    resolver: Optional[Any] = None,
    resolve: bool = True,
) -> TargetDecision:
    """Whether the browser may navigate to this URL.

    `resolver` is injectable so the classification is testable without real DNS - the alternative is
    a test that depends on network state, which is a test that fails on a plane.

    `resolve=False` SKIPS name resolution and classifies only what the URL literally says: the scheme,
    literal IP addresses, and conventionally-internal names. That is the BUILD-TIME mode, and the
    split is deliberate:

      * at BUILD time, a validation function must not depend on the network. Making it do DNS turned
        `validate_task` into something that could fail because a resolver was down or a legitimate
        funder domain was temporarily unresolvable - and it refused every fixture host that is
        deliberately fake, which is most of them.
      * at RUN time, the browser is about to make a connection, so resolution is checked then, when
        an answer actually exists.

    A LITERAL private address is refused in BOTH modes, so the build-time screen is not decoration:
    it still catches the configuration that names `10.0.0.5` or `169.254.169.254` outright.
    """
    parts = urlsplit(url or "")
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()

    if scheme not in ALLOWED_SCHEMES:
        return TargetDecision(
            permitted=False,
            because=(
                f"scheme {scheme or '(none)'!r} is not permitted; the browser may only be sent to "
                f"{sorted(ALLOWED_SCHEMES)} - a navigation to file: or data: is not a web request"
            ),
            host=host, scheme=scheme,
        )

    if not host:
        return TargetDecision(permitted=False, because="the URL names no host", scheme=scheme)

    # 1. Names that route internally by convention, checked before resolution because they are the
    #    intent even when the DNS answer happens to be public today.
    if host in METADATA_HOSTS:
        return TargetDecision(
            permitted=False,
            because=(
                f"{host} is a cloud instance metadata endpoint - not a website. It serves instance "
                "credentials, and reaching it is the canonical SSRF objective"
            ),
            host=host, scheme=scheme,
        )

    for suffix in INTERNAL_SUFFIXES:
        if host.endswith(suffix) or host == suffix.lstrip("."):
            return TargetDecision(
                permitted=False,
                because=f"{host} ends in {suffix!r}, which routes to internal infrastructure",
                host=host, scheme=scheme,
            )

    # 2. LITERAL addresses and names that RESOLVE to addresses. The second is why this is not a string
    #    check: `internal.example.com` -> 10.0.0.5 passes any name-based rule.
    addresses: tuple[str, ...] = ()
    if _looks_like_ip(host):
        addresses = (host,)
    elif not resolve:
        # BUILD TIME. A literal address has already been classified above; a NAME has not been, and
        # without DNS there is nothing more this layer can honestly claim to know. Say so, rather than
        # reporting a pass that was never checked.
        return TargetDecision(
            permitted=True,
            because=(
                f"{host} is not a literal address and resolution was not requested; the build-time "
                "screen checks the scheme, literal addresses and internal naming conventions, and the "
                "RESOLVED address is checked when the browser actually navigates"
            ),
            host=host, scheme=scheme,
        )
    else:
        try:
            addresses = _resolve(host, resolver)
        except OSError as exc:
            return TargetDecision(
                permitted=False,
                because=f"{host} could not be resolved ({exc.__class__.__name__}); refusing rather "
                        "than sending the browser somewhere unclassified",
                host=host, scheme=scheme,
            )

    if not addresses:
        return TargetDecision(
            permitted=False,
            because=f"{host} resolved to no addresses; refusing rather than proceeding unclassified",
            host=host, scheme=scheme,
        )

    reasons: list[str] = []
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            reasons.append(f"{address} (unparseable)")
            continue
        if str(parsed) in METADATA_HOSTS:
            return TargetDecision(
                permitted=False,
                because=f"{host} resolves to {parsed}, a cloud instance metadata address - "
                        "the canonical SSRF objective",
                host=host, scheme=scheme, addresses=addresses,
            )
        reason = _classify(parsed)
        if reason is None:
            continue
        # THE ONLY EXCEPTION, and it must be requested. A production task cannot inherit it.
        if reason == "loopback" and allow_loopback:
            continue
        reasons.append(f"{parsed} ({reason})")

    if reasons:
        return TargetDecision(
            permitted=False,
            because=(
                f"{host} resolves to {'; '.join(reasons)}. The browser runs with a network position "
                "inside Granada's own infrastructure"
                + ("" if allow_loopback else "; loopback is refused unless the task explicitly opts in")
            ),
            host=host, scheme=scheme, addresses=addresses,
        )

    return TargetDecision(
        permitted=True,
        because=f"{host} resolves only to publicly routable addresses",
        host=host, scheme=scheme, addresses=addresses,
    )


def assert_target(url: str, *, allow_loopback: bool = False, resolver: Optional[Any] = None) -> TargetDecision:
    """`check_target`, refusing rather than reporting."""
    decision = check_target(url, allow_loopback=allow_loopback, resolver=resolver)
    if not decision.permitted:
        raise TargetRefused(decision.because)
    return decision


def screen_hosts(
    hosts: Iterable[str],
    *,
    allow_loopback: bool = False,
    resolver: Optional[Any] = None,
) -> list[tuple[str, str]]:
    """Screen an allow-list and return the refused entries WITH their reasons.

    Run when a task is BUILT, not only when it runs: a task carrying an internal host that can never
    be permitted should be refused at construction, when the reason can still reach whoever wrote the
    configuration, rather than at 3am inside a browser run.
    """
    refused: list[tuple[str, str]] = []
    for host in hosts:
        # A bare host in a list has no scheme; give it one so the shared classifier applies unchanged.
        decision = check_target(f"https://{host}", allow_loopback=allow_loopback, resolver=resolver, resolve=False)
        if not decision.permitted:
            refused.append((host, decision.because))
    return refused


def _looks_like_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _resolve(host: str, resolver: Optional[Any]) -> tuple[str, ...]:
    """Every address a name resolves to.

    ALL of them, not the first: a host that resolves to one public and one private address would pass
    a check that only looked at the first answer.
    """
    lookup = resolver if resolver is not None else socket.getaddrinfo
    if resolver is None:
        infos = lookup(host, None, proto=socket.IPPROTO_TCP)
    else:
        infos = lookup(host)
    return tuple(sorted({info[4][0] for info in infos}))


def describe() -> dict[str, Any]:
    """The rule, stated where a reviewer will find it."""
    return {
        "allowed_schemes": sorted(ALLOWED_SCHEMES),
        "refused": {
            "cloud_metadata": sorted(METADATA_HOSTS),
            "loopback": "127.0.0.0/8, ::1 - refused unless the task names it explicitly",
            "private": "10/8, 172.16/12, 192.168/16",
            "link_local": "169.254/16",
            "reserved_multicast_unspecified": "refused",
            "names": list(INTERNAL_SUFFIXES),
        },
        "resolution": (
            "names are RESOLVED and every address classified; a name-based check would pass "
            "internal.example.com pointing at 10.0.0.5, which is the attack this exists for"
        ),
        "loopback_exception": (
            "explicit per task via allow_loopback, because the controlled test portal runs on "
            "loopback and a guard that made it untestable would be deleted within a week. A "
            "production task cannot inherit the test portal's permission"
        ),
        "known_limit": (
            "DNS REBINDING IS NOT SOLVED HERE. A name that resolves publicly when checked and "
            "privately when the browser connects defeats this, because the check and the connection "
            "are separate events. The mitigation is to pin the resolution at browser launch, which is "
            "not implemented - recorded rather than implied away"
        ),
        "when": "when a task is BUILT, so the reason reaches whoever wrote the config, and again at run",
    }
