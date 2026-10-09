"""The browser launch arguments: security properties that were true only by omission.

`PlaywrightProvider.launch` passes `args=["--disable-dev-shm-usage"]`. No `--no-sandbox` (correct, and
the docstring explains the AppArmor profile). No `--remote-debugging-port` (correct, so no debugging
endpoint is exposed).

Both were satisfied BY ABSENCE from a list of strings. One edit adding a debugging flag, or a
`--no-sandbox` to make a container start, would have silently undone a security property that NO TEST
WAS WATCHING.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.launch_guard import (  # noqa: E402
    DEBUGGING_FLAGS,
    SANDBOX_FLAGS,
    LaunchArgsRefused,
    assert_launch_args,
    check_launch_args,
    describe,
)


def _code_only(source: str) -> str:
    """The worker's source with comments and docstrings removed.

    THE FOURTH TIME THIS HAS MATTERED IN THIS DIRECTIVE. The first version of these tests asserted
    `'"--no-sandbox"' not in source`, and the source DOES contain that string - inside the comment that
    says the worker deliberately does not pass it:

        # NOTE: no `args=["--no-sandbox"]`. The AppArmor profile is what makes this work.

    So the test failed against correct code, and a regex for `args=[...]` matched the same comment.
    String matching against prose about code has now produced four false results here; the fix is to
    strip prose before matching, which is what `ast` and this helper do.
    """
    import ast

    tree = ast.parse(source)
    docstrings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.append(doc)

    # Strip whole-line comments and trailing `# ...`, then remove docstring text. Together those are
    # the two places prose about code lives.
    kept: list[str] = []
    for raw in source.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        kept.append(stripped.split("#", 1)[0])

    body = "\n".join(kept)
    for doc in docstrings:
        body = body.replace(doc, "")
    return body


# ===========================================================================
# THE FLAGS THE WORKER ACTUALLY PASSES
# ===========================================================================
def test_the_args_the_worker_actually_uses_are_permitted():
    """Read the REAL launch list from the code and validate it.

    The provider now builds `launch_args` and passes it to both `assert_launch_args` and
    `launch_persistent_context`, so this reads that assignment. A test that read a hard-coded copy
    would keep passing after someone changed the list - which is the whole failure it exists to catch.
    """
    import re

    code = _code_only((BACKEND / "tools" / "browser_worker.py").read_text(encoding="utf-8"))
    match = re.search(r"launch_args\s*=\s*\[([^\]]*)\]", code)
    assert match, "no launch_args=[...] found in the provider's CODE; the guard has nothing to check"
    args = re.findall(r'"([^"]+)"', match.group(1))
    assert args, "the launch args list is empty; that is a behaviour change worth noticing"
    decision = check_launch_args(args)
    assert decision.permitted is True, decision.because


def test_the_validated_list_is_the_list_that_is_passed():
    """The guard is worthless if it validates one list and a different one reaches Chromium. Both must
    be the same variable."""
    import re

    code = _code_only((BACKEND / "tools" / "browser_worker.py").read_text(encoding="utf-8"))
    # assert_launch_args(launch_args) and args=launch_args - the same NAME in both places.
    assert re.search(r"assert_launch_args\(\s*launch_args\s*\)", code), (
        "the launch arguments are validated under a different name than they are passed"
    )
    assert re.search(r"args\s*=\s*launch_args\b", code), (
        "the passed args are not the validated variable"
    )


def test_the_sandbox_is_not_disabled_in_the_worker_code():
    """The directive forbids it explicitly: 'Do not run Chromium with the sandbox disabled merely to
    make installation easier.' Checked against CODE, so the comment explaining the absence of
    `--no-sandbox` cannot be mistaken for its presence."""
    code = _code_only((BACKEND / "tools" / "browser_worker.py").read_text(encoding="utf-8"))
    for flag in SANDBOX_FLAGS:
        assert f'"{flag}"' not in code, f"the worker's code passes {flag}"


def test_no_debugging_port_is_opened_by_the_worker_code():
    code = _code_only((BACKEND / "tools" / "browser_worker.py").read_text(encoding="utf-8"))
    for flag in DEBUGGING_FLAGS:
        assert f'"{flag}' not in code, f"the worker's code opens {flag}"


def test_the_comment_explaining_the_absence_IS_present():
    """If the comment were deleted, the checks above could pass by having nothing to match against -
    and the reasoning for keeping the sandbox on would be lost with it."""
    source = (BACKEND / "tools" / "browser_worker.py").read_text(encoding="utf-8")
    assert "--no-sandbox" in source, "the comment explaining why it is not passed is gone"
    assert "AppArmor" in source


# ===========================================================================
# SANDBOX - REFUSED AT EVERY SETTING
# ===========================================================================
@pytest.mark.parametrize("flag", SANDBOX_FLAGS)
def test_disabling_the_sandbox_is_refused(flag):
    d = check_launch_args([flag])
    assert d.permitted is False
    assert "security boundary" in d.because


@pytest.mark.parametrize("flag", SANDBOX_FLAGS)
def test_the_sandbox_is_refused_EVEN_when_debugging_is_opted_in(flag):
    """`allow_debugging` is for a local diagnostics session. It must not become a route to the sandbox
    being off, because that is the flag people reach for when a container will not start."""
    d = check_launch_args([flag], allow_debugging=True)
    assert d.permitted is False


def test_single_process_is_refused():
    """It collapses the renderer into the browser process, which is the split the sandbox relies on."""
    assert check_launch_args(["--single-process"]).permitted is False


# ===========================================================================
# DEBUGGING - REFUSED BY DEFAULT, LOOPBACK ONLY WHEN ASKED
# ===========================================================================
@pytest.mark.parametrize("flag", DEBUGGING_FLAGS)
def test_debugging_is_refused_BY_DEFAULT(flag):
    d = check_launch_args([f"{flag}=9222"])
    assert d.permitted is False
    assert "DevTools" in d.because or "debugging" in d.because.lower()


def test_port_zero_is_not_a_loophole():
    """A random port is still a LISTENING SOCKET, and the endpoint is the exposure, not the number."""
    assert check_launch_args(["--remote-debugging-port=0"]).permitted is False


def test_debugging_on_loopback_is_permitted_when_asked():
    d = check_launch_args(["--remote-debugging-port=9222", "--remote-debugging-address=127.0.0.1"],
                          allow_debugging=True)
    assert d.permitted is True
    assert "loopback" in d.because


def test_debugging_bound_to_ALL_INTERFACES_is_refused_even_when_opted_in():
    """The opt-in permits a loopback port. A switch that could permit a public one would just be the
    defect with a comment in front of it."""
    d = check_launch_args(["--remote-debugging-address=0.0.0.0"], allow_debugging=True)
    assert d.permitted is False
    assert "beyond loopback" in d.because


def test_a_public_debugging_address_is_refused():
    d = check_launch_args(["--remote-debugging-address=81.17.96.135"], allow_debugging=True)
    assert d.permitted is False


# ===========================================================================
# THE SAME-ORIGIN POLICY
# ===========================================================================
def test_disabling_web_security_is_refused():
    d = check_launch_args(["--disable-web-security"])
    assert d.permitted is False
    assert "same-origin" in d.because


# ===========================================================================
# WHAT IS DELIBERATELY ALLOWED
# ===========================================================================
def test_shm_workaround_is_permitted_and_the_reason_is_recorded():
    """Refusing it would push someone toward --no-sandbox, which is worse. It weakens no boundary."""
    d = check_launch_args(["--disable-dev-shm-usage"])
    assert d.permitted is True
    assert "does not refuse unrecognised flags" in " ".join(describe()["does_not_do"])


def test_an_unknown_flag_is_REPORTED_not_refused():
    """A guard that refuses every unrecognised flag is one people work around."""
    d = check_launch_args(["--disable-dev-shm-usage", "--some-new-flag"])
    assert d.permitted is True
    assert "--some-new-flag" in d.unrecognised


def test_no_args_is_permitted():
    assert check_launch_args([]).permitted is True


def test_user_data_dir_is_not_reported_as_unrecognised():
    """It is passed as an argument and is expected; reporting it every run would train people to
    ignore the report."""
    d = check_launch_args(["--user-data-dir=/tmp/profile"])
    assert d.permitted is True
    assert d.unrecognised == ()


# ===========================================================================
# REFUSAL IS BY RAISING
# ===========================================================================
def test_assert_refuses_by_raising():
    with pytest.raises(LaunchArgsRefused):
        assert_launch_args(["--no-sandbox"])


def test_assert_returns_a_decision_when_permitted():
    assert assert_launch_args(["--disable-dev-shm-usage"]).permitted is True


def test_flag_matching_ignores_the_value():
    """`--no-sandbox` must be caught whether or not a value is attached."""
    assert check_launch_args(["--no-sandbox=true"]).permitted is False


# ===========================================================================
# THE BOUNDARY IS STATED
# ===========================================================================
def test_describe_states_the_rule_and_its_limits():
    d = describe()
    assert sorted(d["refused_always"]["sandbox"]) == sorted(SANDBOX_FLAGS)
    assert "loopback" in d["refused_unless_opted_in"]["and_then"]
    assert "ABSENCE" in d["why"]
    joined = " ".join(d["does_not_do"])
    assert "work around" in joined
    assert "AppArmor" in joined, "the guard must say what it does not cover"
