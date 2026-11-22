-- db/seed.sql
-- Development and demo data. Run as the database owner after schema.sql.
-- Capacity rows here are the S0 benchmark profile (32 vCPU, 128 GB RAM, 2 GPUs of 16 GB).
-- On the demo machine, scripts/capacity.py --profile host rewrites the resources rows.
-- Demo passwords (change before the demo): demo-student, demo-faculty, demo-admin.

BEGIN;

-- Workload types (proposal: names from the five S0 workload kinds; need classes and limits are proposals)
INSERT INTO workload_types (type_code, default_need_class, max_time_limit_s) VALUES
    ('code_eval', 2,   60),
    ('cpu_bound', 3,  600),
    ('mem_bound', 3,  600),
    ('gpu_bound', 3, 1800),
    ('mixed',     3,  600),
    ('python_ml', 3, 3600),  -- D-85: 14,400 s is a proposal the CHECK does not allow yet
    ('code_run',  3,   10);  -- D-96: practice runs, one run with the user's stdin (the 10 s limit is a proposal)

-- Demo users (bcrypt, cost 12)
INSERT INTO users (user_id, role, password_hash) VALUES
    ('01FE24BCS901', 'student',       '$2b$12$Atgu5qfd5zIyj8ZhilQB/Op8JC8JhNjdyTVe3sMXwnSDtkiZkheZ2'),
    ('01FE24BCS902', 'student',       '$2b$12$Atgu5qfd5zIyj8ZhilQB/Op8JC8JhNjdyTVe3sMXwnSDtkiZkheZ2'),
    ('FAC001',       'faculty',       '$2b$12$30Bt1m1CzAu1qSL/fNxCw..qDZsPJFNNEhOz76/zJbDu8yVPpnw92'),
    ('ADM001',       'administrator', '$2b$12$TUETnZzHa5R42Q7tc7jUeuLXKm7u1eKgZlRNTnrDC062NIzr6HDXK');

-- Lab session 7 (exam, open now) and 8 (lab), matching the API sample in the baseline
INSERT INTO lab_sessions (lab_session_id, faculty_id, kind, opens_at, closes_at) VALUES
    (7, 'FAC001', 'exam', now() - interval '1 hour', now() + interval '12 hours'),
    (8, 'FAC001', 'lab',  now() - interval '1 hour', now() + interval '12 hours');

-- Problem 12 with 10 test cases; files live under DATA_DIR/problems/12/
INSERT INTO problems (problem_id, created_by, title, time_limit_ms) VALUES
    (12, 'FAC001', 'Sum of N integers', 1000);
INSERT INTO test_cases (problem_id, case_no, input_path, expected_path)
SELECT 12, n, format('problems/12/%s.in', lpad(n::text, 2, '0')),
              format('problems/12/%s.out', lpad(n::text, 2, '0'))
FROM generate_series(1, 10) AS n;

-- Capacity, S0 profile. Lock order follows resource_id: 1 CPU, 2 RAM, 3 and 4 GPUs.
INSERT INTO resources (resource_id, kind, gpu_index, total, reserved) VALUES
    (1, 'cpu', NULL,  32000,  3200),    -- millicores
    (2, 'ram', NULL, 131072, 13108),    -- MiB; allocatable 117964
    (3, 'gpu', 0,     16384,  1639),    -- VRAM MiB; allocatable 14745
    (4, 'gpu', 1,     16384,  1639);
-- Move the identity counters past the fixed ids used above
DO $$
BEGIN
    PERFORM setval(pg_get_serial_sequence('lab_sessions', 'lab_session_id'), 100);
    PERFORM setval(pg_get_serial_sequence('problems', 'problem_id'), 100);
    PERFORM setval(pg_get_serial_sequence('resources', 'resource_id'), 4);
END $$;

-- The first submitted job gets Job ID 1042, as in the baseline scenario
ALTER TABLE jobs ALTER COLUMN job_id RESTART WITH 1042;

COMMIT;
