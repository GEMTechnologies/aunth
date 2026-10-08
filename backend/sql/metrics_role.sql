-- The metrics role, and why the operational gauges need one.
--
-- THE PROBLEM
-- -----------
-- `/metrics` reports cross-tenant counts: outbox backlog, stuck jobs, unknown submissions,
-- overdue reports. The application role is bound by row-level security, so it reads ZERO rows
-- from those tables unscoped:
--
--     app role, unscoped:  outbox backlog 0,  jobs 0     <- the tables are not empty
--
-- A gauge computed from that reports `granada_outbox_backlog_events 0` on a system whose relay
-- died hours ago. The alert on it never fires, the graph is flat and green, and the first
-- anyone hears of it is a customer asking why nothing works.
--
-- So the gauges are read through a SEPARATE connection, and when none is configured they are
-- **omitted** rather than reported as zero - `granada_metrics_operational_available` is 0, and
-- `GranadaOperationalMetricsUnavailable` fires on it. That behaviour is correct and it is also
-- a gap: today the gauges cannot be measured at all.
--
-- THE ROLE
-- --------
-- Read-only, cross-tenant, and nothing else. It is deliberately NOT the owner: the owner can
-- write, and a monitoring connection has no business writing anything. `BYPASSRLS` is what
-- makes a cross-tenant READ possible; `GRANT SELECT` is all it gets, so the worst an exploited
-- metrics endpoint can do is disclose counts and ages - which `/metrics` already publishes
-- unauthenticated.
--
-- Compare `backup_role.sql`, which needs the same privilege for a different reason: `pg_dump`
-- cannot read the data otherwise, because FORCE RLS binds the owner too.
--
-- Run as a superuser:

-- `BYPASSRLS` and `NOSUPERUSER` together. A superuser would work and is the wrong answer: it
-- carries every other privilege as well, and the point of this role is that it has exactly one.
CREATE ROLE granada_metrics LOGIN PASSWORD '<a strong secret>' NOSUPERUSER BYPASSRLS;

GRANT CONNECT ON DATABASE granada_auth TO granada_metrics;
GRANT USAGE ON SCHEMA public TO granada_metrics;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO granada_metrics;

-- So a table added by a later migration does not silently become unreadable. Without this the
-- gauges would be absent for a NEW table and present for the old ones, which reads as "fine".
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO granada_metrics;

-- ---------------------------------------------------------------------------
-- Then, in the API service:
-- ---------------------------------------------------------------------------
--   GRANADA_METRICS_DATABASE_URL=postgresql+psycopg2://granada_metrics:<secret>@postgres:5432/granada_auth
--
-- Verify:
--
--   curl -s localhost:8000/metrics | grep operational_available
--   -> granada_metrics_operational_available 1
--
-- Before the role exists it reads 0 and every operational gauge is ABSENT. That is the correct
-- behaviour and it is also a real gap: the alerts written for those gauges cannot fire.
--
-- ---------------------------------------------------------------------------
-- WHY NOT SIMPLY USE THE OWNER
-- ---------------------------------------------------------------------------
-- `GRANADA_METRICS_DATABASE_URL` could be pointed at `granada_user`, and the gauges would work.
-- That is what `docker-compose.yml` does today, as the pragmatic choice, and it is recorded
-- there as such. It is worse for one reason worth stating plainly: **the owner can write.**
-- A monitoring connection that can write is a monitoring connection that can, through a bug or
-- a credential leak, alter the data it is reporting on - and a metric that can be edited by its
-- own reporter is not a metric.
--
-- ---------------------------------------------------------------------------
-- WHAT THIS DOES NOT FIX
-- ---------------------------------------------------------------------------
-- It does not make the alerts fire. The rules in `ops/prometheus/granada.rules.yml` are correct
-- and **inert** until a Prometheus and an Alertmanager exist to evaluate them. This role is one
-- of three things needed; the other two are a running Prometheus and somewhere to send an alert.
