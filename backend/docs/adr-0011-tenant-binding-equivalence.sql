-- ADR-0011 step 3: the equivalence check for the tenant-binding migration.
--
-- WRITE THIS BEFORE THE CODE. It has to exist first, because the failure mode of step 2 is SILENT:
-- a mis-set tenant context returns zero rows, and a sweep that returns zero rows looks exactly like a
-- sweep with nothing to do. This directive has already:
--
--   * reported `PROBE EQUIVALENCE = IDENTICAL` over an EMPTY set (two empty sets are always equal)
--   * set `app.current_org` when the function reads `app.current_org_id`, and read the resulting 0 as
--     evidence that a correct design was wrong
--
-- So this check does two things the earlier ones did not: it REQUIRES non-empty input, and it
-- distinguishes "agrees" from "both blind".
--
-- RUN:
--   docker compose exec -T postgres psql -U postgres -d granada_auth -f /tmp/equiv.sql
--
-- EXPECT: every line ends in OK, and the final line is EQUIVALENCE CHECK = PASS.

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------------------------
-- 1. There must be real work to compare over. Without this the whole check is vacuous.
-- ---------------------------------------------------------------------------------------------
SELECT '1. due workflows in the system = ' || count(*) AS precondition
FROM agent_workflows
WHERE state IN ('PENDING', 'WAITING')
  AND next_run_at IS NOT NULL
  AND next_run_at <= now() + interval '7 days';

DO $$
DECLARE n integer;
BEGIN
    SELECT count(*) INTO n FROM agent_workflows WHERE state IN ('PENDING','WAITING');
    IF n = 0 THEN
        RAISE EXCEPTION
            'VACUOUS CHECK: agent_workflows holds no PENDING/WAITING rows. Two empty result sets '
            'always agree, so equivalence here would prove nothing. Seed data before running.';
    END IF;
    RAISE NOTICE 'precondition OK: % workflows exist to compare over', n;
END $$;

-- ---------------------------------------------------------------------------------------------
-- 2. The fleet's view: privileged, unbound, what the dispatcher sees today.
-- ---------------------------------------------------------------------------------------------
CREATE TEMP TABLE fleet_view AS
SELECT w.id, w.org_id, w.priority, w.next_run_at
FROM agent_workflows w
WHERE w.state IN ('PENDING','WAITING')
  AND w.next_run_at IS NOT NULL
  AND w.next_run_at <= now() + interval '7 days';

-- ---------------------------------------------------------------------------------------------
-- 3 + 4. Collect BOTH views as id arrays in PL/pgSQL variables and compare.
--
-- WHY ARRAYS AND NOT TEMP TABLES: a role that has been dropped to granada_app cannot write to a temp
-- table created by a more privileged role - "permission denied for table bound_view". A local variable
-- has no permission check. This cost two iterations to find, and the first failure ("permission denied
-- for table _roster") was the same mistake in the opposite direction: READING a temp table after the
-- role drop.
-- ---------------------------------------------------------------------------------------------
DO $$
DECLARE
    org_ids        text[];
    one_org        text;
    fleet_ids      text[];
    bound_ids      text[] := ARRAY[]::text[];
    this_org_ids   text[];
    missing        text[];
    extra          text[];
BEGIN
    -- FLEET VIEW: the privileged, unbound read the dispatcher does today.
    SELECT array_agg(w.id ORDER BY w.id) INTO fleet_ids
    FROM agent_workflows w
    WHERE w.state IN ('PENDING','WAITING')
      AND w.next_run_at IS NOT NULL
      AND w.next_run_at <= now() + interval '7 days';

    -- ROSTER: learned while still privileged. granada_app is denied EXECUTE on this function, so it
    -- must be read before the role drops - which is also the order a real dispatcher must use.
    SELECT array_agg(DISTINCT org_id) INTO org_ids FROM fleet_active_agent_ids();

    -- BOUND VIEW: unprivileged, one organisation at a time.
    SET LOCAL ROLE granada_app;
    FOREACH one_org IN ARRAY org_ids LOOP
        PERFORM set_config('app.current_org_id', one_org, true);
        SELECT array_agg(w.id ORDER BY w.id) INTO this_org_ids
        FROM agent_workflows w
        WHERE w.state IN ('PENDING','WAITING')
          AND w.next_run_at IS NOT NULL
          AND w.next_run_at <= now() + interval '7 days';
        bound_ids := bound_ids || COALESCE(this_org_ids, ARRAY[]::text[]);
    END LOOP;
    RESET ROLE;

    -- COMPARE, both directions. One direction alone cannot see rows the new path MISSES.
    SELECT array_agg(x ORDER BY x) INTO missing
      FROM unnest(COALESCE(fleet_ids, ARRAY[]::text[])) x
      WHERE x <> ALL(COALESCE(bound_ids, ARRAY[]::text[]));
    SELECT array_agg(x ORDER BY x) INTO extra
      FROM unnest(COALESCE(bound_ids, ARRAY[]::text[])) x
      WHERE x <> ALL(COALESCE(fleet_ids, ARRAY[]::text[]));

    RAISE NOTICE 'fleet view rows = %', COALESCE(array_length(fleet_ids, 1), 0);
    RAISE NOTICE 'bound view rows = %', COALESCE(array_length(bound_ids, 1), 0);
    RAISE NOTICE 'would be LOST  = %', COALESCE(array_length(missing, 1), 0);
    RAISE NOTICE 'would be LEAKED= %', COALESCE(array_length(extra, 1), 0);

    -- THE GUARDS. Two empty sets are always equal, so emptiness is FAILURE, not agreement.
    IF COALESCE(array_length(fleet_ids, 1), 0) = 0 THEN
        RAISE EXCEPTION 'VACUOUS: the fleet view is empty. Seed due work before running.';
    END IF;
    IF COALESCE(array_length(bound_ids, 1), 0) = 0 THEN
        RAISE EXCEPTION
            'BLIND, NOT EQUIVALENT: fleet sees % rows, the tenant-bound path sees 0. This is the '
            '"healthy-looking and blind" failure. Check app.current_org_id is actually set and that '
            'the GUC name matches what app.current_org() reads.',
            COALESCE(array_length(fleet_ids, 1), 0);
    END IF;
    IF COALESCE(array_length(missing, 1), 0) > 0 OR COALESCE(array_length(extra, 1), 0) > 0 THEN
        RAISE EXCEPTION 'MISMATCH: % missing, % extra',
            COALESCE(array_length(missing, 1), 0), COALESCE(array_length(extra, 1), 0);
    END IF;

    RAISE NOTICE 'EQUIVALENCE CHECK = PASS (% rows identical, both directions)',
        COALESCE(array_length(fleet_ids, 1), 0);
END $$;
