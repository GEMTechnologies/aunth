#!/usr/bin/env python3
"""The host-side browser worker. One task in, one outcome out.

WHY THIS RUNS ON THE HOST AND NOT IN THE EXECUTOR CONTAINER
-----------------------------------------------------------
Measured, not assumed:

    executor container   CapEff 0000000000000000, Docker seccomp profile
    unshare --user       "unshare failed: Operation not permitted"

Chromium's sandbox is built on that syscall, so a SANDBOXED browser cannot start in the container.
The alternatives were `--no-sandbox` (the directive forbids it) or unconfining the container's seccomp,
which would strip syscall filtering from every job the executor runs rather than only browser jobs.

On the host it works: exit 0, real DOM, sandbox on, with an AppArmor profile granting user namespaces
to two binary paths only. That is a narrower change than either alternative.

WHAT IT IS NOT
--------------
It is not an agent. It does not plan, decide authority, verify outcomes or reconcile - those are
`browser_runtime`, `action_grounding`, `submission_authority`, `verification` and
`submission_lifecycle`, and duplicating any of them here would put decisions outside the tested
modules. This process is a PROVIDER: it observes a page and performs actions, and reports what
happened.

It also never decides it has submitted. A receipt is passed through if the page yielded one; the
outcome is `SUBMISSION_PENDING` otherwise, because a click is not a confirmation.

USAGE
-----
    echo '<BrowserTask json>' | browser_worker.py

Reads the task from stdin and writes one JSON object to stdout. Nothing tenant-identifying is passed
on the command line, where it would appear in the process table.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# The worker imports the same runtime the tests exercise. It is deliberately NOT a second
# implementation: everything it does is expressed through the Agent* contracts.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.browser_runtime import (  # noqa: E402
    BrowserRuntime,
    BrowserError,
    Failure,
    PageState,
    RetryPolicy,
    Step,
    ActionResult,
)


@dataclass
class Perception:
    """What was seen on one page, in the shape browser_runtime plans from.

    Structural only. Vision, when a model is configured, would ADD to this rather than replace it -
    the runtime already knows how to escalate when structure is silent.
    """

    url: str = ""
    title: str = ""
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    controls: list[str] = field(default_factory=list)
    validation_messages: list[str] = field(default_factory=list)
    untrusted_text: str = ""


class PlaywrightProvider:
    """A BrowserProvider backed by Playwright's Chromium, with the sandbox ON.

    One browser per task, closed in a finally block by the runtime. No session is retained between
    tasks, because the directive forbids a permanent browser process per organisation and because
    retaining one would be the easiest way to leak state across tenants.
    """

    name = "playwright-chromium"

    def __init__(self, *, headless: bool = True, timeout_ms: int = 20000) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._profile: Optional[str] = None
        #: Where captured artefacts go so they outlive the run. None means scratch-only.
        self._evidence: Any = None
        #: Whether the last reference returned by screenshot() survives close().
        self.evidence_durable: bool = False

    # -- lifecycle -----------------------------------------------------------
    def launch(self, *, profile_dir: str, headless: bool = True) -> None:
        from playwright.sync_api import sync_playwright

        # THE TENANT PROFILE IS THE mkdtemp DIRECTORY, NOT `profile_dir`.
        #
        # `os.makedirs(profile_dir)` used to run here and create /tmp/granada-browser/<org_id>/ - and
        # NOTHING REMOVED IT. `close()` removes `self._profile`, which is the mkdtemp directory, so
        # every organisation that ever ran left a directory named after itself, forever. On a host
        # serving thousands of NGOs that is both an unbounded leak and a directory listing of which
        # organisations have been active.
        #
        # The real Chromium profile is `self._profile`: launch_persistent_context uses it as
        # `user_data_dir`, which is what actually gives each organisation its own cookie jar. The
        # `profile_dir` argument was created and then never used - dead code that leaked.
        #
        # `profile_dir` is kept in the signature because the runtime supplies it and a caller may
        # want it for diagnostics, but it is NOT created. A directory that exists only to be
        # abandoned is worse than no directory.
        self._profile = tempfile.mkdtemp(prefix="granada-browser-")
        self._pw = sync_playwright().start()

        # THE LAUNCH ARGUMENTS ARE CHECKED, NOT TRUSTED.
        #
        # Note there is no `args=["--no-sandbox"]`: the AppArmor profile at
        # /etc/apparmor.d/granada-chromium grants `userns` to the two Playwright binary paths, and that
        # is the supported route. Both properties here - sandbox ON, and no debugging endpoint - were
        # true only by ABSENCE from this list: one edit adding a debugging flag, or a --no-sandbox to
        # make a container start, would have silently undone a security property no test was watching.
        # So the list is now validated before it is used.
        from agent.launch_guard import assert_launch_args

        launch_args = ["--disable-dev-shm-usage"]
        assert_launch_args(launch_args)

        # launch_persistent_context, NOT new_context: `user_data_dir` is a persistent-context
        # parameter, and passing it to new_context raises TypeError. The persistent form is also the
        # correct one here - it is what gives each organisation its own profile directory on disk, so
        # cookies and local storage cannot cross between tenants.
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=self._profile,
            headless=headless and self.headless,
            args=launch_args,
            viewport={"width": 1280, "height": 900},
        )
        self._browser = self._context.browser
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_timeout(self.timeout_ms)

    def authenticate(self, *, username: str, password: str) -> bool:
        """Sign in before the application form is reached.

        CREDENTIALS COME FROM THE ENVIRONMENT, NEVER FROM THE TASK PAYLOAD. The payload travels
        through the job system and is persisted; a password belongs in neither. BrowserTask carries
        CredentialRef (a name, not a secret) for exactly this reason.

        A login form is a form. Granada fills it only because the values are SUPPLIED, not inferred -
        the same rule that stops it inventing a registration number.
        """
        try:
            self._page.fill("[name='email']", username)
            self._page.fill("[name='password']", password)
            # The button's ID, not its text. `text=Sign in` matched the page's <h1>Sign in</h1>
            # heading first and clicked that, so the form was never submitted - which is the same
            # lesson as the grounding module one layer down: a label is not a control.
            self._page.click("#sign-in")
            self._page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
            # Verified by the page, not by the click: a click that did not sign in is not a session.
            #
            # A POSITIVE check. The first version asked whether the title lacked the substring
            # "sign" - but the success page is titled "Signed in", so it always answered "not signed
            # in" and every authenticated run reported AUTHENTICATION_FAILED. A negative assertion
            # against ambiguous text is not a check; the portal's own marker is.
            return self._page.query_selector("a[href='/apply/start']") is not None
        except Exception:
            return False

    def goto(self, url: str) -> None:
        """Navigate to the task target. WITHOUT THIS the worker plans against about:blank.

        The first end-to-end run reported status=COMPLETED having completed zero steps, because
        nothing had been loaded, nothing was required, and "nothing to do" was indistinguishable from
        "done". That is precisely the false completion claim the directive forbids: a successful
        browser command is not proof of task completion.
        """
        self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)

    def close(self) -> None:
        for closer in (getattr(self, "_context", None), self._browser, self._pw):
            try:
                if closer is not None:
                    closer.close() if hasattr(closer, "close") else closer.stop()
            except Exception:
                pass
        self._context = self._browser = self._pw = self._page = None
        if self._profile and os.path.isdir(self._profile):
            shutil.rmtree(self._profile, ignore_errors=True)
        self._profile = None

        # A CRASH LEAVES THE mkdtemp DIRECTORY BEHIND, because close() never runs. The runtime calls
        # close() in a finally, but a SIGKILL does not reach finally. Sweep any sibling profile from a
        # previous run of THIS process, bounded and best-effort: a leak that only appears on crashes
        # is the one nobody notices until the disk is full.
        _sweep_stale_profiles(keep=None)

    # -- perception ----------------------------------------------------------
    def observe(self) -> PageState:
        """Read the page's structure. Deterministic, no model, no inference.

        Fields carry their label as well as their name, which is what lets the planner work on a
        relabelled page - the `?variant=b` case the whole comparison rests on.
        """
        page = self._page
        data = page.evaluate(
            """() => {
                const els = Array.from(document.querySelectorAll('input,select,textarea'));
                const fields = {};
                for (const el of els) {
                    if (!el.name) continue;
                    let label = '';
                    if (el.labels && el.labels.length) label = el.labels[0].innerText.trim();
                    if (!label && el.getAttribute('aria-label')) label = el.getAttribute('aria-label');
                    fields[el.name] = {
                        label: label,
                        type: (el.type || el.tagName).toLowerCase(),
                        required: !!(el.required || el.getAttribute('aria-required') === 'true'),
                        value: el.value || '',
                    };
                }
                const controls = Array.from(document.querySelectorAll('button,input[type=submit],a[role=button]'))
                    .map(b => (b.innerText || b.value || '').trim()).filter(Boolean);
                const errors = Array.from(document.querySelectorAll(
                    '.field-error,[role=alert],.error,.invalid-feedback'
                )).map(e => e.innerText.trim()).filter(Boolean);
                return {
                    url: location.href, title: document.title,
                    fields: fields, controls: controls, errors: errors,
                    text: document.body ? document.body.innerText.slice(0, 4000) : '',
                };
            }"""
        )
        # A SCREENSHOT IS CAPTURED WITH EVERY OBSERVATION, not only when something goes wrong.
        #
        # The visual path was built and unreachable: `perception.needs_vision` is true when a picture
        # exists and the tree does not answer the question, and an observation with no picture always
        # looked like a page with nothing to see. Capturing here is what connects them.
        #
        # The capture is best-effort. A screenshot that fails must not fail the run - the structural
        # observation is still valid, and refusing to proceed because a picture could not be taken
        # would turn a cosmetic problem into a blocked workflow.
        shot_ref = self.screenshot()

        return PageState(
            url=data.get("url", ""),
            title=data.get("title", ""),
            fields=data.get("fields", {}),
            controls=data.get("controls", []),
            validation_messages=data.get("errors", []),
            untrusted_text=data.get("text", ""),
            screenshot_ref=shot_ref,
            captured_at=datetime.now(timezone.utc),
        )

    def screenshot(self) -> str:
        """Capture a screenshot into the EVIDENCE area, and return its durable reference.

        IT USED TO WRITE INTO THE PROFILE DIRECTORY, WHICH close() DELETES. A live run captured
        16,982-byte PNGs, referenced them in its outcome, and left zero files on disk - so every
        evidence reference in every report pointed at something that no longer existed. That is worse
        than having no reference, because it LOOKS like evidence.

        The capture goes to scratch first (Playwright writes a file; it does not hand back bytes),
        and is then copied into the evidence store, whose lifetime is the report's rather than the
        run's. If no store is configured the path is returned unchanged and the reference will be
        short-lived - named in `evidence_durable` so a reader can tell which they received.
        """
        import time

        scratch = os.path.join(self._profile or tempfile.gettempdir(), f"shot-{int(time.time() * 1000)}.png")
        try:
            self._page.screenshot(path=scratch, full_page=True)
        except Exception:
            # Best-effort: the structural observation is still valid, and a missing picture must not
            # turn a cosmetic problem into a blocked workflow.
            return ""

        if self._evidence is None:
            self.evidence_durable = False
            return scratch

        try:
            record = self._evidence.store(kind="screenshot", source=Path(scratch), name=os.path.basename(scratch))
        except Exception:
            # The store's own bounds or a missing source. The capture still happened, so the scratch
            # path is returned - with the durability flag false, so nobody is told it is evidence
            # when it is about to be deleted.
            self.evidence_durable = False
            return scratch

        self.evidence_durable = True
        return record.ref

    # -- action --------------------------------------------------------------
    def act(self, step: Step, target: str, value: Optional[str] = None) -> ActionResult:
        """Perform one action, grounded in the current page.

        Failures are CLASSIFIED rather than merely caught, because the runtime's recovery policy
        depends on the class: a transient network error is retryable, an intercepted click is not.
        """
        page = self._page
        try:
            if step is Step.FILL:
                page.fill(f"[name='{target}']", value or "")
            elif step is Step.UPLOAD:
                page.set_input_files(f"[name='{target}']", value)
            elif step is Step.DECLARE:
                # A declaration control is a checkbox in the fixture and a button elsewhere; try both.
                try:
                    page.check(f"text={target}")
                except Exception:
                    page.click(f"text={target}")
            elif step is Step.CLICK:
                page.click(f"text={target}")
            elif step is Step.SUBMIT:
                # NO retry wrapper here. An ambiguous submit is the runtime's UNCERTAIN case, and
                # retrying inside the provider would file twice before the runtime ever saw it.
                page.click(f"text={target}")
            else:
                return ActionResult(ok=False, failure=Failure.UNKNOWN, detail=f"unsupported step {step}")
            page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
            return ActionResult(ok=True, page=self.observe())
        except Exception as exc:
            return ActionResult(ok=False, failure=_classify(exc), detail=str(exc)[:300])


#: Profiles older than this are assumed abandoned. Generous, because a slow portal is still a live
#: run, and sweeping a running session's cookies would break it in a way that looks like a site bug.
STALE_PROFILE_SECONDS = 6 * 60 * 60


def _sweep_stale_profiles(*, keep: str | None, now: float | None = None) -> list[str]:
    """Remove abandoned profile directories. Best-effort, bounded, and never fatal.

    Bounded by AGE rather than by name, because a profile belonging to a concurrent run must survive:
    sweeping a live session's cookies would corrupt an in-flight submission and look like a site bug.

    Returns what it removed, so a caller can report it rather than silently tidying.
    """
    import glob
    import time

    moment = now if now is not None else time.time()
    removed: list[str] = []
    for path in glob.glob(os.path.join(tempfile.gettempdir(), "granada-browser-*")):
        if keep and os.path.abspath(path) == os.path.abspath(keep):
            continue
        try:
            if moment - os.path.getmtime(path) < STALE_PROFILE_SECONDS:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path)
        except OSError:
            continue
    return removed


def _classify(exc: Exception) -> Failure:
    """Map a Playwright error to the runtime's failure vocabulary.

    Ordered from most specific to least: a timeout during submit must not be mistaken for a timeout
    while reading the page, because only one of those could have had an external effect.
    """
    text = str(exc).lower()
    if "captcha" in text or "recaptcha" in text:
        return Failure.HUMAN_VERIFICATION
    if "403" in text or "forbidden" in text:
        return Failure.ACCESS_DENIED
    if "strict mode violation" in text or "not attached" in text or "detached" in text:
        return Failure.ELEMENT_CHANGED
    if "timeout" in text:
        return Failure.TRANSIENT_NETWORK
    if "net::" in text:
        return Failure.TRANSIENT_NETWORK
    if "invalid" in text or "validation" in text:
        return Failure.VALIDATION_REJECTED
    return Failure.UNKNOWN


def main() -> int:
    """Read a task, run it, print one JSON outcome.

    A malformed input is reported as an outcome rather than a traceback, because the caller parses
    stdout and a stack trace on stderr with exit 0 would look like success.
    """
    # THE PRIVILEGE GUARD, BEFORE ANYTHING ELSE. ADR-0011 in its applied form: this process opens a
    # live portal, reads an organisation's documents and holds their credentials, so it must not
    # connect as `granada_fleet` - the BYPASSRLS role that sees every organisation. It needs no
    # cross-tenant visibility at all, and a worker that cannot prove it is narrow does not open a
    # page.
    from agent.worker_privileges import assert_worker_privileges, WorkerPrivilegeError

    try:
        assert_worker_privileges()
    except WorkerPrivilegeError as exc:
        print(json.dumps({
            "status": "REJECTED",
            "outcome_certain": True,
            "problems": [{"kind": "PRIVILEGE_REFUSED", "detail": str(exc)}],
        }))
        return 0

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        print(json.dumps({"status": "FAILED", "problems": [{"kind": "BAD_TASK_JSON"}]}))
        return 0

    # The worker trusts NOTHING in the payload beyond what it needs, and never treats page text as
    # instruction. The runtime re-validates the task; this is a second gate, not the only one.
    from agent.browser_boundary import ActionScope, BrowserTask

    # ActionScope must be CONSTRUCTED, not passed through as a dict. Sending the raw JSON made
    # validate_task fail with "'dict' object has no attribute 'allowed_hosts'" - and the worker's own
    # crash handler caught it and reported UNCERTAIN rather than a false success, which is the safety
    # design doing its job on its own author's bug.
    scope_payload = payload.get("action_scope") or {}
    try:
        scope = ActionScope(
            portal_name=str(scope_payload.get("portal_name", "")),
            allowed_hosts=tuple(scope_payload.get("allowed_hosts") or ()),
            allowed_path_prefixes=tuple(scope_payload.get("allowed_path_prefixes") or ()),
            max_steps=int(scope_payload.get("max_steps", 200)),
        )
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "problems": [{"kind": "BAD_ACTION_SCOPE", "detail": str(exc)[:200]}]}))
        return 0

    try:
        task = BrowserTask(
            task_id=payload.get("task_id", ""),
            org_id=payload.get("org_id", ""),
            package_id=payload.get("package_id", ""),
            workflow_id=None,
            job_id=None,
            package_fingerprint=payload.get("package_fingerprint", ""),
            action_scope=scope,
            credentials=[],
            form_data=payload.get("form_data") or {},
            documents=payload.get("documents") or [],
        )
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "problems": [{"kind": "TASK_CONSTRUCTION", "detail": str(exc)[:200]}]}))
        return 0

    values = {k: str(v) for k, v in (payload.get("form_data") or {}).items() if v not in (None, "")}
    uploads = {
        d.get("field") or d.get("doc_type") or "file": d.get("path", "")
        for d in (payload.get("documents") or [])
        if d.get("path")
    }

    # THE EVIDENCE STORE, opened for this run. Its lifetime is the REPORT's, not the run's - the
    # profile directory is deleted by close() and used to take every screenshot with it.
    from agent.evidence_store import EvidenceStore, open_store

    try:
        evidence = open_store(org_id=task.org_id, run_id=task.task_id)
    except Exception:
        # A store that cannot be opened must not stop the run: the browser work is still valid, and
        # the references are simply marked non-durable rather than presented as evidence.
        evidence = None

    provider = PlaywrightProvider()
    provider._evidence = evidence
    runtime = BrowserRuntime(provider, policy=RetryPolicy())
    target_url = payload.get("target_url") or ""
    try:
        # Navigate FIRST. The runtime plans from what the page shows, so an unloaded page is not an
        # empty task - it is an unasked question.
        provider.launch(profile_dir=f"/tmp/granada-browser/{task.org_id}", headless=True)
        creds_user = os.environ.get("GRANADA_PORTAL_USER", "")
        creds_pass = os.environ.get("GRANADA_PORTAL_PASSWORD", "")
        if creds_user and creds_pass:
            root = payload.get("login_url") or (target_url.rsplit("/", 2)[0] + "/login")
            provider.goto(root)
            signed_in = provider.authenticate(username=creds_user, password=creds_pass)
            if not signed_in:
                print(json.dumps({
                    "status": "BLOCKED",
                    "outcome_certain": True,
                    "problems": [{"kind": "AUTHENTICATION_FAILED",
                                  "detail": "the supplied credentials did not produce an authenticated session"}],
                }))
                return 0

        if not target_url:
            print(json.dumps({
                "status": "BLOCKED",
                "outcome_certain": True,
                "problems": [{
                    "kind": "NO_TARGET_URL",
                    "detail": "the task named no target_url, so there was nothing to open; refusing to report completion without having visited a page",
                }],
            }))
            return 0
        provider.goto(target_url)
        report = runtime.run(
            task, skip_launch=True,
            values=values,
            uploads=uploads,
            declaration_authorised=False,
            org_document_ids=set(payload.get("org_document_ids") or []),
            submission_authorised=False,   # never granted by the worker; authority is the caller's
        )
        out = report.to_dict()

        # FALSE-COMPLETION GUARD. "COMPLETED" with no completed steps and no uploads means the run
        # finished without doing anything - which is a blocked or unloaded page, not a success. The
        # directive is explicit that a successful browser command is not proof of task completion.
        # WHETHER THE REFERENCES SURVIVE. A report that lists evidence which is about to be deleted is
        # the defect this whole path exists to prevent, so the reader is told which they received.
        out["evidence_durable"] = bool(getattr(provider, "evidence_durable", False))
        if evidence is not None:
            missing = evidence.verify()
            out["evidence_files"] = len(evidence.stored)
            if missing:
                out["evidence_missing"] = missing

        if out.get("status") == "COMPLETED" and not out.get("completed_steps") and not out.get("uploaded"):
            out["status"] = "BLOCKED"
            out.setdefault("problems", []).append({
                "kind": "NOTHING_ACHIEVED",
                "detail": "the run reported completion having performed no steps; treating that as blocked rather than done",
            })
    except BrowserError as exc:
        out = {"status": "FAILED", "problems": [{"kind": "RUNTIME_BOUND", "detail": str(exc)[:300]}]}
    except Exception as exc:
        # A crash is reported as an uncertain outcome, NOT as success and not as a plain failure: if
        # it happened after a submit attempt, the runtime's own UNCERTAIN rule applies and the caller
        # must reconcile.
        out = {
            "status": "UNCERTAIN",
            "outcome_certain": False,
            "problems": [{"kind": "WORKER_CRASH", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}],
        }

    out["provider"] = provider.name
    out["sandbox"] = "enabled"

    # SCRUB BEFORE PRINTING. The portal password is filled into the page, and the page's own validation
    # messages come back through observe() -> report.to_dict(). A portal that echoes the submitted value
    # - one of the most common validation shapes there is - would put the credential in this JSON, which
    # the caller persists as a job outcome. Nothing had to be logged for it to escape.
    #
    # The values are read HERE, from the environment, and handed to a run-scoped scrubber. They are never
    # written anywhere, and `report()` records counts and names only.
    try:
        from agent.output_scrub import OutputScrubber

        scrubber = OutputScrubber(
            [os.environ.get("GRANADA_PORTAL_PASSWORD", ""), os.environ.get("GRANADA_PORTAL_USER", "")]
        )
        out = scrubber.scrub(out)
        out["scrub"] = scrubber.report()
    except Exception:
        # A scrubber that fails must not stop the outcome being reported, but the report SAYS the
        # outcome was not verified clean rather than implying it was.
        out = dict(out)
        out["scrub"] = {"error": "scrubber unavailable; output not verified free of credentials"}

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
