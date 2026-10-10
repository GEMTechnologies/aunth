# Self-hosted autonomous browser execution — phase report

Status: **the executor runs through the job system and completes a form field on a live controlled
portal.** Every claim below is either measured on the production VPS or reproduced by a test; where
something is *not* established it says so.

---

## A. Existing architecture inspected

The browser capability is not a second platform. It is a handler inside the existing fleet:

```
agent_workflows → jobs → job_attempts
   → agent.fleet_runner / agent.executor          (claims work, holds the lease)
   → WorkflowEngine._execute_handler              (builds the handler context)
   → WorkflowEngine._handle_browser_task          (agent/workflow_engine.py:2193)
   → browser_boundary.build_task                  (package → BrowserTask, readiness-gated)
   → browser_invocation.invoke                    (the capability gate + policy)
   → SubprocessInvoker                            (one process per invocation, no daemon)
   → tools/browser_execute_client.py              (container side, stdin JSON → HTTP)
   → 172.18.0.1:8765 tools/browser_execute_server.py  (host side, concurrency 1)
   → tools/browser_worker.py                      (Playwright/Chromium, observe→act→verify)
```

Inputs, per §6: tenant (`org_id`), opportunity (`workflow.subject_id`), package id + version,
**verified field values**, document references, authorised target URL, allowed hosts, workflow/job
ids, and `submission_authorised=False` at every call site.

Outputs land in `job_attempts` (outcome, duration) and `agent_activity` (`summary_key`, e.g.
`browser.blocked`). The workflow transitions to `COMPLETED` only on a completed run; `BLOCKED`,
`UNCERTAIN` and `UNAVAILABLE` all park at `WAITING` with `next_run_at` pushed forward, so a blocked
workflow does not generate jobs endlessly.

## B. Technology comparison

Measured in an earlier round; the full table is in `docs/adapter-evaluation.md`.

| | Stagehand Python | Browser Use Python |
|---|---|---|
| Launch | 5.1 s / RSS 1200.7 MB | 5.6 s / RSS 1485.0 MB |
| Model call on DeepSeek | **cannot** — `ModelConfig.model_name` validates against five Literal patterns and has no `base_url` | works |
| `tool_choice` | n/a | 400 on `deepseek-flash` (`_supports_thinking()` keeps it in thinking mode); `deepseek-v4-pro` works |
| Leak after close | 0 MB | 0 MB |

**Selected: Browser Use Python**, for one reason — it is the only one of the two that can make a
model call on Granada's configured provider. `BrowserProvider` remains a `@runtime_checkable`
Protocol, so the choice is reversible.

A correction made against my own earlier work: the first adapter evaluation concluded vision was
unavailable because the probe used `max_output_tokens=16`, and reasoning tokens drained the budget.
Re-measured, `deepseek-flash` reads images; `deepseek-v4-pro` silently drops them at HTTP 200.

## C. Implementation

New: `tools/browser_execute_server.py`, `tools/browser_execute_client.py`,
`tools/browser_worker_runner.py`, `tools/altered_portal.py`, `tools/test_portal.py`,
`docs/browser-execution-boundary.md`, `docs/adapter-evaluation.md`, `docs/adr-0011-*.{md,sql}`, and
the test modules named in §D.

Modified this phase: `agent/perception.py` (vision channel), `agent/browser_runtime.py`,
`agent/browser_invocation.py`, `agent/browser_boundary.py`, `agent/workflow_engine.py`,
`agent/executor.py`, `tools/browser_worker.py`.

No dependency was added to the Granada image to run the browser. The browser lives in a separate
host environment; the container keeps only the stdin/stdout client.

## D. Browser tests

Against the controlled portal (`tools/test_portal.py`, 127.0.0.1:8099), with fictitious data:

| Scenario | Result |
|---|---|
| Job drives the browser end to end | `SUCCEEDED`, 6.1 s, `browser.blocked` |
| Form field filled from a **verified** answer | **`invoke completed = ['FILL:email']`** |
| **Unverified** answer | not used — no `FILL:amount` |
| Login requirement recognised | blocked at `password`, correctly not invented |
| Missing organisation information | BLOCKED: "the page requires email and Granada holds no verified value" |
| Receipt discipline | `receipt: None` throughout; `submission_authorised=False` |
| Layout adaptation | `altered_portal.py` (no `label for`, ids `f1`/`f2`, reversed order) |

