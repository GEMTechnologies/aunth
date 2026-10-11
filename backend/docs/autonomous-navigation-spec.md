# Autonomous portal navigation — implementation spec

Status: **NOT IMPLEMENTED.** This is the locked design for the work, written after measuring the
existing chain. Nothing here is claimed as working.

## The defect, measured

`browser_worker` is a **form-fill executor**, not a navigation agent.

- It reads `values` from `payload["form_data"]` and fills fields. With `form_data = {}` it performs
  no steps and reports `NOTHING_ACHIEVED` — correctly, and honestly.
- There is **no task element meaning "click the control labelled X"**, so it cannot express
  navigation.

**A render-timing hypothesis was tested and disproved.** Against
`https://inspira.un.org/psp/PUNA1J/?cmd=login&languageCd=ENG`:

```
domcontentloaded : readyState interactive, title "Inspira", bodyTextLen 310,
                   inputs 11, buttons 2, links 6, forms 1
+3s / +5s / +7s / +10s / networkidle : IDENTICAL
```

The page is fully populated immediately and never changes. A render-aware wait would fix nothing.
Do not add one expecting it to help.

## What already works (do not rebuild)

- Host execution service `172.18.0.1:8765`, `{"ok": true, "worker": true, "concurrency": 1}`.
- Real Chromium; a real click on Inspira's "Create account here" navigated to
  `.../psc/REGISTER/EMPLOYEE/HRMS/c/UN_REG_MENU_FL.UN_HRS_EXT_USER_FL.GBL?...`, title
  "External User Registration". Screenshots captured.
- `BrowserRuntime.run(task, skip_launch=..., values=..., uploads=..., declaration_authorised=...,
  org_document_ids=..., submission_authorised=False)` — the existing planner, already page-driven.
- `agent/evidence_store.py` (`open_store(org_id, run_id)`) for screenshots/evidence.
- `PlaywrightProvider` with `goto`, `authenticate`, and `_evidence`.
- Decision layer: `DecisionGateway.decide(request: DecisionRequest, *, action=None) -> DecisionResult`.
- Model path verified configured: `model_provider=openai_compatible`, `model_base_url=https://api.deepseek.com`,
  `model_classification_model=deepseek-flash`, API key present.

## Design

### 1. Task model (`agent/browser_boundary.py`)

Add `steps: tuple[BrowserStep, ...] = ()` to `BrowserTask`, and a typed `BrowserStep`:

```
op: navigate | observe | click | fill | select | wait | extract | stop
by: text | role | label | id | css        (for click/fill/select)
value: str
url: str                                   (navigate)
condition: present | absent | text | url_contains  (wait)
reason: str                                (stop)
```

`as_dict()` must emit `steps` (the worker reads the serialised form — the same seam where
`target_url` and `allow_loopback` were silently dropped; see
`test_browser_path_reachability.py::test_the_scope_ROUND_TRIPS_through_serialisation`).
Reject unknown `op` with `BrowserTaskRefused`. **`steps=()` must reproduce today's form-fill
behaviour byte-for-byte** — that is the compatibility contract.

### 2. Goal-driven planning (`browser_worker.py` + `BrowserRuntime`)

Add `goal: Optional[str]` to the task. When a goal is present and `steps` is empty, run a bounded
loop in `BrowserRuntime`:

```
observation = observe(page)                     # url, title, controls[], forms[]
if goal_satisfied(observation): stop
action = plan(observation, goal, history)       # DecisionGateway, not a hardcoded script
verify(executed)                                # did the page actually change?
record(...)                                     # evidence
```

- `plan()` builds a `DecisionRequest(decision_type=..., questions=(DecisionQuestion(...),), state=...)`
  — see the real dataclass fields in `agent/decision/models.py`. Do **not** pass `question=`/`options=`.
- The planner must offer the model the **observed controls** and require it to choose one by index or
  by visible text. Never a guessed CSS selector.
- Bounds: `max_steps` (reuse `ActionScope.max_steps`, default 200 is too high — use ~12),
  `max_navigation_depth`, and a wall-clock deadline.
- Classifications: `EMPTY_DOM`, `RENDER_TIMEOUT`, `OBSERVE_FAILED`, `NO_ACTION_SELECTED`,
  `ACCESS_DENIED`, `EXECUTION_FAILED`, `GOAL_ACHIEVED`, `BLOCKED_NEEDS_HUMAN`.
- A step that does not change the page twice in a row must stop with `NO_ACTION_SELECTED` rather than
  retrying — the bounded-recovery rule.

### 3. Safety (extend, do not weaken)

- `validate_task` already screens hosts via `target_guard`; **re-validate the destination after every
  navigation and every redirect** — the allowlist is exact-match, so a redirect to another host must
  be refused.
- `click` may never target a control whose text matches submit/register/apply/agree/declare/pay
  unless `submission_authorised=True`. Today that flag is hard-coded `False` at the call site.
- No CAPTCHA/access-control bypass. Stop with `BLOCKED_NEEDS_HUMAN`.
- Never invent values: `form_data` remains `_verified_form_data(package)` — verified answers only.

### 4. Evidence and honesty

One durable record per attempted action: url, page title, observed controls, chosen action +
rationale, execution result, destination url, screenshot ref, classification. The existing
`EvidenceStore` and `agent_activity` are the homes.

**A job must not report success because the process exited.** `NOTHING_ACHIEVED` already enforces
this for the zero-step case; the planner must report `GOAL_ACHIEVED` only on a verified goal
predicate, and `BLOCKED` otherwise.

## Proof required

Through the **job system**, not a direct probe:

1. Inspira, goal *"Determine whether this portal supports organisation registration."* — must
   observe, find the registration control, click it, read the fields, and conclude. Live evidence
   already shows the answer is **no** (fields: `FIRSTNAME`, `UN_PHPRD_WRK_MIDDLE_NAME`, `LASTNAME`,
   `HRS_APPL_WRK_EMAILID`, `HRS_APPL_WRK_BIRTHDATE`, `UN_PASSWORD_1`; no organisation field).
2. `https://www.unpartnerportal.org/landing/register/`, goal *"Determine how gloabmotion can register
   as an NGO… stop before submitting."*

Acceptance needs ≥2 verified navigation actions chosen from live observation, screenshots, action
traces, a safe halt, existing tests green, six services healthy, and browser execution **disabled by
default** afterwards.

## Known traps (learned the hard way — do not rediscover)

- `MailSendIntent`-style naming: check attribute names, do not assume (`status`, not `state`).
- Raw `set_config('app.current_org_id', ...)` does **not** survive a commit. Use
  `tenant_context.set_tenant(db, org_id, user_id)`, which re-binds after every transaction.
- `fleet_active_agent_ids()` is granted to `granada_fleet` only.
- The container has **no bind mounts**; deploy = host source → `docker compose build` → `up -d`.
  Use `docker compose cp` to run a probe.
- PowerShell mangles `-m`, `||`, `$()`, nested quotes, `count(*)`. Pipe base64 + `bash -s`.
- The `granada_fleet` role has no DELETE on `submission_packages`; fixture cleanup needs the superuser.
