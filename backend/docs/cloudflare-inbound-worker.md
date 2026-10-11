# Cloudflare Email Worker — inbound mail for granadac.com

The Worker is a **reporter**. It holds no tenant knowledge, no model key and no database credential;
it forwards the message to Granada and obeys the answer. That is deliberate: the sandboxed edge is
the least trusted part of the chain, so it decides nothing.

## The script

```javascript
export default {
  async email(message, env, ctx) {
    // Bounded before it leaves the edge: a huge message must not be able to exhaust the API.
    const MAX = 200_000;
    let text = "";
    try {
      text = await new Response(message.raw).text();
    } catch (e) {
      text = "";
    }
    if (text.length > MAX) {
      text = text.slice(0, MAX);
    }

    const payload = {
      to: message.to,
      sender: message.from,
      subject: message.headers.get("subject") || "",
      text: text,
      html: "",
      message_id: message.headers.get("message-id") || "",
      received_at: new Date().toISOString(),
    };

    let body;
    try {
      const resp = await fetch(env.GRANADA_INGEST_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Ingest-Key": env.GRANADA_INGEST_KEY,
        },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) {
        // 4xx is our own refusal to authenticate or our own bad request: retrying will not help.
        // 5xx is Granada being unwell: retry.
        if (resp.status >= 500) return;   // let Cloudflare retry
        message.setReject("Delivery refused");
        return;
      }
      body = await resp.json();
    } catch (e) {
      // Network failure talking to Granada. NOT a rejection: bouncing a funder's message because
      // our API was briefly unreachable is the worst outcome available.
      return;   // let Cloudflare retry
    }

    if (body.action === "reject") {
      // A real bounce, so a sender who mistyped finds out instead of being silently dropped.
      message.setReject(body.reason || "No such mailbox");
      return;
    }
    if (body.action === "retry") {
      // Granada could not store it. Let Cloudflare retry rather than losing the message.
      return;
    }
    // action === "accept": stored, and offered to any verification request that matched.
  },
};
```

## Setup

1. **Workers & Pages → Create → Worker.** Paste the script above. Give it a name like
   `granada-mail-ingest`.
2. **Settings → Variables and Secrets.** Add two:
   - `GRANADA_INGEST_URL` = `https://granadac.com/api/v1/internal/mail/ingest`
   - `GRANADA_INGEST_KEY` = **the value of `MAIL_INGEST_KEY` in `~/granada/.env` on the VPS**.
     It is already generated. Retrieve it with:
     ```
     ssh granada-vps "grep '^MAIL_INGEST_KEY=' ~/granada/.env | cut -d= -f2"
     ```
     Mark it a **Secret**, not a plaintext variable.
3. **Save and Deploy.**
4. **Email Routing → Routes → catch-all.** Edit the existing (currently **disabled, action Drop**)
   catch-all: set the action to **Send to a Worker**, choose `granada-mail-ingest`, and enable it.
5. **Leave `admin@granadac.com` exactly as it is** — it forwards to `gemtech2050@gmail.com`, and
   specific rules are evaluated before the catch-all.

## What you get

- **No per-NGO mailbox provisioning anywhere.** Every `@granadac.com` address arrives at this one
  Worker. Thousands of NGOs are just rows in `mail_routing`.
- **Unknown or disabled addresses bounce** rather than being dropped, so a mistyping sender finds out.
- **Transient Granada failures retry** rather than bouncing — the message is not lost.

## Verify before enabling the catch-all

```bash
K=$(ssh granada-vps "grep '^MAIL_INGEST_KEY=' ~/granada/.env | cut -d= -f2")
# Unauthenticated must be refused
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://granadac.com/api/v1/internal/mail/ingest \
  -H 'Content-Type: application/json' -d '{"to":"x@granadac.com"}'          # expect 401
# Authenticated, foreign domain must be rejected
curl -s -X POST https://granadac.com/api/v1/internal/mail/ingest \
  -H "X-Ingest-Key: $K" -H 'Content-Type: application/json' \
  -d '{"to":"a@example.org"}'                                               # expect action=reject
```

## Upgrade note

Migration `023_mail_routing` backfills the routing table, but **it cannot read `mail_identities`** —
that table has FORCE ROW LEVEL SECURITY owned by `granada_user`, so the migration sees zero rows and
the backfill silently inserts nothing. New identities are covered by a trigger, so only pre-existing
rows need this, and it must be run once as a superuser:

```bash
ssh granada-vps "cd ~/granada && sudo docker compose exec -T postgres psql -U postgres -d granada_auth -c \"
INSERT INTO mail_routing (address, org_id, identity_id, is_active)
SELECT lower(address), org_id, id, (upper(coalesce(status,'')) = 'ACTIVE')
FROM mail_identities
ON CONFLICT (address) DO UPDATE SET org_id=EXCLUDED.org_id,
  identity_id=EXCLUDED.identity_id, is_active=EXCLUDED.is_active;\""
```
