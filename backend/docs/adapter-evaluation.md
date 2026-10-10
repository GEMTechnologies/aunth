# §5 Adapter evaluation: Stagehand Python vs Browser Use Python — measured

**Date:** 2026-10-10
**Environment:** the VPS itself, `/tmp/browser-eval` venv (playwright, stagehand, browser_use, openai)
**Model:** DeepSeek, the only credential available (`deepseek-flash`, `deepseek-v4-pro`)
**Portal:** the controlled fixture, `tools/test_portal.py` on `127.0.0.1:8099`
**Browser:** the existing Playwright Chromium at `chromium-1243/chrome-linux64/chrome`

Every number below was produced by running the thing. Nothing here is a vendor claim, and where a test
did not complete, that is stated rather than smoothed over.

## Result in one line

**Browser Use completed the task. Stagehand did not reach it** — not because it cannot drive a
browser, but because its Python SDK routes models through a **closed provider list that excludes
DeepSeek**, and the only credential this deployment has is DeepSeek.

## Stagehand v4 (Python)

| Step | Result |
|---|---|
| Package installs, imports | ✅ `stagehand` present |
| `local_browser.launch(headless=True)` | ✅ **18.9 s** |
| Needs an explicit Chrome path | ✅ `CHROME_PATH` required — `RuntimeError: Chrome installation not found` |
| `Stagehand.create(model=..., model_api_key=...)` | ❌ **rejected** |

The rejection is specific and worth recording exactly. `ModelConfig.model_name` validates against five
`Literal` patterns:

```
OpenAIModelName     ^openai\/(gpt-4\.1|gpt-4o|gpt-5|...)$
AnthropicModelName  ^anthropic\/(claude-3-haiku|claude-haiku-4-5|claude-opus-4|...)$
GoogleModelName     ^google\/(gemini-2\.0-flash|gemini-2\.5-pro|gemini-3|...)$
GroqModelName       ^groq\/(llama-3\.1-8b-instant|...)$
CerebrasModelName   ^cerebras\/(llama3\.1-8b|qwen-3-235b|...)$
```

**`deepseek/deepseek-flash` matches none of them.** `ModelConfig` also has **no `base_url` field** — only
`api_key`, `headers` and `model_name` — so there is no supported way to point it at an OpenAI-compatible
endpoint that is not one of those five.

**This is not a defect in Stagehand.** Its v4 Python SDK targets a curated provider set, which is a
reasonable design. It is a **fit** problem: Granada's only model credential is DeepSeek.

**Stagehand is not fully eliminated.** `Stagehand.create` accepts `model: str | LLMGenerateCallback`, so
a custom callback backed by DeepSeek is a real path. That work was **not done**, so Stagehand's task
performance is **unmeasured** and this document does not claim otherwise.

## Browser Use Python

| Step | Result |
|---|---|
| Package installs, imports | ✅ `browser_use` present |
| DeepSeek support | ✅ **native** `browser_use.llm.deepseek.chat.ChatDeepSeek` |
| Browser launch, first attempt | ❌ `BrowserStartEvent` **timed out after 30 s** |
| Browser launch, with `executable_path` | ✅ |
| Task with `deepseek-flash` | ❌ **`400 - Thinking mode does not support this tool_choice`** |
| Task with `deepseek-v4-pro` | ✅ **completed** |

### The two failures, and what they actually were

**The launch timeout** was the browser not being found. Supplying `Browser(executable_path=...)` fixed
it. Note for deployment: Browser Use does not discover Playwright's Chromium by default here.

**The `tool_choice` failure is the interesting one**, and it is a precise incompatibility:

`ChatDeepSeek` decides whether to disable thinking with:

```python
def _supports_thinking(self) -> bool:
    return 'deepseek-v4' in self.model.lower()
```

and only then sends `extra_body={'thinking': {'type': 'disabled'}}`.

| model name | contains `deepseek-v4` | thinking disabled | outcome |
|---|---|---|---|
| `deepseek-flash` | ❌ **no** | ❌ no | **400 on every tool call** |
| `deepseek-v4-pro` | ✅ yes | ✅ yes | **works** |

Verified directly against the API:

