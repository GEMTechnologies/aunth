"""SSRF and internal-network protection.

§8 requires "protection against SSRF and access to private/internal network resources". Inspected
before assuming, and there was NONE: `ActionScope.allowed_hosts` was exact-match, which is good, and
nothing checked what a host RESOLVED to.

Exact-match alone is not a defence. The allow-list is CONFIGURATION, and the controlled test portal
genuinely needs `127.0.0.1` to be testable. A rule that says "127.0.0.1 may be allowed per task" and a
rule that says "127.0.0.1 is fine by default" look identical in a config file and are completely
different security postures.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.target_guard import (  # noqa: E402
    METADATA_HOSTS,
    TargetRefused,
    assert_target,
    check_target,
    describe,
    screen_hosts,
)


def resolver_for(*addresses: str):
    """A fake getaddrinfo. Injectable so the classification is testable without real DNS - a test that
    depends on network state is a test that fails on a plane."""
    def _resolve(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)) for addr in addresses]
    return _resolve


# ===========================================================================
# CLOUD METADATA - THE CANONICAL SSRF OBJECTIVE
# ===========================================================================
def test_the_metadata_address_is_refused():
    """Not "the internal network" in general: a CREDENTIAL ENDPOINT, reachable from any instance, and
    the objective in a large share of real SSRF incidents."""
    d = check_target("http://169.254.169.254/latest/meta-data/iam/security-credentials/")
    assert d.permitted is False
    assert "metadata" in d.because


def test_the_metadata_hostname_is_refused():
    assert check_target("http://metadata.google.internal/computeMetadata/v1/").permitted is False


def test_a_name_that_RESOLVES_to_metadata_is_refused():
    """The reason this is not a string check: the URL names `harmless.example`, and the address is the
    credential endpoint."""
    d = check_target("http://harmless.example/x", resolver=resolver_for("169.254.169.254"))
    assert d.permitted is False
    assert "metadata" in d.because


# ===========================================================================
# PRIVATE AND INTERNAL RANGES
# ===========================================================================
@pytest.mark.parametrize("address", ["10.0.0.5", "172.16.4.4", "192.168.1.10", "169.254.1.1"])
def test_private_and_link_local_ranges_are_refused(address):
    assert check_target(f"http://{address}/", resolver=resolver_for(address)).permitted is False


def test_a_NAME_pointing_at_a_private_address_is_refused():
    """`internal.example.com` -> 10.0.0.5 passes any name-based rule. This is the attack."""
    d = check_target("https://internal.example.com/apply", resolver=resolver_for("10.0.0.5"))
    assert d.permitted is False
    assert "private range" in d.because


@pytest.mark.parametrize("host", ["db.internal", "api.local", "svc.cluster.local", "thing.svc"])
def test_conventionally_internal_names_are_refused(host):
    assert check_target(f"https://{host}/").permitted is False


def test_a_public_address_is_permitted():
    d = check_target("https://funder.example/apply", resolver=resolver_for("93.184.216.34"))
    assert d.permitted is True
    assert "publicly routable" in d.because


# ===========================================================================
# THE LOOPBACK EXCEPTION - EXPLICIT, PER TASK
# ===========================================================================
def test_loopback_is_refused_BY_DEFAULT():
    """The whole point of the exception being explicit. A production task must not inherit the test
    portal's permission by being constructed the same way."""
    assert check_target("http://127.0.0.1:8099/apply").permitted is False
    assert check_target("http://[::1]:8099/apply").permitted is False


def test_loopback_is_permitted_only_when_the_task_asks():
    d = check_target("http://127.0.0.1:8099/apply", allow_loopback=True)
    assert d.permitted is True


def test_the_loopback_exception_does_NOT_permit_private_ranges():
    """It is an exception for the TEST PORTAL, not a general "internal is fine" switch. Opting in must
    not also open 10/8 and the metadata endpoint."""
    assert check_target("http://10.0.0.5/", allow_loopback=True).permitted is False
    assert check_target("http://169.254.169.254/", allow_loopback=True).permitted is False
    assert check_target("http://192.168.1.1/", allow_loopback=True).permitted is False


# ===========================================================================
# SCHEMES
# ===========================================================================
@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://funder.example/x",
    "data:text/html,<script>x</script>",
    "gopher://funder.example/",
    "javascript:alert(1)",
])
def test_non_web_schemes_are_refused(url):
    """A navigation to file:///etc/passwd is not a web request and has no business in a portal
    workflow."""
    d = check_target(url, resolver=resolver_for("93.184.216.34"))
    assert d.permitted is False
    assert "scheme" in d.because


def test_a_url_with_no_host_is_refused():
    assert check_target("https:///path").permitted is False


def test_an_empty_url_is_refused():
    assert check_target("").permitted is False


# ===========================================================================
# ALL ANSWERS ARE CHECKED, NOT JUST THE FIRST
# ===========================================================================
def test_a_host_resolving_to_public_AND_private_is_refused():
    """A check that only looked at the first answer would permit this, depending on DNS ordering."""
    d = check_target("https://mixed.example/", resolver=resolver_for("93.184.216.34", "10.0.0.5"))
    assert d.permitted is False
    assert "10.0.0.5" in d.because


