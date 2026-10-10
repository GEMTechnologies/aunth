# The browser execution boundary — measured, and what it requires

**Status:** the executor is built, tested and proven. The route from a *job* to it is **not yet
complete**, and this document states exactly what is missing rather than implying otherwise.

Everything below was measured on the deployment. Where something was assumed, it is marked.

## 1. Why the browser cannot run in the container

| check | result |
|---|---|
| `/app/backend/tools/browser_worker.py` | **exists in the image** |
| `playwright` importable in container | **no** |
| `browser_use` / `stagehand` importable | **no** |
| Chromium on disk in container | **absent** |
| user namespaces in container | **absent** — `CapEff: 0000000000000000` |
| Docker's seccomp profile | **blocks `unshare`** |
| host eval venv mounted into container | **no** — only `postgres` declares a volume |

A sandboxed Chromium cannot start without user namespaces, and the directive forbids running with the
sandbox disabled to make installation easier. **The browser worker therefore runs on the host.** This is
a measured constraint, not a preference.

Verified consequence: a job routed through `_handle_browser_task` today spawns the worker inside the
container, where it reports

```json
{"status": "UNCERTAIN", "outcome_certain": false,
 "problems": [{"kind": "WORKER_CRASH", "detail": "ModuleNotFoundError: No module named 'playwright'"}]}
```

**`UNCERTAIN`, not `FAILED`, and no submission.** That degradation is correct. It is also not an
execution.

## 2. The second gap: the host cannot reach the database either

`tools/browser_worker_runner.py` was written to claim the same leased jobs host-side. It cannot: the
deployment does not publish Postgres to the host.

```
ports:
  # The only published port in this file, taken from ops/ports.yaml
  # BOUND TO LOOPBACK, and that matters on a public host.
```

**That restriction is correct and should not be relaxed** to make a runner convenient. A database port
on the host is a larger exposure than the problem it solves.

So the runner as committed **cannot run in this deployment**, and this document says so rather than
leaving a component that looks deployed. Its bounds — one session, never submits, lease required — are
tested and remain the right bounds; only its channel is wrong.

## 2b. THE BRIDGE IS BUILT AND PROVEN — the chain runs

The corrected design below was implemented as `tools/browser_execute_server.py` (host) and
`tools/browser_execute_client.py` (container), and the full chain now runs:

```
container -> client -> host service -> real Chromium -> real portal
  client exit: 0   elapsed: 7.9s
  status: BLOCKED   outcome_certain: True
  provider: playwright-chromium   sandbox: enabled   receipt: None
  "the page requires email and Granada holds no verified value"
```

**Read the last line carefully.** The executor reached the sign-in page and **refused to fill it in**,
because Granada holds no *verified* email for that organisation. It did not invent one. That is
`perception.FACT_CHANNELS` doing its job at the far end of a chain that crosses a container boundary, a
network hop and a subprocess — and it is the single most reassuring result in this document.

Obstacles cleared to get there, each measured rather than assumed:

| obstacle | what it was | resolution |
|---|---|---|
| Chromium not found | host service needed the interpreter that has Playwright | `--interpreter /tmp/browser-eval/bin/python3` |
| UFW `INPUT policy DROP` | the container could not reach the host on 8765 | `ufw allow in on br-d10cf7c8838c to any port 8765` — **bridge interface only**, never the public one |
| SSRF guard fired | loopback target refused, correctly | the controlled fixture sets `allow_loopback: true` |
| **the opt-in was discarded in transit** | `browser_worker.py` built `ActionScope` without passing `allow_loopback`, so the refusal named a switch the transport dropped | now passed through; still defaulting to `False` |

That fourth row is worth keeping: **the error message told the operator to opt in, and the code made
opting in impossible.** A refusal that names an unavailable remedy is worse than a refusal that names
none, because it sends someone to look for a configuration that does not exist.

`browser_invocation.py` needed **no change at all** — `browser_worker_command` points at the client,
which satisfies the invoker's stdin/stdout contract while forwarding the work to the host.

## 3. The corrected design

**Invert it: the host runner should be a stateless execution service with NO database credentials.**

```
container (has DB access, has the lease, has the package)
    │  POST /execute   {task: {...}}          loopback only
    ▼
host runner (has Chromium, has Playwright, NO database)
    │  spawns one browser_worker.py process
    └─ returns the worker's report verbatim
```

Why this is strictly better than the committed version:

* **The host holds no database credentials at all.** §12 asks that a browser worker not receive
  unrestricted tenant access; a host process with no database connection is the strongest form of that.
* **The lease stays where it already is.** `JobLedger.claim`, `job_attempts`, the dispatcher and RLS all
  remain in the container, so nothing about the existing job system changes.
* **The runner stays a transport.** It still plans nothing and decides nothing — it now also *stores*
  nothing, which is less privilege, not more.
* **Reachable over the Docker bridge gateway**, which is already the container's route to the host, with
  the listener bound to that interface only — never a public address, and never a published port on the
  host's public interface.

## 4. What must be true before a controlled internal browser job runs

1. The host runner serves `POST /execute` on the docker bridge address, **loopback/bridge-only**, and
   refuses anything else.
2. `browser_worker_command` on the agent points at a **container-side** client that calls that
   endpoint — not at a host path, which is what an operator would naturally try and which cannot work.
3. `browser_execution_enabled` is set **true** on one agent, for one controlled job.
4. `submission_authorised` remains **false** everywhere. External submission stays disabled.
5. The run is observed end to end: a real Chromium on a real portal, the report recorded against the
   real `job_attempts` row.

Until 1–5 hold, the honest status is: **the executor works and is not yet driven by the job system.**

## 5. Security posture of what exists today

| requirement | state |
|---|---|
| sandbox enabled | **yes** — `"sandbox": "enabled"` in every worker report; never disabled |
| SSRF / private-network refusal | enforced in `browser_boundary.ActionScope` and tested |
| tenant isolation | `validate_task` refuses another organisation's document before launch |
| submission authority | separate record; `submission_authorised=False` at every call site |
| secrets to the model | none; the task carries values, never credentials |
| prompt injection | page text is `untrusted_text`, never instruction; tested |
| credential storage | Fernet-encrypted, `granada_fleet` denied by RESTRICTIVE policy |
| ADR-0011 | resolved — `granada_fleet` is `NOBYPASSRLS`, bound per tenant, equivalence proven |

## 6. Resource figures

```
machine: 7.75 GB total · 6.6 GB available · 4 cores · 84 GB free disk

Stagehand:   launch 5.1s · RSS 1,200.7 MB · 0 MB leaked
Browser-Use: launch 5.6s · RSS 1,485.0 MB · 0 MB leaked
```

Blank page. A loaded grant portal uses more, by an unmeasured amount — the loaded-page harness hung and
was killed at 200 s, and that gap is stated rather than filled with an estimate.

Safe concurrency by memory: **3** (6,922 MB available, ~1 GB spared, 1,485 MB per session).
Actual operating limit: **1** — the ceiling is credential blast radius, not RAM.