Registration, multi-page completion, uploads, validation recovery and mock submission are **not**
demonstrated — see §H.

## E. Resource consumption

7.75 GB RAM, ~6.6 GB available, 4 vCPU, 84 GB free disk. Idle stack ≈311 MB across six services.
Measured browser: ~1.2–1.5 GB peak RSS, released fully on close. Concurrency is capped at **1** —
by the execute service's `_SESSION_LOCK`, not by memory. The real ceiling is credential blast
radius, not RAM.

Observed through the job system: 26.3 s (refused before the fix), 6.1 s, 5.8 s (both after).

## F. Security

- **ADR-0011 findings.** The dispatcher was narrowed and cut over to per-tenant binding; the
  executor was not, and `BYPASSRLS` was revoked anyway. The executor then read **0 rows** of 1,443
  while reporting healthy. Fixed by giving `JobExecutor` the same roster-and-bind design the
  dispatcher uses (`fleet_active_agent_ids()`, SECURITY DEFINER, already granted). **No privilege was
  expanded and no new DDL was introduced.**
- Measured: session-scoped `set_config` does **not** survive a commit (pool
  `ResetStyle.reset_rollback`), so the executor re-binds before every job.
- `has_table_privilege('granada_fleet','credential_secrets','SELECT') = true`; `credential_secrets`
  row count = **0**. Migration 020 installs a restrictive policy; its row-level effect was **not**
  re-verified this round.
- SSRF: exact-host allow-list plus `target_guard` screening of loopback, private ranges and
  link-local (169.254/16 refused even with `resolve=False`). Loopback requires an explicit per-task
  opt-in. No publicly exposed debugging port; host port 8765 is bound to the docker bridge with a
  bridge-scoped UFW rule.
- A browser task for one organisation can reference no other organisation's documents —
  `validate_task` refuses a reference outside `org_document_ids`.

## G. Workflow integration

`PACKAGE → readiness → BrowserTask → gate → worker`, with the honest outcomes observed:

```
package (AWAITING_AUTHORISATION, target_url=http://127.0.0.1:8099/)
  → readiness READY
  → build_task(..., form_data=verified answers only, target_url, allow_loopback)
  → validate_task PASSED
  → invoke(submission_authorised=False)
  → SubprocessInvoker → client → host service → Chromium
  → FILL:email  →  BLOCKED on password
  → job SUCCEEDED, workflow WAITING, receipt None
```

## H. Outstanding work

1. **Credentials.** `_handle_browser_task` passes `credentials=None`, and `credential_secrets` is
   empty. The browser can fill forms but cannot log in. Storing a credential needs the encrypted
   store and `CREDENTIAL_ENCRYPTION_KEY`; connecting a plaintext value to make a demo proceed is the
   shortcut §8 forbids.
2. **Uploads.** The fixture's document slot carries no vault reference, so `task.documents` is empty
   and the file-upload path is untested end to end.
3. **§7 durable submission authorisation** is unimplemented: no record of which organisation granted
   authority, who granted it, its scope, expiry, revocation, or the covered package version.
   `submission_authorised=False` is hard-coded at the call site, which is safe but not a mechanism.
4. **§10 scenario sweep** not run: registration, conditional questions, session expiry, navigation
   interruption, final review, duplicate-submission protection.
5. **§11 lifecycle** is not connected to the submission contracts; no `FORM_VALIDATED` /
   `SUBMISSION_PENDING` transitions exist.
6. **ADR-0011** — the exact restrictions required before a live browser worker receives tenant
   credentials are documented in `docs/adr-0011-credential-restrictions.md`. The credential path is
   not merely unwired but **undecided**: the task carries a `CredentialRef`, the worker needs a
   value, and the choice of where the plaintext lives is a security decision with a transport
   consequence that must be accepted in writing first.

## I. Production status

- **Tests: 2062 passed, 101 skipped, 0 failed.**
- Commits: `b3c741f`, `949e6c9`, `618af59`, `930d4d4`, `1e23274` — pushed; local and host both clean
  and in sync.
- Six services healthy; served code verified, not just the build log.
- Alembic head `020_credential_deny_fleet`; `granada_fleet bypassrls=false`; queued jobs `0`.
- **Feature flag off**: agent settings `{}`, so browser execution is disabled by default.
- The original production package remains correctly blocked at `NEEDS_DATA/HANDOFF`; all fixtures
  used for testing were removed.