def test_all_addresses_are_reported_so_a_decision_can_be_reviewed():
    d = check_target("https://mixed.example/", resolver=resolver_for("93.184.216.34", "10.0.0.5"))
    assert set(d.addresses) == {"93.184.216.34", "10.0.0.5"}


# ===========================================================================
# FAIL CLOSED
# ===========================================================================
def test_an_unresolvable_host_is_REFUSED_not_permitted():
    """The default must be refusal. A host that cannot be classified must not be assumed safe."""
    def broken(host, *a, **k):
        raise socket.gaierror("name or service not known")

    d = check_target("https://nowhere.example/", resolver=broken)
    assert d.permitted is False
    assert "could not be resolved" in d.because


def test_a_host_resolving_to_nothing_is_refused():
    assert check_target("https://empty.example/", resolver=resolver_for()).permitted is False


def test_assert_refuses_by_raising():
    with pytest.raises(TargetRefused):
        assert_target("http://169.254.169.254/")


def test_assert_returns_a_decision_when_permitted():
    assert assert_target("https://ok.example/", resolver=resolver_for("93.184.216.34")).permitted is True


# ===========================================================================
# SCREENING AN ALLOW-LIST
# ===========================================================================
def test_screening_a_host_list_returns_the_refused_entries_with_reasons():
    refused = screen_hosts(["funder.example", "10.0.0.5", "169.254.169.254"],
                           resolver=lambda host, *a, **k: resolver_for(
                               "93.184.216.34" if host == "funder.example" else host)(host))
    hosts = [h for h, _ in refused]
    assert "funder.example" not in hosts
    assert "10.0.0.5" in hosts
    assert "169.254.169.254" in hosts
    assert all(reason for _, reason in refused), "a refusal without a reason cannot be acted on"


def test_screening_DOES_NOT_resolve_because_it_runs_at_build_time():
    """A DELIBERATE limitation, asserted so it is a decision rather than an oversight.

    `screen_hosts` runs inside `validate_task`. Making that do DNS turned a pure validation function
    into one that could fail because a resolver was down or a legitimate funder domain was briefly
    unresolvable - and it refused every fixture host that is deliberately fake, which is most of them.
    Twenty-seven existing tests failed when it resolved, which is how the flaw was found.

    So build time checks what the configuration LITERALLY says. `internal.example.com` is not caught
    here, and pretending otherwise would be worse than saying so.
    """
    refused = screen_hosts(["internal.example.com"], resolver=resolver_for("10.0.0.5"))
    assert refused == [], (
        "screening resolved a name; build-time screening must not depend on the network"
    )


def test_but_the_RUN_TIME_check_does_catch_the_same_name():
    """The other half of the split. The browser is about to make a connection, so an answer exists and
    is checked then - and `internal.example.com` -> 10.0.0.5 is refused."""
    d = check_target("https://internal.example.com/apply", resolver=resolver_for("10.0.0.5"))
    assert d.permitted is False
    assert "private range" in d.because


def test_screening_still_catches_a_LITERAL_private_address():
    """So the build-time screen is not decoration: the configuration that names the address outright
    is refused without any DNS at all."""
    refused = screen_hosts(["10.0.0.5", "169.254.169.254", "192.168.1.1"])
    hosts = sorted(h for h, _ in refused)
    assert hosts == ["10.0.0.5", "169.254.169.254", "192.168.1.1"]


def test_screening_catches_internal_naming_conventions_without_dns():
    refused = screen_hosts(["db.internal", "api.local", "x.svc"])
    assert sorted(h for h, _ in refused) == ["api.local", "db.internal", "x.svc"]


def test_screening_does_not_resolve_so_a_fake_fixture_host_passes():
    """This is what made 27 tests fail when the screen resolved: `portal.example` is deliberately fake
    and unresolvable, and a build-time screen that refuses it would refuse every fixture in the suite -
    and, in production, any domain whose DNS was momentarily unavailable."""
    assert screen_hosts(["portal.example", "funder.example"]) == []


def test_the_build_time_decision_says_it_did_not_resolve():
    """An unchecked pass must not look like a checked one."""
    d = check_target("https://funder.example/apply", resolve=False)
    assert d.permitted is True
    assert "not a literal address" in d.because
    assert "checked when the browser actually navigates" in d.because


# ===========================================================================
# THE BOUNDARY IS STATED, INCLUDING ITS LIMIT
# ===========================================================================
def test_describe_records_the_rule_and_the_UNSOLVED_limit():
    d = describe()
    assert d["allowed_schemes"] == ["http", "https"]
    assert sorted(d["refused"]["cloud_metadata"]) == sorted(METADATA_HOSTS)
    assert "loopback" in d["refused"]
    assert "explicit" in d["loopback_exception"].lower()
    # DNS rebinding cannot be solved from this layer and must say so rather than be implied away.
    assert "REBINDING IS NOT SOLVED" in d["known_limit"]
    assert "separate events" in d["known_limit"]


def test_describe_says_when_the_check_runs():
    """Build time matters: the reason can still reach whoever wrote the configuration."""
    assert "BUILT" in describe()["when"]
