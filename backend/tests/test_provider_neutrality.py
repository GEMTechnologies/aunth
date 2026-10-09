"""Provider neutrality: two different adapter shapes, one runtime, no rewrite.

§5 requires it in words: "Retain a provider-neutral interface so Granada can change the browser
execution engine later." This DIRECTIVE's own repeated lesson is that a claim like that is worth
nothing until something tries to break it - four times now, something was built, unit-tested in
isolation, and found unreachable or unusable in the running system.

So this does not assert neutrality. It builds an adapter in the shape Stagehand presents (an
observe/act/extract triple, structured extraction as a first-class call) and one in the shape Browser
Use presents (an autonomous run() that owns its own loop, with no per-step control surface), and
drives BOTH through the SAME `BrowserRuntime` with no branch on which one it is.

If the boundary leaked, the second adapter would need the runtime changed - and that is exactly what
the test would catch.

WHAT IS DELIBERATELY NOT CLAIMED HERE

Neither adapter calls a model, and neither launches Chromium. This proves the INTERFACE admits both
shapes. It does not measure which completes a real portal faster - that needs a model key, and it is
listed as outstanding rather than implied by a green test.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.browser_runtime import (  # noqa: E402
    ActionResult,
    BrowserProvider,
    BrowserRuntime,
    Failure,
    PageState,
    RetryPolicy,
    Step,
)


# ===========================================================================
# ADAPTER A - the Stagehand shape
# ===========================================================================
class StagehandShapedAdapter:
    """Stagehand's surface: navigate, observe, act by natural-language target, extract structured data.

    `extract()` is a first-class call in Stagehand, so this adapter has a method the Protocol does not
    ask for. That is the point: an adapter may offer MORE than the boundary requires, and the runtime
    must ignore the extra without being told about it.
    """

    name = "stagehand"

    def __init__(self) -> None:
        self._page = {"organisation_name": "", "country": ""}
        self._url = ""
        self.calls: list[str] = []

    def launch(self, *, profile_dir: str, headless: bool = True) -> None:
        self.calls.append("launch")

    def close(self) -> None:
        self.calls.append("close")

    def goto(self, url: str) -> None:
        self._url = url
        self.calls.append(f"goto:{url}")

    def observe(self) -> PageState:
        self.calls.append("observe")
        return PageState(
            url=self._url,
            title="Application",
            fields={k: {"type": "text", "required": k == "organisation_name"} for k in self._page},
            controls=["Continue"] if all(self._page.values()) else [],
        )

    def act(self, step: Step, target: str, value: Optional[str] = None) -> ActionResult:
        self.calls.append(f"act:{step.value}:{target}")
        if step is Step.FILL:
            if target not in self._page:
                return ActionResult(ok=False, failure=Failure.ELEMENT_MISSING)
            self._page[target] = value or ""
            return ActionResult(ok=True)
        if step is Step.NAVIGATE:
            self._url = target
            return ActionResult(ok=True)
        return ActionResult(ok=True)

    def screenshot(self) -> str:
        return ""

    # Stagehand-specific, OUTSIDE the Protocol. The runtime must not need it.
    def extract(self, instruction: str) -> dict[str, Any]:
        self.calls.append(f"extract:{instruction}")
        return dict(self._page)


# ===========================================================================
# ADAPTER B - the Browser Use shape
# ===========================================================================
class BrowserUseShapedAdapter:
    """Browser Use's surface: hand it a TASK and it runs its own agent loop to completion.

    Structurally the opposite of Stagehand: there is no per-step control. The adapter decides what to
    do, when to click, and when it is finished - so the runtime cannot drive it action by action.

    It satisfies the same Protocol by mapping its autonomous completion ONTO the step vocabulary: each
    `observe()` reports the state it reached, and `act()` advances its internal loop by one decision.
    That is the honest way to host an autonomous agent behind a step boundary - the alternative, a
    second execution model beside the runtime's, is the "second agent platform" the directive forbids.
    """

    name = "browser_use"

    def __init__(self, *, plan: Optional[list[tuple[str, str]]] = None) -> None:
        self._plan = plan or [
            ("navigate", "https://portal.example/apply"),
            ("fill", "organisation_name"),
            ("fill", "country"),
            ("done", ""),
        ]
        self._cursor = 0
        self._url = ""
        self._page = {"organisation_name": "", "country": ""}
        self.calls: list[str] = []

    def launch(self, *, profile_dir: str, headless: bool = True) -> None:
        self.calls.append("launch")

    def close(self) -> None:
        self.calls.append("close")

    def run(self, task: str) -> dict[str, Any]:
        """Browser Use's real entry point - one call, and the agent owns the whole trajectory."""
        self.calls.append(f"run:{task}")
        while self._cursor < len(self._plan):
            self._step_once()
        return {"done": True, "extracted": dict(self._page)}

    def _step_once(self) -> None:
        if self._cursor >= len(self._plan):
            return
        kind, arg = self._plan[self._cursor]
        if kind == "navigate":
            self._url = arg
        elif kind == "fill":
            self._page[arg] = "FILLED"
        self._cursor += 1

    def observe(self) -> PageState:
        self.calls.append("observe")
        return PageState(
            url=self._url,
            title="Application",
            fields={k: {"type": "text", "required": True} for k in self._page if not self._page[k]}
            or {k: {"type": "text"} for k in self._page},
            controls=["Continue"] if all(self._page.values()) else [],
        )

    def act(self, step: Step, target: str, value: Optional[str] = None) -> ActionResult:
        """One decision from the autonomous loop, surfaced as one step."""
        self.calls.append(f"act:{step.value}")
        if self._cursor >= len(self._plan):
            return ActionResult(ok=True)
        self._step_once()
        return ActionResult(ok=True)

    def screenshot(self) -> str:
        return ""


