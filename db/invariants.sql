-- db/invariants.sql
-- Each query returns one row per violation. Empty results mean the database is consistent.
-- tests/helpers.py runs "invariants" after every system test, and "leaks" once the run has settled.

-- name: invariants
-- 1. allocated equals the sum of open allocation items and stays in 0 .. total - reserved (NFR-06)
SELECT 'capacity' AS check_name, r.resource_id::text AS subject,
       format('allocated=%s held=%s limit=%s', r.allocated, coalesce(h.held, 0), r.total - r.reserved) AS detail
FROM resources r
LEFT JOIN (SELECT i.resource_id, sum(i.amount) AS held
           FROM allocation_items i
           JOIN allocations a ON a.allocation_id = i.allocation_id AND a.status = 'open'
           GROUP BY i.resource_id) h ON h.resource_id = r.resource_id
WHERE r.allocated <> coalesce(h.held, 0) OR r.allocated < 0 OR r.allocated > r.total - r.reserved
UNION ALL
-- 2. at most one open allocation and one open attempt per job
SELECT 'open_allocations', job_id::text, count(*)::text
FROM allocations WHERE status = 'open' GROUP BY job_id HAVING count(*) > 1
UNION ALL
SELECT 'open_attempts', job_id::text, count(*)::text
FROM attempts WHERE ended_at IS NULL GROUP BY job_id HAVING count(*) > 1
UNION ALL
-- 3. queued, admitted and released jobs hold no open allocation
SELECT 'holds_while_idle', j.job_id::text, j.state
FROM jobs j JOIN allocations a ON a.job_id = j.job_id AND a.status = 'open'
WHERE j.state IN ('queued', 'admitted', 'released')
UNION ALL
-- 4. allocated and running jobs hold exactly one open allocation
SELECT 'missing_allocation', j.job_id::text, j.state
FROM jobs j
WHERE j.state IN ('allocated', 'running')
  AND NOT EXISTS (SELECT 1 FROM allocations a WHERE a.job_id = j.job_id AND a.status = 'open')
UNION ALL
-- 5. every allocation has exactly one committed allocate audit row (NFR-05)
SELECT 'audit_allocate', a.allocation_id::text, count(au.audit_id)::text
FROM allocations a
LEFT JOIN allocation_audit au
       ON au.allocation_id = a.allocation_id AND au.action = 'allocate' AND au.outcome = 'committed'
GROUP BY a.allocation_id HAVING count(au.audit_id) <> 1
UNION ALL
-- 6. a released allocation has exactly one release audit row, an open one has none (NFR-05)
SELECT 'audit_release', a.allocation_id::text, a.status || ':' || count(au.audit_id)
FROM allocations a
LEFT JOIN allocation_audit au ON au.allocation_id = a.allocation_id AND au.action = 'release'
GROUP BY a.allocation_id, a.status
HAVING count(au.audit_id) <> CASE WHEN a.status = 'released' THEN 1 ELSE 0 END
UNION ALL
-- 7. the recorded state equals the last history row, and every job has history (NFR-05)
SELECT 'history_tail', j.job_id::text, j.state || ' vs ' || coalesce(h.to_state, 'none')
FROM jobs j
LEFT JOIN LATERAL (SELECT to_state FROM job_state_history
                   WHERE job_id = j.job_id ORDER BY event_id DESC LIMIT 1) h ON true
WHERE h.to_state IS DISTINCT FROM j.state
UNION ALL
-- 8. a worker marked alive belongs to a running job with an open attempt
SELECT 'alive_without_run', w.attempt_id::text, j.state
FROM worker_liveness w
JOIN attempts a ON a.attempt_id = w.attempt_id
JOIN jobs j     ON j.job_id = a.job_id
WHERE w.status = 'alive' AND (j.state <> 'running' OR a.ended_at IS NOT NULL);

-- name: leaks
-- NFR-07: once a run has settled, no completed, failed or cancelled job still holds resources,
-- and no attempt is left open on a job that is not running.
SELECT 'leak' AS check_name, j.job_id::text AS subject, j.state AS detail
FROM jobs j JOIN allocations a ON a.job_id = j.job_id AND a.status = 'open'
WHERE j.state IN ('completed', 'failed', 'cancelled')
UNION ALL
SELECT 'open_attempt_not_running', a.job_id::text, j.state
FROM attempts a JOIN jobs j ON j.job_id = a.job_id
WHERE a.ended_at IS NULL AND j.state NOT IN ('allocated', 'running');
