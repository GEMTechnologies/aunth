# ADR-0011 — restrictions required before a live browser worker receives tenant credentials

§12 asks for this document in the case where the redesign cannot be safely completed in the phase.
It cannot, and the reason is concrete rather than a matter of time: **the credential path is not
merely unwired, it is undecided.** This records what is measured, what the disagreement is, and the
conditions that must hold before a real organisation's credential reaches a browser.

## What is measured

- `WorkflowEngine._handle_browser_task` calls `browser_boundary.build_task(db, package,
  action_scope=..., form_data=...)` **without `credentials`**, so `task.credentials` is always `[]`.
  The browser can fill a form field; it cannot authenticate.
- `credential_secrets` contains **0 rows**. There is nothing to send even if it were wired.
- `has_table_privilege('granada_fleet','credential_secrets','SELECT') = true`. Migration
  `020_credential_deny_fleet` installs a **restrictive policy**, not a table-privilege denial; its
  row-level effect was *not* re-verified this round and must be before it is relied on.
- The worker runs **on the host, in a separate process tree with no database access**. It reads its
  input as JSON on stdin and prints a report on stdout.
- `browser_worker` expects the credential **as a value**: it calls
  `provider.authenticate(username=creds_user, password=creds_pass)`.
- `BrowserTask.credentials` is a list of `CredentialRef` — and its own comment says this is deliberate:
  "a password belongs in neither" the task nor the persisted record.

## The unresolved disagreement

Those last two facts do not fit together, and no amount of wiring resolves it:

- The task carries a **reference**, because persisting or logging a secret is unacceptable.
- The worker needs a **value**, because it types it into a login form.

Something must hold the plaintext between them, and each candidate widens exposure somewhere:

| Where the secret is decrypted | Consequence |
|---|---|
| Executor, then over the bridge to the worker | Plaintext traverses HTTP on the docker bridge. Today that bridge is plain HTTP. |
| Worker, from a scoped short-lived token minted by the executor | Needs a token exchange that does not yet exist; smallest plaintext lifetime. |
| Worker, with its own scoped DB read | Gives the host process database access, which is exactly what ADR-0011 removed. |

Choosing is a security decision, not an implementation detail, and it is the reason this document
exists rather than a commit.

## Conditions that must hold first

1. **A chosen decryption point**, with the transport consequence stated and accepted in writing.
2. **The bridge is not adequate for a secret as it stands.** `172.18.0.1:8765` is plain HTTP with a
   bridge-scoped UFW rule. Either TLS, a unix socket, or the short-lived-token design above.
3. **`credential_secrets` holds a real row, and the restrictive policy is verified to bite.** A
   policy that has never had a row to filter has never been tested.
4. **Resolution is org-scoped twice over** — the `org_id` filter *and* RLS — matching
   `credential_store._row`, which does both on purpose.
5. **A task can never name another organisation's credential.** Same rule `validate_task` already
   enforces for documents, extended to `CredentialRef`.
6. **The scrubber is proven before the first credential flows**, not after. `browser_worker` already
   scrubs before printing, and records a failure rather than printing unscubbed output — that
   behaviour needs a test with a real login before it guards anything.
7. **Profile isolation and expiry.** Chromium profiles and cookies are per-organisation and removed
   on completion or expiry; a leaked profile is a leaked session.
8. **No debugging endpoint.** `--remote-debugging-port` must remain absent; the sandbox must stay
   enabled. Neither is negotiable to make an install easier.
9. **A blank `CREDENTIAL_ENCRYPTION_KEY` must fail closed**, not fall back.

## Until then

`submission_authorised=False` is passed at every call site and no credential is supplied, so the
executor reaches a login page and stops. That is the intended resting state for this phase: the
capability is built, demonstrated on a controlled portal, and cannot yet touch a real organisation's
account.