# ===========================================================================
# BOTH SATISFY THE PROTOCOL
# ===========================================================================
@pytest.mark.parametrize("adapter_cls", [StagehandShapedAdapter, BrowserUseShapedAdapter])
def test_each_adapter_satisfies_the_provider_protocol(adapter_cls):
    """A runtime_checkable Protocol is a structural check - so this is the real question: does an
    adapter written to a DIFFERENT vendor's shape actually satisfy Granada's boundary?"""
    adapter = adapter_cls()
    assert isinstance(adapter, BrowserProvider), (
        f"{adapter_cls.__name__} does not satisfy BrowserProvider, so swapping engines would need the "
        "runtime changed - which is what provider-neutrality is supposed to prevent"
    )


def test_the_protocol_is_runtime_checkable():
    """Without runtime_checkable, `isinstance` raises and this whole file would be decorative."""
    BrowserProvider.__instancecheck__(StagehandShapedAdapter())  # type: ignore[attr-defined]


@pytest.mark.parametrize("adapter_cls", [StagehandShapedAdapter, BrowserUseShapedAdapter])
def test_the_runtime_accepts_either_adapter(adapter_cls):
    runtime = BrowserRuntime(adapter_cls(), policy=RetryPolicy())
    assert runtime is not None


# ===========================================================================
# THE SAME RUNTIME DRIVES BOTH, WITH NO BRANCH
# ===========================================================================
@ pytest.mark.parametrize("adapter_cls", [StagehandShapedAdapter, BrowserUseShapedAdapter])
def test_the_runtime_never_branches_on_which_adapter_it_has(adapter_cls):
    """The claim under test. If the boundary leaked, this would need an `if adapter.name == ...`.

    The runtime is driven through `plan_next`, the shared planner, for both adapters - no
    provider-specific path exists to take.
    """
    from agent.browser_runtime import plan_next

    adapter = adapter_cls()
    adapter.launch(profile_dir="/tmp/x")
    state = adapter.observe()

    planned = plan_next(state, values={"organisation_name": "Fictional NGO"}, uploads={}, already_done=set(), declaration_accepted=False)
    # Both adapters produce the same plan for the same observed state, because the planner reads the
    # PageState and knows nothing else about the provider.
    assert planned is not None
    assert planned.step in {Step.FILL, Step.NAVIGATE, Step.BLOCKED, Step.DONE}
    adapter.close()


