# Granada email system — technical handover

**Purpose:** continue this work in a fresh session without repeating completed investigation.
**No secrets are recorded here, by design.** Credentials live only in `~/granada/.env` (mode 600).

---

## 1. Current state

| Item | Value |
|---|---|
| Inner repo (`Auth`) | branch `agentic-v2`, clean, in sync with `origin`, HEAD `17d59e2` |
| VPS deployment | `~/granada/Auth` at the same commit; six services healthy |
| Alembic head | `020_credential_deny_fleet` |
| `.env` | SES SMTP config written (mode 600); `AUTONOMOUS_MAIL_ENABLED=false` |
| Catch-all (Cloudflare) | **disabled, action DROP** — must stay so until inbound is proven |
| `admin@granadac.com` | ACTIVE → forwards to `gemtech2050@gmail.com` — **preserve** |

## 2. What is verified, with evidence

### SES outbound transport — WORKING

- `build_from_settings(settings)` returns `SmtpOutboundMailProvider` (it returned `None` before, which
  is why *no* mail could leave Granada by any route: the factory treats `localhost` as "unset").
- SMTP `LOGIN: OK` against `email-smtp.us-east-1.amazonaws.com:587` with STARTTLS.
- Config flows through compose interpolation (`SMTP_HOST: ${SMTP_HOST:-localhost}`), verified inside
  the running container, not just in the file.

### The application send path — PROVEN AS FAR AS THE AUTHORITY GATE

Driven in-process through `SendService` under the **application** role with RLS enforced:

```
outbound : SmtpOutboundMailProvider
identity : onboarding-test@granadac.com
draft    : READY
intent   : created, status=WAITING_FOR_APPROVAL, risk_class=ROUTINE
execute  : refused_code=NOT_APPROVED  ("no live approval for this intent")
messages : 0
```

**This is correct behaviour, not a failure.** `SendService.execute_send` runs
`_final_authority_check`, and an intent with no live approval is refused. Do **not** bypass it to
make a test pass. The remaining step for a real delivery is to approve the intent legitimately.

### The mail platform is already large — reuse it

`agent/mail/` is ~500 KB across 26 modules: `service.py`, `send_service.py`, `gateway.py`,
`classification.py`, `correlation.py`, `approval.py`, `autonomy.py`, `risk.py`, `security.py`,
`scanning.py`, `ceiling.py`, `fingerprint.py`, `outbound.py`, `vocabulary.py`.

Providers: `smtp.py`, `imap.py`, `google.py`, `microsoft.py`, `http.py`, `fake.py`,
`fake_outbound.py`, `base.py`.

Models: `MailIdentity` (`UNIQUE(address)`, `UNIQUE(token)`, types `MANAGED`/`CONNECTED`/`REPLY_ALIAS`),
`MailAccount`, `MailMessage`, `MailThread`, `MailAttachment`, `MailDraft`, `MailSendIntent`,
`MailSendAttempt`, `MailProviderEvent`, `MailDeadline`, `MailClassification`, `MailApproval`.

Tests already exist: `test_mail.py`, `test_mail_outbound.py`.

## 3. Architecture facts learned the hard way

**The send path is THREAD-FIRST, not draft-first.**
`MailDraft` has **no** `to_addresses` and **no** `from_address`. Columns:
`id, org_id, agent_id, thread_id, application_id, reply_to_message_id, version, subject, body,
status, status_reason, model_invocation_id, prompt_version, facts_used, documents_used,
organisation_profile_version, application_version, research_version, created_at, approved_at,
approved_by, sent_at, supersedes_id, edit_source, edited_by, edited_at`.
The recipient lives on `MailThread`; `from_address` is a `create_send_intent` argument.

`MailDraft` statuses: `GENERATING, READY, NEEDS_DATA, NEEDS_REVIEW, APPROVED, SUPERSEDED, SENT`
(there is no `DRAFT`).

`MailSendIntent` has `status`, not `state`.

The proven construction is in `tests/test_mail_outbound.py` — `_draft()`, `_identity()`, `_intent()`
around lines 99–167. Copy it; do not re-derive it.

**`create_send_intent` / `execute_send` are the application's sending service.** There is no HTTP
route that creates drafts or intents; the routes are approve/reject/request-changes/cancel/reconcile
on `/mail/send-intents/{id}/...`. Approval requires a member whose role grants `mail.approve_send`.