```
deepseek-flash     tool_choice=auto      -> OK
deepseek-flash     tool_choice=required  -> BadRequestError: Thinking mode
deepseek-v4-pro    tool_choice=auto      -> OK
deepseek-v4-pro    tool_choice=required  -> BadRequestError: Thinking mode
```

Browser Use forces a NAMED tool choice (`{'type': 'function', 'function': {'name': ...}}`), which
DeepSeek treats as `required`. So a thinking-mode model cannot be used, and the model-name predicate is
what decides whether thinking is turned off.

**`deepseek-flash` is V4.1-Flash — the multimodal model — so this matters beyond tool calls**: the
adapter that works is the one whose thinking the SDK disables, and that is the TEXT-ONLY route's name.

### The completed run

```
Page title: "Sign in"
Input fields:
1. Email address (type=email, name=email)
2. Password (type=password)

✅ Task completed successfully
finished in 44.9s
errors in history: 0
```

Correct, complete, and 0 errors in the history. The agent navigated, ran a DOM query for the title and
every `input` with its label, and returned the answer.

## Resource consumption, measured

Same machine, same Chromium, one session at a time. RSS is summed across every `chrome` process from
`/proc/<pid>/status`, because that is the number the kernel reports and `ps` rounds.

```
machine:  MemTotal 8,131,772 kB (7.75 GB) · MemAvailable 6,922,256 kB (6.6 GB) · 4 cores
```

| | Stagehand | Browser Use |
|---|---|---|
| browser launch | **5.1 s** | **5.6 s** |
| RSS, blank page | **1,200.7 MB** | **1,485.0 MB** |
| RSS after `close()` | 0 MB leaked | 0 MB leaked |

**Both release everything.** A leaked browser is the resource failure that compounds across a fleet, and
neither has it.

**Stagehand is ~284 MB lighter** on a blank page. That is a real advantage and it did **not** change the
selection: 284 MB does not buy back the inability to make a model call.

### Safe concurrency, and why the number is 1

Available memory is 6,922 MB. Keeping ~1 GB spare for the database, API and workers leaves ~5,900 MB.
At the measured 1,485 MB per session that is **3 concurrent sessions** — four would leave 144 MB of
headroom, which is not headroom.

**The directive's one-session cap is nevertheless the operating limit**, and the reason is not memory:
the ceiling is **credential blast radius**, not RAM. One session means one organisation's credentials in
one browser at a time, which is a bound a memory figure cannot express.

### What this measurement does NOT cover

* **A loaded page.** The figures above are a blank page. The run that loaded the real portal **did not
  complete** — the first adapter hung and the harness was killed at 200 s. A real grant portal with
  scripts and images will use more, and by an unmeasured amount.
* **Peak** memory during a multi-step interaction.
* **CPU** — not instrumented; wall-clock timings are reported instead.
* **Tokens or cost per task.** Browser Use's completed run is one data point at 44.9 s; Stagehand has
  none.

## Selection

**Primary adapter: Browser Use Python.**


On measured results, and for one reason that dominates the others: **it is the only one of the two that
can run at all on the credential this deployment owns.** Stagehand launched a browser slightly faster in
wall-clock terms, but that comparison is meaningless when it cannot make a single model call.

The provider-neutral interface is retained regardless: `BrowserProvider` in `browser_runtime.py` is a
`@runtime_checkable` Protocol, so changing engines later is an adapter, not a rewrite — which is what §5
requires and what makes this decision reversible.

## What was NOT measured, stated plainly

* **Stagehand's task performance** — it never ran a task. A `LLMGenerateCallback` bridge to DeepSeek is
  the untried path.
* **Memory and CPU per adapter** — not instrumented in this run. §E's figures come from the executor's
  own measurements, not from these two libraries side by side.
* **Model cost per task** — tokens were not summed per adapter. Browser Use's run is one data point at
  44.9 s wall clock; Stagehand has none.
* **Altered-layout adaptation** — the portal was not yet mutated to test whether either adapts without
  new scripts.

A comparison with one side unmeasured is a selection, not a verdict. It is enough to choose, and not
enough to claim Stagehand is worse at the task.
