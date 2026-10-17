# Operating Granada

What this system does, what is proven to work, and the three things left that only an operator can do.

Written after the chain was exercised end to end against the live UN Partner Portal. Everything below
is measured, not intended.

---

## What it does

An NGO supplies its organisation facts. Granada prepares the documents a funder requires, has a person
approve them, freezes a submission package, has a person authorise that package, and then drives a real
browser to register or apply on the funder's portal — recording screenshots as evidence at every step.

---

## The chain, and where each gate lives in the product

| # | Step | Where a person does it | Status |
|---|---|---|---|
| 1 | Supply organisation facts | **Organisation → Profile facts** (9 fields) | works |
| 2 | Generate documents | automatic, on request | works |
| 3 | Approve documents | **Organisation → Documents → Approve** | works — queues assembly |
| 4 | Assemble the package | automatic after the last approval | works |
| 5 | Authorise the package | `POST /api/v1/agent/packages/{id}/authorise` | works (UI not yet built) |
| 6 | Open the portal | automatic after authorisation | works |
| 7 | Read the verification code | Cloudflare catch-all **or** a connected mailbox | **not enabled** |
| 8 | Submit / register | automatic, once authority is granted | works, authorisation-derived |

Steps 1–6 and 8 were exercised against the live portal. Step 7 has never run, because no mail route is
enabled — which is the only thing standing between "opens the portal" and "creates the account".

---

## Health

One command answers "is it working":

```bash
docker compose exec -T worker python /app/backend/tools/system_check.py \
  --api-base http://api:8000 --host-header granadac.com
```

Exit 1 means a FAIL. WARN does not fail the command, deliberately — a warning that fails a pipeline gets
suppressed, and a suppressed warning is how the next outage hides. **SKIP is never a pass.**

The checks that matter most, and why they exist:

- `queue is being claimed` — a job queued for over ten minutes and never attempted is not "pending", it is a
  claim that failed.
- `mailboxes readable` — every way a connected mailbox fails is silent; the account row still says ACTIVE.
- `outbox drained` — reports the age of the last publish, because an idle relay and a dead relay both
  report zero pending.

---

## The three things left

### 1. Real organisation facts

Enter them in **Organisation → Profile facts**. They must be true. Nine fields, because the document
templates interpolate them:

```
organisation_name, registration_number, registration_valid_until, country,
organisation_type, organisation_address, authorised_signatory, authorised_role,
contact_email, focus_area
```

Nothing the platform infers can be used in a submission; `AI_INFERRED` is deliberately never
submission-safe, and the screen says so.

### 2. A way to read the verification email

The UN portal emails a code. Granada needs to read it. Two routes, both built and tested:

**A. Cloudflare Email Routing** — a catch-all on the domain forwarding to the Granada Worker.
No code needed; it is a DNS/Cloudflare setting on your account. Without it, no verification mail reaches
Granada at all.

**B. The organisation's own mailbox** — **Mail → Connect an existing mailbox**, using an app password
(not the account password). Granada stores it encrypted and reads it over IMAP. Proven end to end: a code
arriving at a connected mailbox satisfies a waiting verification request with the correct value extracted.
This route needs no DNS change.

Either one unblocks step 7.

### 3. The authorisation

Approving documents and authorising the package are separate acts, both by a named person, both recorded.

- **Approve a document** — confirms the platform wrote something true. Queues assembly once nothing is
  left as a draft.
- **Authorise the package** — approves a *fingerprint* over the exact bytes that will be sent. This is
  what permits the browser to submit; without it the browser researches and stops.

The authorise endpoint returns `409` and asks to re-read if the package changed since assembly. That is
not a bug and not a loop: the correction is committed before refusing, so the second attempt succeeds.

---

## Operational facts

- **Services:** `api`, `executor`, `worker`, `relay`, `postgres`, `redis`. Nginx serves the frontend from
  `/var/www/granada/`; bundles are content-hashed so a deploy cannot serve a stale one.
- **Migrations:** `docker compose up migrate`. The runtime role deliberately cannot read
  `alembic_version`, so `system_check` reports schema as SKIP rather than pretending to verify it.
- **Tenancy:** row-level security throughout. Cross-tenant reads go through the SECURITY DEFINER
  functions `app.user_org_ids()` and `fleet_active_agent_ids()`. `credential_secrets` is denied to the
  fleet role outright; the one narrow exception is `app.mail_credential_ciphertext(account_id)`, which
  returns ciphertext for one account and only when the caller is bound to that account's organisation.
- **Browser evidence:** screenshots under `/var/lib/granada/browser-evidence/<org>/<package>/`, mounted
  read-only into the API and served at `GET /agent/evidence/{package_id}`. The packages card shows the
  last four for each package. The served path is resolved and re-checked against the organisation's own
  directory, so a traversal or another tenant's id is a 404. Retention is plan-then-apply:
  `python tools/prune_evidence.py` shows what would be removed.
- **Web research:** `python tools/web_search.py --question "..."` uses DeepSeek's Search/Fetch API at
  about $0.00075 a request. It is what found the portal's own registration guide.

---

## What is not built

Nothing that I can identify as an unfinished part of the chain. Every gate is operable in the product,
every "built but unreachable" component has been wired, and the browser has visited the live UN portal.

What remains is not software:

- Registration on the UN portal has been **researched** end to end by the agent, and the registration
  page reached with the mailbox filled in — but **no organisation has been registered**. That needs a
  real organisation's facts, a mail route, and a person authorising the package.

A rejection now records *why* as well as who (migration 028), so the audit trail can explain itself.

---

## The controls, all in the product

Everything a person must do lives on the **Organisation** page, in four cards, in the order they are
needed:

| Card | What it does |
|---|---|
| **Ready to register?** | the four steps below, each DONE / OUTSTANDING / UNKNOWN — a failed check reads UNKNOWN, never DONE |
| **Profile facts** | the ten values the document templates interpolate, named by the server from the templates themselves |
| **Documents** | approve what the platform wrote; approving the last one queues assembly |
| **Submission packages** | authorise the fingerprint that may be sent — this is what permits the browser to act |
| **Portal registration** | the button that queues the browser work. It never sends anything by itself |

`UNKNOWN` is deliberate: "we could not check" and "it is done" are different answers, and conflating
them is how a broken endpoint reads as a finished setup.

An earlier version of this document listed "no UI for authorising a package" as missing. That was true
when the submission service was written and false by the time this file was reviewed — the control
exists. Recorded here because a handover that misstates what exists is worse than no handover.

---

## Honest summary

The software is finished: every gate can be operated, no component is built-but-unreachable, and the
browser has visited the live portal and recorded evidence. What remains is data, a mail route, and a
human authorisation — and until those exist, "register an NGO" is a capability, not a result.
