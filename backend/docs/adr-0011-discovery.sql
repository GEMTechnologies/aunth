-- ADR-0011 narrowing, step 1: the discovery function.
--
-- Additive only. Creates one function and grants EXECUTE to one role. No table is altered, no policy
-- is touched, no row is migrated - which is what makes rollback a single DROP FUNCTION and means
-- nothing in the running system is forced to call this.
--
-- WHY A SECURITY DEFINER FUNCTION AND NOT A WIDER ROLE
--
-- Only DISCOVERY needs cross-tenant visibility: asking which work is due, across organisations.
-- Claiming is resolved by a lease and a unique constraint, and execution belongs to one
-- organisation. The existing arrangement gives the whole execution path the wide role, when only the
-- sweep needs it.
--
-- SECURITY DEFINER runs the BODY with the owner's privilege, so the CALLER does not need
-- BYPASSRLS. That is the entire point: the dispatcher can call this while connected as a role that
-- cannot see across tenants, because this function returns nothing but job identifiers.
--
-- WHAT IT MUST NOT RETURN
--
-- No organisation name, no organisation id, no document id, no payload, no error text. A caller
-- learns WHICH jobs are due and nothing else. That is not a convenience decision - it is the boundary
-- that lets the dispatcher stay narrow.

SET search_path = public, pg_catalog;

-- NOTE: `jobs.id` is `character varying`, NOT `uuid`. Verified against the live schema after the
-- first attempt failed with "return type mismatch ... Actual return type is character varying".
-- This is the third schema detail this file got wrong from memory; see the predicate comment below.
CREATE OR REPLACE FUNCTION fleet_due_job_ids(batch_size integer DEFAULT 50)
RETURNS TABLE (job_id character varying)
LANGUAGE sql
STABLE
SECURITY DEFINER
-- Pin search_path on the function itself, so the definer's privileges cannot be redirected through a
-- schema the caller controls. Without this, SECURITY DEFINER is a privilege-escalation primitive.
SET search_path = public, pg_catalog
AS $$
    -- MUST MATCH agent/executor.py:190-198 EXACTLY, or the equivalence check in step 2 is
    -- comparing two different things and would pass while changing which jobs get claimed.
    --
    -- Read from the code, not from memory. The first draft of this function was wrong in three ways
    -- - it used `status` instead of `state`, invented states ('PENDING','RETRY') instead of the real
    -- QUEUED, and added a lease clause that the dispatcher deliberately does NOT have. The
    -- equivalence check caught the column; the other two would have silently changed behaviour.
    --
    -- Note the absence of a lease clause. Per executor.py: "A FILTER, NOT A RESERVATION.
    -- JobLedger.claim inside execute is what actually takes the lease; this query only narrows the
    -- field so the loop is not scanning finished work." Adding a lease condition here would be a
    -- different query, not a narrower one.
    SELECT j.id
    FROM jobs j
    WHERE j.state = 'QUEUED'
      AND j.available_at <= now()
    ORDER BY j.available_at ASC, j.id ASC
    LIMIT GREATEST(1, LEAST(batch_size, 500));
$$;

COMMENT ON FUNCTION fleet_due_job_ids(integer) IS
    'ADR-0011: returns identifiers of due jobs across all tenants, so the dispatcher does not need '
    'BYPASSRLS on its own connection. Returns job ids ONLY - no organisation or document data. '
    'Rollback: DROP FUNCTION fleet_due_job_ids(integer).';

-- The caller needs EXECUTE and nothing else. Not SELECT on jobs.
GRANT EXECUTE ON FUNCTION fleet_due_job_ids(integer) TO granada_app;
GRANT EXECUTE ON FUNCTION fleet_due_job_ids(integer) TO granada_fleet;