## 4. Environment traps that cost time here

- **RLS binding is dropped by every commit.** `set_config('app.current_org_id', ..., false)` is
  session-scoped, but the pool runs `ResetStyle.reset_rollback` on return. Measured: bound → rows
  visible; after `commit()` → 0. Re-bind after every commit, and read attributes **before**
  committing (`expire_on_commit` raises `ObjectDeletedError` for a row that wrote fine).
- **`fleet_active_agent_ids()` is granted to `granada_fleet` only.** Tenant work runs as
  `granada_app`; supply the org id rather than enumerating.
- Org id is `3d4edeec-f4dc-4e5f-a99d-29100780b3f2`; agent `872c92ce-0e97-484a-a5ca-3854bd36d380`.
- The container has **no bind mounts** — `/app/backend` is baked into the image. Deploy is
  host source → `docker compose build` → `up -d`. Use `docker compose cp` to run a script.
- PowerShell mangles `-m`, `||`, `$()`, nested quotes and `count(*)`. Pipe base64 + `bash -s`.

## 5. Remaining work

**P1 — finish outbound.** Approve the pending intent via a member with `mail.approve_send`, then
`execute_send`, and confirm external delivery to `gemtech2050@gmail.com` **plus** a `MailMessage`
Sent copy and the correct sender identity. `EMAIL_FROM=noreply@granadac.com` is the default system
sender only; NGO correspondence must use that NGO's assigned address.

**P2 — inbound.** Build `POST /api/v1/mail/ingest/cloudflare` and the Email Worker.
Reuse `MailService.ingest_webhook` / `_ingest_from_sync` — *"the same pipeline as a webhook"*, per its
docstring, so dedupe/threading/classification/attachments are not re-implemented.
Note the design tension: the service **refetches** from the provider rather than trusting the webhook
body, but Cloudflare **pushes**. A Cloudflare adapter must implement `MailTransport.fetch_message()`
against a bounded, single-use handoff buffer.
Required: secret auth, recipient validation, unknown → **Cloudflare reject** (not bare 422),
disabled → rejected, transient backend failure handled separately so legitimate mail is not lost,
dedupe, attachments, threading.

**P3 — provisioning.** Friendly unique `@granadac.com` per NGO at approved onboarding; reserved
words; thousands of NGOs with no per-mailbox Cloudflare/SES work (catch-all makes this a DB row).
Keep per-application reply aliases random (`MailIdentity.token`).

**P4 — mailbox UI.** Frontend has **no mail UI at all** (pages: Auth, Login, Organization, Profile,
Security, Agent). Needs Inbox/Sent/Drafts/Archive/Compose/Reply/Search/Attachments on the existing
design system, wired to real services.

**P5 — production.** SES `ses:FromAddress` restriction must be an explicit **Deny** (IAM is additive;
adding an allow-policy will not restrict a shared group's broad grant — decision 7). This needs AWS
API access, which SMTP credentials cannot provide. Quotas (SES capacity is **shared with PARJ**),
bounce/delivery events, queues, retries, isolation tests, PARJ unaffected.

## 6. Blockers needing the operator

1. **`mail.approve_send` holder** to approve send intents legitimately (or confirm the existing
   member's role should have it).
2. **`MAIL_INGEST_KEY`** placed in `~/granada/.env` by the operator, so it never transits chat:
   `ssh granada-vps 'printf "\nMAIL_INGEST_KEY=%s\n" "VALUE" >> ~/granada/.env && chmod 600 ~/granada/.env'`
3. **AWS IAM access** to apply and verify the `ses:FromAddress` deny.
4. **Cloudflare catch-all** flip to *Send to a Worker* + enable — **only after** P2 tests pass.
5. A decision on unknown-recipient policy: reject at Cloudflare (preferred) vs quarantine.

## 7. Do not

- Do not enable catch-all before inbound tests pass; it currently `DROP`s, which is a safe failure.
- Do not touch PARJ's IAM user, SES config, or port 8000.
- Do not bypass approval or tenant isolation to make a test pass.
- Do not commit secrets; do not print them.
- Do not re-investigate what section 2 and 3 already establish.
