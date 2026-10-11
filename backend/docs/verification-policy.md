# Verification policy

Replaces the previous blanket "do not handle CAPTCHA or automated verification" clause, which was
too broad to be useful and conflated two different things: **completing a verification step on an
account Granada is authorised to manage**, and **defeating an access control on a system that is not
ours**. The first is ordinary automation. The second is not.

This document is the authority. Where an older spec says "do not bypass CAPTCHA", this is more
precise and supersedes it.

## The distinction that matters

| | |
|---|---|
| **Authorised verification** | Granada proves it controls the mailbox/identity it is acting for. The organisation owns the account and the address. |
| **Circumvention** | Granada defeats a control designed to establish that a *human* is present, or that the *identity* is genuine. |

Only the second is refused, and it is refused narrowly.

## Tier 1 — automated, unattended

| Challenge | Handling |
|---|---|
| Verification **email** — code or confirmation link | Granada reads the NGO's own mailbox, extracts the code/link, completes the step. **This is the main gate on NGO portals.** |
| Verification **link** with a token | Followed and the resulting page recorded. |
| **Session preservation** | Cookies/profile reused per organisation so a verified session is not re-established on every step. Never share a profile across tenants. |
| **reCAPTCHA v3 / risk-scored, non-interactive** | Nothing to solve. Granada proceeds and records the score if exposed. |
| **A checkbox CAPTCHA that passes on a legitimate browser** | Clicked once. If it presents an image challenge instead, this becomes Tier 3 — it is not retried in a loop. |
| **Simple non-adversarial questions** (arithmetic, "what is 3+4", a static word) | Answered. This is not meaningful bot protection and is not treated as a security boundary. |
| **Transient/challenge-free waiting** (rate limit, "try again shortly") | Backed off and retried within budget. |

## Tier 2 — automated, with a bounded wait

| Challenge | Handling |
|---|---|
| Email delivery delay | Poll the mailbox with bounded backoff until the message arrives or the budget expires. |
| Challenge that clears after a page reload / settle | One retry, not a loop. |

## Tier 3 — human, by design

Granada **pauses, keeps the browser session alive, and hands off**. It does not fail terminally and
does not lose the session — a gate it cannot pass must be resumable, not a dead end.

| Challenge | Why |
|---|---|
| **Image-grid / audio CAPTCHA** (reCAPTCHA v2 challenge, hCaptcha), and any challenge served by a third-party solving service | This exists specifically to establish a human is present. Paying humans or farms to defeat it is circumvention. |
| **MFA that requires a device we do not control** (authenticator app, hardware key) | The factor proves possession of something Granada does not have. |
| **Identity proof** — ID document, selfie, liveness | Proves a *person*. |
| **Legal declarations, terms acceptance, e-signature** | Creates liability for the entity. |
| **Payment / application fee** | Spends money. |

**SMS OTP is deliberately not in Granada's scope as an interception capability.** Where an
organisation wants codes automated, it forwards its own SMS to its own mailbox through
infrastructure it already controls (carrier or gateway forwarding), and Granada reads the *email*
under Tier 1. Granada does not read a device's messages: that capability is the account-takeover
primitive, and it is not needed for the forwarding path to work.

## Required behaviour

1. **Classify before failing.** Every challenge is identified and recorded by type. `BLOCKED` on an
   unclassified challenge is a defect: the operator cannot act on "it stopped".
2. **Never assume all challenges are solvable, and never assume all need a human.** Both errors are
   equally costly: the first wastes a run, the second wastes the operator.
3. **A Tier 3 gate is resumable.** The worker parks holding the live session and records the exact
   step; the operator clears it and the run continues. Losing the session on handoff is a defect.
4. **Record which tier each challenge fell into**, per portal. This produces the unattended
   completion rate, which is the honest metric for this capability.
5. **Evidence of completion is retained** — the page state after verification, with the reference
   stored through the existing evidence store.
6. **No Tier 3 bypass, ever, by any route**, including a "solving service" integration, a
   human-click farm, or a rented phone number.

## The metric to report

Per portal: `unattended_completions / attempts`, and for each failure the **tier and challenge type**
that stopped it. That number is the deliverable — not a claim that everything is solvable.
