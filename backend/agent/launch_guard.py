"""The browser's launch arguments, checked rather than trusted.

TWO §8 REQUIREMENTS THAT WERE TRUE ONLY BY OMISSION
---------------------------------------------------
Inspected before assuming. `PlaywrightProvider.launch` passes:

    args=["--disable-dev-shm-usage"]

No `--no-sandbox` - correct, and the docstring explains why the AppArmor profile exists instead. No
`--remote-debugging-port` - correct, so no browser-debugging endpoint is exposed.

Both are satisfied **by absence from a list of strings.** A future edit adding a debugging flag for one
debugging session, or a `--no-sandbox` to make a container start, would silently undo a security
property that no test was watching. "True today" and "enforced" are different claims, and the directive
has already produced five defects of exactly that shape.

WHAT IS REFUSED

  * any flag that exposes the DevTools protocol - `--remote-debugging-port`,
    `--remote-debugging-address`, `--remote-debugging-pipe`. Port 0 gets a random port, which is still
    a listening socket, and `--remote-debugging-address=0.0.0.0` is a publicly reachable one.
  * any flag that disables the sandbox - `--no-sandbox`, `--disable-setuid-sandbox`,
    `--single-process` (which defeats the process boundary the sandbox depends on).
  * `--disable-web-security`, which removes the same-origin policy the SSRF boundary partly relies on.

WHAT IS DELIBERATELY ALLOWED

`--disable-dev-shm-usage` is permitted and is present today: it redirects shared memory to /tmp because
a container's default /dev/shm is 64 MB and Chromium crashes on it. It weakens no boundary - it is a
capacity workaround, and refusing it would push someone toward `--no-sandbox`, which is worse.

AN OPT-IN THAT IS NOT A LOOPHOLE

`allow_debugging` exists for a genuine local-diagnostics case, and defaults False. It does **not** make
`--no-sandbox` acceptable at any setting, and it does not make `--remote-debugging-address=0.0.0.0`
acceptable: opting in permits a LOOPBACK debugging port only. A switch that could permit a public
one would just be the defect with a comment in front of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

#: Flags that expose the DevTools protocol. Any of these is a debugging endpoint.
DEBUGGING_FLAGS = (
    "--remote-debugging-port",
    "--remote-debugging-address",
    "--remote-debugging-pipe",
)

#: Flags that remove a security boundary. `--single-process` is here because it collapses the renderer
#: into the browser process, which the Chromium sandbox depends on being separate.
SANDBOX_FLAGS = (
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--single-process",
)

#: Flags that remove the same-origin policy.
WEB_SECURITY_FLAGS = ("--disable-web-security",)

#: Known and accepted, with the reason - so the allowance is a decision rather than an omission.
PERMITTED_FLAGS = {
    "--disable-dev-shm-usage": (
        "redirects shared memory to /tmp; a container's /dev/shm is 64 MB and Chromium crashes on it. "
        "It weakens no boundary, and refusing it would push someone toward --no-sandbox"
    ),
    "--headless": "the runtime is headless by design",
    "--disable-gpu": "no GPU on the host; unrelated to any boundary",
    "--no-first-run": "suppresses first-run UI, which has no place in an automated profile",
    "--no-default-browser-check": "same",
}


class LaunchArgsRefused(RuntimeError):
    """The launch arguments would remove a security boundary. Refused, never warned about."""


@dataclass(frozen=True)
class LaunchDecision:
    permitted: bool
    because: str
    refused: tuple[str, ...] = ()
    #: Flags present that are neither refused nor in the known list. Reported, not refused - an
    #: unrecognised flag is a review prompt, not automatically a vulnerability, and refusing every
    #: unknown flag would make the guard something people work around.
    unrecognised: tuple[str, ...] = ()


def _flag_name(argument: str) -> str:
    return argument.split("=", 1)[0].strip()


def check_launch_args(args: Iterable[str], *, allow_debugging: bool = False) -> LaunchDecision:
    """Whether these launch arguments preserve every boundary §8 requires."""
    items = [a for a in (args or []) if a]
    names = {_flag_name(a) for a in items}

    # 1. SANDBOX. Refused at any setting, including with allow_debugging - the directive forbids
    #    turning the sandbox off "merely to make installation easier", and there is no setting at which
    #    that becomes acceptable.
    hit = sorted(names & set(SANDBOX_FLAGS))
    if hit:
        return LaunchDecision(
            permitted=False,
            because=(
                f"{', '.join(hit)} disables a security boundary. The directive forbids running Chromium "
                "with the sandbox off to make installation easier; the AppArmor profile exists so the "
                "sandbox can stay on, and --single-process collapses the process split it depends on"
            ),
            refused=tuple(hit),
        )

    hit = sorted(names & set(WEB_SECURITY_FLAGS))
    if hit:
        return LaunchDecision(
            permitted=False,
            because=(
                f"{', '.join(hit)} removes the same-origin policy, which the SSRF and target-domain "
                "boundaries partly rely on"
            ),
            refused=tuple(hit),
        )

    # 2. DEBUGGING. Permitted ONLY on loopback and only when asked for. A random port is still a
    #    listening socket, so `--remote-debugging-port=0` is not a loophole.
    hit = sorted(names & set(DEBUGGING_FLAGS))
    if hit:
        if not allow_debugging:
            return LaunchDecision(
                permitted=False,
                because=(
                    f"{', '.join(hit)} exposes the DevTools protocol. §8 requires no publicly exposed "
                    "browser-debugging endpoint; a debugging session is not a reason to have one open "
                    "by default"
                ),
                refused=tuple(hit),
            )
        address = ""
        for item in items:
            if _flag_name(item) == "--remote-debugging-address":
                address = item.split("=", 1)[1] if "=" in item else ""
        if address and address not in ("127.0.0.1", "localhost", "::1", "[::1]"):
            return LaunchDecision(
                permitted=False,
                because=(
                    f"--remote-debugging-address={address} binds the DevTools protocol beyond loopback. "
                    "Opting in permits a LOOPBACK debugging port only; a switch that could permit a "
                    "public one would be the defect with a comment in front of it"
                ),
                refused=("--remote-debugging-address",),
            )
        return LaunchDecision(
            permitted=True,
            because="debugging requested on loopback only, and no security boundary was removed",
        )

    # 3. Everything else. Known flags are fine; unknown ones are reported for review.
    unknown = sorted(n for n in names if n not in PERMITTED_FLAGS and not n.startswith("--user-data-dir"))
    return LaunchDecision(
        permitted=True,
        because="no debugging endpoint and no removed security boundary",
        unrecognised=tuple(unknown),
    )


def assert_launch_args(args: Iterable[str], *, allow_debugging: bool = False) -> LaunchDecision:
    """`check_launch_args`, refusing rather than reporting."""
    decision = check_launch_args(args, allow_debugging=allow_debugging)
    if not decision.permitted:
        raise LaunchArgsRefused(decision.because)
    return decision


def describe() -> dict[str, Any]:
    """The rule, stated where a reviewer will find it."""
    return {
        "refused_always": {
            "sandbox": list(SANDBOX_FLAGS),
            "web_security": list(WEB_SECURITY_FLAGS),
        },
        "refused_unless_opted_in": {
            "debugging": list(DEBUGGING_FLAGS),
            "and_then": "loopback addresses only - port 0 is also a listening socket",
        },
        "permitted": dict(PERMITTED_FLAGS),
        "why": (
            "these were true only by ABSENCE from the launch argument list; an edit adding a debugging "
            "flag or a --no-sandbox would have silently undone a security property no test watched"
        ),
        "does_not_do": [
            "it does not refuse unrecognised flags - it reports them, because a guard that refuses "
            "every unknown flag is one people work around",
            "it does not check the AppArmor profile, which is host configuration outside the process",
        ],
    }
