# Deploying the browser execution work — and the false pass that hid it

**Status:** deployment NOT done. Production runs code from the beginning of this directive.
**Date:** 2026-10-10
**Scope:** how to deploy the browser execution milestone, and how the check that said it was
already deployed reported success while testing nothing.

## What is actually deployed, verified

```
VPS ~/granada      branch master, HEAD 24f7270
                   2026-10-09 "chore: advance the Auth gitlink for the browser execution boundary"

backend/agent/target_guard.py    ABSENT
backend/agent/launch_guard.py    ABSENT
backend/agent/output_scrub.py    ABSENT
/app/backend/agent/browser_runtime.py   @runtime_checkable: 0
```

`24f7270` is the outer commit from **the start** of this directive. Everything built since - the
runtime, the boundary integration, the privilege guard, the SSRF guard, the launch guard, the
credential scrubber, the single exit - is committed on `agentic-v2` and **has never been deployed.**

This is not a defect in the code. It is the difference between "the tests pass" and "the change is
live", and §14 states it explicitly: *verify the actual served code rather than relying only on
successful build logs.*

## The false pass, and why it happened

The first verification hashed five files in the container and in the checkout:

```
for f in agent/target_guard.py ...; do
  c=$(docker compose exec -T api sha256sum /app/backend/$f | cut -c1-12)
  r=$(sha256sum backend/$f | cut -c1-12)
  [ "$c" = "$r" ] && echo "MATCH $f" || echo "DIFFER $f"
done

  MATCH   agent/target_guard.py
  MATCH   agent/launch_guard.py
  MATCH   agent/output_scrub.py
  MATCH   agent/worker_privileges.py
  MATCH   tools/browser_worker.py
```

**All five files are absent from both places.** `sha256sum` of a missing file writes nothing to
stdout, so `$c` and `$r` were both the empty string - and **two failures compared equal.**

Nothing was checked. The output looked like a clean bill of health.

This is the fifth time in this directive that a verification matched the wrong thing and reported
success, and the first time it was a DEPLOYMENT check - where the consequence is believing a security
fix is protecting production when it is not on the host at all.

## The corrected check

Assert the file EXISTS first. A hash comparison must never be able to compare two absences:

```bash
# 1. the file must exist on both sides, or the comparison is meaningless
docker compose exec -T api test -f "/app/backend/$f" || { echo "ABSENT in container: $f"; continue; }
test -f "backend/$f" || { echo "ABSENT in checkout: $f"; continue; }

# 2. then compare, and compare the FULL digest - a 12-character prefix is a truncation that can only
#    ever make two different files look more alike
[ "$(docker compose exec -T api sha256sum "/app/backend/$f" | cut -d' ' -f1)" \
  = "$(sha256sum "backend/$f" | cut -d' ' -f1)" ] && echo "MATCH $f" || echo "DIFFER $f"
```

The same shape applies to the module check: `grep -c` returning **0** is the signal, and it must never
be silently treated as "not applicable". `runtime_checkable: 0` was the one honest number in the whole
verification, and it was the one that contradicted the others.

## What a correct deployment needs

Not done here, and deliberately not started: it is a production change on a host that also serves
another application, and a half-finished one is worse than a documented one.

1. **Fetch `agentic-v2` on the VPS.** `~/granada` is the OUTER repository on `master`; the work is on
   `agentic-v2` in the nested `Auth` repository, whose gitlink the outer commit pins. Both must move.
2. **Confirm the gitlink.** The outer commit's `Auth` pointer must name the inner commit that
   contains the new modules - otherwise the checkout has the outer change and the old inner tree,
   which looks like a successful fetch.
3. **Rebuild the images.** The code is BAKED, not mounted - `docker inspect` showed no mounts for
   `api` - so a restart without a rebuild changes nothing.
4. **Restart, then verify health** for all six services.
5. **Re-run the corrected check above**, and require `@runtime_checkable: 1` in the container.
6. **Confirm `browser_execution` is still off.** The flag is what keeps every one of these paths
   unreachable, and it must be verified AFTER the restart rather than assumed to have survived it.
7. **Confirm external submission is still disabled** and `MODE_HANDOFF` unchanged.

## Why the flag being off is doing real work

Every guard described in this document - the privilege refusal, the SSRF screen, the launch-argument
check, the credential scrubber - currently protects nothing in production, because none of it is on
the host. What protects production is that `browser_execution_enabled` is 0, so the browser path is
never entered.

That is a real safeguard and it is not the same as the guards being deployed. Conflating the two is
how a fleet ends up believing it is defended by code it has not shipped.