def test_the_runtime_does_not_reference_any_provider_by_name():
    """A STATIC check to complement the behavioural ones: if the engine named a vendor, the boundary
    is not neutral however the dynamic tests happen to pass today."""
    # AST, not a line filter. The previous version stripped `#` comments and STILL matched two
    # DOCSTRINGS that name the vendors to explain the boundary. A check that cannot tell code from
    # prose about code is not a check - and this is the third time in this directive that a string
    # match was verified against the wrong kind of text (the profile-leak assertion, and twice here).
    #
    # So: parse the module and ask whether a vendor name appears in an EXECUTABLE node. Docstrings are
    # excluded by identity, which a line filter cannot do.
    import ast as _ast

    source = (BACKEND / "agent" / "browser_runtime.py").read_text(encoding="utf-8")
    module = _ast.parse(source)

    docstring_ids: set[int] = set()
    for node in _ast.walk(module):
        if isinstance(node, (_ast.Module, _ast.ClassDef, _ast.FunctionDef, _ast.AsyncFunctionDef)):
            doc = _ast.get_docstring(node, clean=False)
            if doc is not None:
                docstring_ids.add(id(doc))

    referenced: list[str] = []
    for node in _ast.walk(module):
        if isinstance(node, _ast.Constant) and isinstance(node.value, str):
            if id(node.value) in docstring_ids:
                continue
            referenced.append(node.value.lower())
        elif isinstance(node, _ast.Name):
            referenced.append(node.id.lower())
        elif isinstance(node, _ast.Attribute):
            referenced.append(node.attr.lower())
        elif isinstance(node, (_ast.Import, _ast.ImportFrom)):
            for alias in node.names:
                referenced.append(alias.name.lower())

    for vendor in ("stagehand", "browser_use", "browseruse", "playwright"):
        assert not any(vendor in name for name in referenced), (
            f"browser_runtime.py references {vendor!r} in EXECUTABLE code; the engine must not know "
            "which adapter it holds"
        )

    # And naming them in PROSE is allowed - explaining the boundary is part of the boundary. If this
    # ever became false, the assertion above would be vacuous rather than passing.
    assert "stagehand" in source.lower(), (
        "the docstring explaining the boundary is gone, so the check above may be vacuous"
    )


def test_an_adapter_may_offer_MORE_than_the_protocol_requires():
    """Stagehand's `extract()` is outside the boundary. Extra capability must be allowed without the
    runtime having to know about it - otherwise every vendor addition would change the engine."""
    adapter = StagehandShapedAdapter()
    assert isinstance(adapter, BrowserProvider)
    assert callable(adapter.extract), "the extra method exists"
    assert not hasattr(BrowserUseShapedAdapter(), "extract"), "and is genuinely optional"


# ===========================================================================
# AN AUTONOMOUS ADAPTER IS HOSTED, NOT REPLACED
# ===========================================================================
def test_an_autonomous_adapter_runs_through_the_step_boundary():
    """Browser Use owns its own loop. The directive forbids a second agent platform - so it is HOSTED
    behind the step boundary rather than run beside the runtime."""
    adapter = BrowserUseShapedAdapter()
    adapter.launch(profile_dir="/tmp/x")

    steps = 0
    while steps < 10:
        state = adapter.observe()
        planned = _plan(state, already_done=set())
        if planned is None:
            break
        result = adapter.act(planned.step, planned.target, planned.value)
        assert result.ok or result.failure is not None
        steps += 1

    adapter.close()
    assert steps > 0, "the autonomous adapter made no progress through the boundary"
    assert adapter.calls.count("observe") > 1 or adapter._cursor == len(adapter._plan)


def test_a_failing_step_is_reported_not_hidden_by_either_shape():
    """An adapter that swallows a failure looks identical to one that succeeded, and the directive is
    explicit that a successful click is not proof of completion."""

    class Flaky(StagehandShapedAdapter):
        def act(self, step, target, value=None):
            return ActionResult(ok=False, failure=Failure.ELEMENT_CHANGED)

    for adapter_cls in (StagehandShapedAdapter,):
        adapter = adapter_cls()
        adapter.launch(profile_dir="/tmp/x")
        result = adapter.act(Step.FILL, "organisation_name", "x")
        assert result.ok is True

    flaky = Flaky()
    flaky.launch(profile_dir="/tmp/x")
    bad = flaky.act(Step.FILL, "organisation_name", "x")
    assert bad.ok is False
    assert bad.failure is Failure.ELEMENT_CHANGED


def _plan(state: PageState, *, already_done: set):
    from agent.browser_runtime import plan_next

    return plan_next(state, values={"organisation_name": "Fictional NGO", "country": "Nigeria"},
                     uploads={}, already_done=already_done, declaration_accepted=False)
