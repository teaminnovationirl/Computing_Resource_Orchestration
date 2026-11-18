# Mini Project: Project Context
 
Prepared 15 November 2026 from the team's latest documents. Current position: Sprint 1 design is complete, the Sprint 1 review deck is built (38 slides), and Gate 2 is the next formal review.
 
How to use this file: load it as background for every chat in this project. It follows the team's latest baseline (the modified S1 requirements plus S0, and the final 15-table database design). Where an older document disagrees with it, this file wins; section 16 lists what is outdated. If two sources conflict on something that matters, ask the team before assuming.
 
## 1. The project in one paragraph
 
A LAN-based system on one shared departmental server. Students, faculty and automated sources submit jobs, including programming submissions that are compiled and judged, and these jobs compete for limited CPU, RAM and GPU memory. The system validates and queues the jobs, admits them by a scheduling policy, allocates resources in single atomic database transactions with row-level locks so nothing is ever over-allocated, runs each job in a sandbox under CPU, memory and time limits, watches it with heartbeats, recovers jobs whose worker dies, and shows live job and resource state. In effect it is a small local cloud scheduler combined with an online judge. The flagship scenario is a lab exam in which many students submit code at once.
 
## 2. Course, grading and people
 
| Item | Detail |
| --- | --- |
| Course | Sem 5 "Mini Project" (Foundation Integration Course) at KLE Technological University, School of Computer Science and Engineering, Hubballi |
| Theme | Theme 11: Concurrent Resource Orchestration & Transaction-Safe Monitoring (also worded as multiple concurrent requests competing for limited resources) |
| Artefact name | Computing Resource Allocation & Utilization Orchestration System |
| Problem statement chosen | "Lab resource allocation and safe monitoring system" |
| Team number | 10 |
| Guide on all documents | Dr. Vijayalakshmi M. (the team's notes also describe the mentor as their HoD) |
| Department | Computer Science & Engineering |
| Process | Hybrid SDLC with stages S0 to S7, three sprints and formal gates. The team meets the mentor before each stage review. |
 
Grading this year: the college is marking whether the team learnt the fundamentals (recruiter feedback), not novelty or a working demo. A novel or working build still earns extra marks. Subject priority from high to low: OS, DBMS, DSA/DAA, CN, WT, DMS. Emphasis: OS memory and process management; DBMS normalisation and ACID on a complex schema; DSA used to solve real problems; WT front-end and back-end integration.
 
Idea history, so old ideas are not revived by mistake: first an IRCTC-style seat booking with consent-based seat swaps (the mentor felt it duplicated IRCTC), then a lab-exam online judge, then the current resource orchestration system, which keeps code evaluation as its flagship workload.
 
### Team and engine ownership
 
| Student | USN | Engine | Main courses |
| --- | --- | --- | --- |
| Sagar Narasannavar | 01FE24BCS005 | E3 Transaction-Safe Resource Allocation | DBMS |
| Aniruddh Naik | 01FE24BCS034 | E4 Job Monitoring & Recovery | Web Technologies, Computer Networks |
| Sudhanv Kulkarni | 01FE24BCS114 | E2 Workload Scheduling | Data Structures & Algorithms, Discrete Mathematical Structures |
| Abhishek Kuri | 01FE24BCS120 | E1 Process Execution & Control | Operating Systems |
 
There is no E5: four members, four engines. At Gate 2 every owner must explain their own engine and one other engine independently.
 
## 3. The problem, scope and stakeholders
 
Problem (S0): a shared departmental computing server receives many concurrent requests from students, faculty and automated workloads. Each needs different CPU, RAM, GPU/VRAM and time. Handled through separate tools, manual checks or plain first-come-first-served allocation, this causes contention, long waits, over-allocation, inconsistent records and stuck jobs.
 
Pain points the design answers:
- Request handling, resource checks, execution and monitoring are spread across separate tools.
- Availability is checked separately and is already stale when the allocation is made (this is the weakness E3's re-check inside the lock removes).
- Allocation and release are not atomic, so concurrent requests can over-allocate.
- There is no single, consistent view of job and resource state.
- Failed workers and timed-out jobs need manual recovery, and resources are not released promptly.
Target outcome: a queue with at least two scheduling policies compared on waiting time, throughput and utilisation; transaction-safe allocation and release; jobs run under OS controls with code evaluation as one representative workload; one consistent view of state; failures detected and jobs recovered or requeued; measured performance.
 
In scope: CPU, RAM and GPU/VRAM modelling; request validation, job creation, queueing and at least two policies; resource-aware admission using explicit resource vectors; single-database ACID allocation, release, job state and audit; controlled worker execution, heartbeats and failure/requeue handling; code evaluation as one workload (compile, run, test, verdict, release).
 
Out of scope: production cloud orchestration or a Kubernetes replacement; multi-node clusters and distributed transactions; learning-based or research-level scheduling; security certification or compliance audit.
 
Stakeholders: students (submit jobs, see status and results), faculty (create lab sessions and problems, supervise, review), technical staff and administrators (maintain the server, policy and capacity profile), the shared-server owner (controlled, auditable capacity use), and automated workload sources (generate concurrent requests for tests). Secondary: the project team, guide and evaluators, department management.
 
Execution host used for the design and benchmarks (S0 configuration): 32 vCPU, 128 GB RAM, 2 GPUs of 16 GB. This is the baseline machine for capacity modelling, not a performance target. The real demo machine is not confirmed: the university may provide a workstation with 2 GPUs (thought to be RTX A6000), otherwise a team laptop is used. With the 10% reserve the allocatable RAM maximum is 117,964 MiB, which is the upper bound the API validates against.
 
## 4. Requirements baseline (latest)
 
The approved baseline is FR-01 to FR-15 and NFR-01 to NFR-10. On evaluator feedback the team merged the earlier S1 v1.3 set (27 functional and 15 non-functional IDs) into these. 13 functional requirements are Mandatory and 2 are Challenge (FR-04, FR-10). Any document that still shows 27 FRs, 15 NFRs, or an ID such as FR-21 is outdated.
 
### Functional requirements
 
| ID | Requirement | Priority | Engine |
| --- | --- | --- | --- |
| FR-01 | FCFS scheduling as the baseline and need-based priority as the alternative policy | Mandatory | E2 |
| FR-02 | Accept requests (resource vector, duration, priority, workload type, payload); reject invalid ones with a field-specific error; assign a unique Job ID; queue | Mandatory | E2 |
| FR-03 | Check that the request fits the allocatable capacity; if not, the job stays queued and is re-checked next cycle or after a capacity change | Mandatory | E2 |
| FR-04 | Suspend new admissions while the host or execution backend is unhealthy; resume when healthy | Challenge | E4 |
| FR-05 | Allocate all requested resources atomically with row-level locking and transaction-level capacity checks; no partial allocation | Mandatory | E3 |
| FR-06 | Audit record for every allocation and release | Mandatory | E3 |
| FR-07 | Dispatch an admitted job to an available worker | Mandatory | E1 |
| FR-08 | Run each job in its own worker under its CPU and memory limits and declared time limit; stop overruns; classify time-limit violations; processes stopped before resources are released | Mandatory | E1 |
| FR-09 | Compile code submissions, run them on defined test cases, store a verdict per test case and overall | Mandatory | E1 |
| FR-10 | Cancel: students and faculty their own jobs, administrators any job; deny unauthorised cancels; remove queued jobs; stop running jobs before release | Challenge | E1, E4, E3 |
| FR-11 | Record every job-state transition; dashboard and live feed show the latest state each role is allowed to see | Mandatory | E4 |
| FR-12 | Heartbeats at a defined interval; mark a worker failed after the timeout | Mandatory | E4 |
| FR-13 | At a terminal state, release all resources in one atomic transaction after termination is confirmed; audit it | Mandatory | E3 |
| FR-14 | On worker failure: stop leftovers, release, requeue only after release is confirmed; at most 2 retries, then failed; reconcile jobs without live workers at platform start | Mandatory | E4 |
| FR-15 | Authenticate users, keep role-based sessions, restrict actions by permission, hide jobs a user may not see | Mandatory | E4 |
 
### Non-functional requirements (these are the approved quantitative targets)
 
| ID | Target |
| --- | --- |
| NFR-01 | Scheduling wait of at most 2 s; a valid job starts within 5 s of submission when resources are available |
| NFR-02 | At least 50 jobs per minute under normal operating conditions |
| NFR-03 | At least 99% validation accuracy on well-formed requests and at least 99% code-evaluation verdict accuracy on the reference test set |
| NFR-04 | Under saturation, average utilisation of at least 70% of allocatable CPU, 60% of RAM and 60% of GPU/VRAM |
| NFR-05 | Zero duplicate, missing or inconsistent allocation and release records; recorded job states match actual states with at least 99% accuracy |
| NFR-06 | Zero over-allocation events; no more than 90% of each resource type allocated (10% reserved for the host) |
| NFR-07 | All resources released within 2 s of the terminal-state event; zero leaks after cleanup |
| NFR-08 | Worker failure detected after 6 s without heartbeats; recovery and requeue complete within 10 s |
| NFR-09 | Dashboard reflects every valid state change within 2 s |
| NFR-10 | 99% of jobs that exceed their time limit are terminated and correctly classified |
 
### Acceptance criteria in short
 
- FR-01: FCFS keeps arrival order; under priority by need a higher need class that fits goes first, the same class keeps arrival order, and no job is overtaken for more than 60 s.
- FR-02: a well-formed request is stored with every field and a unique Job ID; an invalid one is rejected with its reason.
- FR-03: a job that does not fit stays queued and is re-checked next cycle; it is never admitted partly.
- FR-05, FR-06: allocation commits completely or rolls back; concurrent requests for the last unit never both succeed; every change has an audit record.
- FR-07, FR-08: each job runs in its own worker under its limits; an overrun is stopped by the platform.
- FR-09: a code submission is compiled, run on every test case and given a verdict.
- FR-12, FR-13, FR-14: heartbeats stop for 6 s, so the worker is marked failed, resources are released, and the job is requeued (2 retries, then failed).
- FR-11, FR-15: the dashboard shows each state change within 2 s; a student sees only their own jobs.
### Priorities and clusters
 
Priority ranking of requirement groups (from the S0 needs): 1 prevent over-allocation and inconsistent state; 2 reliable scheduling and admission; 3 detect failures and release resources; 4 unified monitoring and auditable state; 5 controlled code evaluation as the representative workload. Mandatory requirements are the minimum working system; Challenge requirements are stretch targets.
 
| Cluster | Area | Requirements | Engine |
| --- | --- | --- | --- |
| C1 | Workload intake and scheduling | FR-01 to FR-03, NFR-01, NFR-03, NFR-04 | E2 |
| C2 | Transaction-safe allocation | FR-05, FR-06, FR-13, NFR-05 to NFR-07 | E3 |
| C3 | Controlled execution and worker lifecycle | FR-07 to FR-10, NFR-02, NFR-03, NFR-10 | E1 |
| C4 | Monitoring, heartbeats and dashboard | FR-04, FR-11, FR-12, FR-15, NFR-05, NFR-09 | E4 |
| C5 | Failure detection and recovery (cross-engine) | FR-12, FR-14, NFR-08 | E4 with E2 and E3 |
 
## 5. Use cases
 
Human actors: Student, Faculty, Administrator (all generalise to User). System actors: automated workload source, scheduling cycle, heartbeat timeout, worker process.
 
| ID | Use case | Engine |
| --- | --- | --- |
| UC-01 | Submit a workload request | E2 |
| UC-02 | Admit the next eligible workload (scheduling cycle) | E2 |
| UC-03 | Allocate resources to an admitted job | E3 |
| UC-04 | Release the resources of a finished job | E3 |
| UC-05 | Execute a workload under limits | E1 |
| UC-06 | Evaluate a code submission | E1 |
| UC-07 | Cancel a job | E1 |
| UC-08 | Monitor jobs and resources | E4 |
| UC-09 | Detect a worker failure and recover | E4 |
| UC-10 | Log in | E4 |
 
## 6. Architecture
 
Three layers, one block per engine (the guide asked that no box mix parts of two engines).
 
| Layer | Contents |
| --- | --- |
| Upper: presentation | Web pages (login, submit job, my jobs and results, live monitor, admin console) and an automated workload script that uses the same API for tests and benchmarks |
| Middle: one application server | Python and FastAPI; the four engines are modules of one server process. E4 is the entry point (API gateway, login, roles). REST over HTTPS, live updates over WebSocket. Engine-to-engine calls are function calls inside the process. |
| Back end: data and execution | PostgreSQL 16 (one database, 15 tables, each engine writes only its own tables); a job file store (code, test cases, outputs, logs, used by reference, mounted read-only into the sandbox); the execution host with one sandbox container per job (cgroups v2) |
 
Why this shape: one server keeps every transaction local, so no distributed transactions are needed (they are out of scope). Each state-changing call commits its own transaction, so a crash never leaves a half hand-over.
 
Diagram colour code used everywhere: E1 green, E2 blue, E3 amber, E4 purple.
 
## 7. The four engines
 
### E1 Process Execution & Control (Abhishek Kuri, Operating Systems)
- Purpose: run every allocated job in its own sandboxed worker under CPU, memory and time limits; stop jobs on overrun, cancellation or worker loss; compile and judge code submissions; ask E3 to release at the end.
- Owns tables: attempts, problems, test_cases, test_results.
- Mechanisms: Linux cgroups v2 (cpu.max, memory.max with swap off, pids.max 64), one sandbox container per job (no network, read-only root, unprivileged user), fork and exec for the worker, wall-clock timer, SIGTERM then SIGKILL after 2 s, sandbox started within 2 s, heartbeat to E4 every 2 s. Outputs are compared outside the sandbox so expected outputs never enter it.
- Failure behaviour: time limit gives failed (timeout); memory limit gives exit 137 with OOM and failed (memory limit); compile error, wrong answer or runtime error is stored as the verdict and the job still completes; cancel stops every process and confirms before release; worker lost is handled by E4 (stop and confirm, then release, then requeue); if the stop cannot be confirmed the allocation stays held and the job is not requeued, so capacity is never given twice.
- Complexity: start, heartbeat and limit enforcement O(1); judge O(t) for t test cases; compare O(s); stop and confirm O(p) with p at most 64.
### E2 Workload Scheduling (Sudhanv Kulkarni, DSA and Discrete Mathematical Structures)
- Purpose: validate requests, assign Job IDs, keep the queue and every 0.5 s (and at once after a release) admit the next job that fits, by FCFS or by priority by need.
- Owns tables: jobs, workload_types, lab_sessions.
- Mechanisms: FIFO list (strict arrival order), binary min-heap keyed (need class, arrival time, Job ID), hash map from Job ID to queue entry, greedy first fit, a 60 s no-overtaking guard against starvation, integer units (millicores and MiB). The need class comes from the workload type and the lab session, never from who submitted: an open exam session gives class 1, so an exam never waits behind practice work and a faculty practice job does not jump ahead. A requeued job keeps its arrival time, with at most 2 retries.
- Policies: FCFS (baseline) and priority by need, switched by an administrator through the API. The Gate 2 document reports that in a dry run of the saturation benchmark B2, mean CPU use while jobs waited rose from 82% under FCFS to 95% under priority by need.
- Failure behaviour: missing or out-of-range field gives 400 with the field name and no job; not enough capacity keeps the job's place; capacity taken first by another job means E3 rolls back and the job keeps its place; admission paused while the host is unhealthy.
- Complexity: insert O(log n) heap, O(1) list and map; next job under FCFS O(1); under priority O(log n); first fit past k jobs that do not fit O(k log n); cancel or requeue O(1) map and O(log n) heap; fit check O(r).
### E3 Transaction-Safe Resource Allocation (Sagar Narasannavar, DBMS)
- Purpose: keep the capacity table with a 10% reserve; allocate and release CPU, RAM and GPU memory, each as one row-locked transaction with an audit record; notify E2 when capacity changes. E3 also holds the database schema, with each owner responsible for their tables.
- Owns tables: resources, allocations, allocation_items, allocation_audit.
- Mechanisms: PostgreSQL 16; SELECT ... FOR UPDATE on the needed resource rows in resource_id order; re-check inside the lock; CHECK (0 <= allocated <= total - reserved) as a backstop; partial unique index (one open allocation per job); idempotent release keyed by the open allocation; best-fit GPU placement (least free VRAM that fits); append-only audit and history through triggers; integer units.
- Failure behaviour: two requests for the last unit are serialised by the row locks, so one commits and the other is refused and audited; a stale snapshot is caught by the re-check, with rollback and the job keeping its place; a failure between update and audit rolls everything back; a second release returns ALREADY_RELEASED and changes nothing; code that would over-allocate is rejected by the CHECK constraint; a lost worker not yet confirmed stopped gets no release.
- Complexity: snapshot O(r); allocate and release O(r log N); find the open allocation O(log N); audit insert O(log N). Here r is the number of resource rows and N the table size.
### E4 Job Monitoring & Recovery (Aniruddh Naik, Web Technologies and Computer Networks)
- Purpose: entry point of the server (API gateway, login, roles); records every state change; watches heartbeats; recovers lost jobs; pauses admission while the host is unhealthy; pushes live updates to the pages.
- Owns tables: users, login_sessions, job_state_history, worker_liveness.
- Mechanisms: FastAPI with OpenAPI docs; REST over HTTPS plus WebSocket /ws/events; bcrypt salted hashes; session tokens stored only as hashes; HTML, CSS and JavaScript pages; heartbeat every 2 s and a worker is lost after 6 s (three missed beats), with a sweep every 1 s so detection takes 6 to 7 s; a database trigger that allows only the permitted state transitions and writes one history row per change; host-health guard (admission paused when host memory is short; running jobs unaffected); reconciliation of every job at server start before admission resumes.
- Roles: a student submits, views and cancels only their own jobs; faculty manage their lab sessions and problems and see all jobs; administrators also change the capacity profile and the scheduling policy and can cancel any job.
- Failure behaviour: no login or wrong role gives 401 or 403; a late heartbeat from a lost worker is ignored; an illegal state change is rejected by the trigger and logged; cancel of a finished job gives 409.
- Complexity: liveness sweep O(w) for w running workers; login lookup O(log U); role check O(1); state change with history row O(log H); push one update O(c) for c connected pages.
## 8. Messages between engines
 
| No. | From to | Message |
| --- | --- | --- |
| 1 | Pages to E4 | login, submit, cancel, view (REST, JSON) |
| 2 | E4 to E2 | authenticated job request (user, role, resource vector, time limit, workload type, lab session, file reference) |
| 3 | E3 to E2 | capacity snapshot: free CPU millicores, RAM MiB, VRAM MiB per GPU |
| 4 | E2 to E3 | admit(Job ID, resource vector), i.e. the allocate call |
| 5 | E3 to E1 | allocated job (Job ID, allocation ID, limits, GPU device, file reference); if refused, the job stays queued in E2 |
| 6 | E1 to E4 | heartbeat every 2 s, state events, verdicts |
| 7 | E1 to E3 | release(Job ID) when the job ends |
| 8 | E3 to E2 | capacity changed (after every release); starts an admission cycle |
| 9 | E4 to E1 | stop or cancel; E1 confirms the stop |
| 10 | E4 to E3 | release, only after the stop is confirmed |
| 11 | E4 to E2 | requeue (at most 2 retries), cancel of a queued job, pause or resume admission |
| 12 | E4 to Pages | live state over WebSocket within 2 s |
 
Every engine writes its state changes with the history row in one transaction, and only to its own tables.
 
## 9. Job states and recovery
 
States: queued, admitted, allocated, running, completed, failed, cancelled, released. The state-tracker trigger checks every change of jobs.state against a list of allowed transitions and rejects anything else (the team confirmed this design). Each engine makes only its own transitions.
 
| From | Event (engine) | To |
| --- | --- | --- |
| (start) | request valid, Job ID assigned (E2) | queued |
| queued | picked by the policy and it fits (E2) | admitted |
| queued | cancelled while queued (E2 removes it) | cancelled |
| admitted | allocation committed (E3) | allocated |
| admitted | refused, capacity taken first, rollback (E3) | queued, same place |
| allocated | worker started under limits (E1) | running |
| allocated | cancelled before start (E1) | cancelled |
| running | ends within its limits (E1) | completed |
| running | time limit, memory limit, crash, or no heartbeat for 6 s (E1, E4) | failed |
| running | cancelled by owner or administrator, processes stopped and confirmed (E1) | cancelled |
| completed, failed | release committed (E3); for failed only after the stop is confirmed | released |
| cancelled | release committed if resources were held (E3) | released, or end if nothing was held |
| released | worker was lost and fewer than 2 requeues so far (E4, then E2) | queued |
| released | any other case | end |
 
Recovery order is always: stop and confirm, then release, then requeue (never release first). A requeued job keeps its original arrival time. After 2 retries the job ends as failed. The history table stores from_state, to_state, reason, actor and at; from_state is empty only in a job's first row; rows can never be changed or deleted.
 
## 10. Core algorithms
 
E2, one admission cycle (every 0.5 s and on "capacity changed"):
 
    if admission_paused: return
    free = E3.snapshot()
    if policy == FCFS:
        job = fifo.head()
        if job and fits(job, free): admit(job)        # otherwise the head waits
    else:                                              # priority by need
        oldest = fifo.head()
        candidates = [oldest] if now - oldest.arrival >= 60 s else heap.in_order()
        job = first c in candidates with fits(c, free)
        if job: admit(job)
    admit(job): if E3.allocate(job.id, job.vector) == COMMITTED:
                    remove job from fifo, heap and index
 
E3, allocate and release:
 
    allocate(job, v):
        BEGIN
        rows = SELECT * FROM resources WHERE resource_id IN needed(v)
               ORDER BY resource_id FOR UPDATE            # fixed order: no deadlock
        if any row: allocated + need > total - reserved:
            ROLLBACK; audit(job, "refused"); return REFUSED
        gpu = best_fit_gpu(rows, v.vram)                  # least free VRAM that fits
        UPDATE resources SET allocated = allocated + need
        INSERT allocations(job, "open"), allocation_items(...)
        INSERT allocation_audit(job, "allocate")
        UPDATE jobs SET state = "allocated"               # trigger checks the transition
        COMMIT; return COMMITTED
 
    release(job):
        BEGIN
        a = SELECT * FROM allocations WHERE job_id = job AND status = "open" FOR UPDATE
        if a is none: COMMIT; return ALREADY_RELEASED     # a repeat changes nothing
        lock its resource rows in id order
        UPDATE resources SET allocated = allocated - amount
        UPDATE allocations SET status = "released"; audit(job, "release")
        COMMIT; notify E2 "capacity changed"
 
E4, liveness and recovery:
 
    every 1 s: for each worker with status alive and now - last_beat_at > 6 s:
                   mark lost (once); recover(job)
    recover(job): E1.stop_and_confirm(job)    # waits until no process of the job is left
                  E3.release(job)             # only after the stop is confirmed
                  if job.retry_count < 2: E2.requeue(job) else final state failed
 
E1, run: start the sandbox (cpus, memory, swap off, pids limit 64, no network, read-only root); at the deadline SIGTERM and after 2 s SIGKILL, giving failed (timeout); exit 137 with a kernel OOM report gives failed (memory limit).
 
## 11. Database design (final: 15 tables, 17 foreign keys)
 
Facts that must stay consistent everywhere:
- The ER diagram and the relational schema now match. There are no STUDENT or FACULTY tables. The specialisation of USER was mapped with Elmasri and Navathe option 8C: one USERS relation with a role attribute (student, faculty, administrator).
- user_id is the login ID: the USN for a student, a staff ID for faculty and administrators. There is no usn column.
- ER diagram (Chen notation): 13 entity types and 16 relationship types. Relationship types: 13 one-to-many, 1 one-to-one (HEARTBEAT) and 2 many-to-many (EVALUATES, CLAIMS). Weak entity types: TEST_CASE (owner PROBLEM) and WORKER_LIVENESS (owner ATTEMPT). The two many-to-many relationships become the tables TEST_RESULTS and ALLOCATION_ITEMS. CONDUCTS and AUTHORS link only users whose role is faculty (a trigger checks the role). Composite attribute on JOB: resource_request (cpu_millicores, ram_mib, vram_mib); composite on LAB_SESSION: window (opens_at, closes_at); derived attribute RESOURCE.allocated.
- Built with the Elmasri and Navathe 8-step ER-to-relational mapping from a universal relation U of 60 attributes. 14 of the 15 relations were already in BCNF; after the manual changes M1 to M4 all 15 are in BCNF and also in 4NF.
- Natural keys: user_id, token_hash, type_code. All other tables use generated keys. Composite keys: test_cases (problem_id, case_no), test_results (attempt_id, case_no), allocation_items (allocation_id, resource_id). faculty_id and created_by are role names for user_id.
- Unique keys: attempts (job_id, attempt_no); resources (kind, gpu_index); allocations (allocation_id, job_id), which is the target of the audit table's composite foreign key.
- Delete rules: CASCADE for login sessions, weak entities, test results and allocation items (they go with their parent); SET NULL for jobs.lab_session_id (a job may exist outside a session); RESTRICT for users with jobs, jobs with history, attempts or audit rows, and used resources.
### The 15 relations
 
The PK column shows each relation's primary key. Amounts are integers: millicores and MiB.
 
| Relation | Owner | PK | Other columns | Purpose |
| --- | --- | --- | --- | --- |
| USERS | E4 | user_id | role, password_hash | Everyone who logs in: student, faculty or administrator. user_id is the USN for a student, a staff ID otherwise. |
| LOGIN_SESSIONS | E4 | token_hash | user_id, expires_at | Active logins; the token is stored only as a hash. |
| LAB_SESSIONS | E2 | lab_session_id | faculty_id, kind, opens_at, closes_at | Exam or lab sessions run by faculty; an open exam session gives its jobs need class 1. |
| PROBLEMS | E1 | problem_id | created_by, title, time_limit_ms | Code-evaluation problems set by faculty. |
| TEST_CASES | E1 | problem_id, case_no | input_path, expected_path | Test data kept as files; expected outputs never enter the sandbox. Weak entity of PROBLEM. |
| WORKLOAD_TYPES | E2 | type_code | default_need_class, max_time_limit_s | The five workload types, with default need class and maximum time limit. |
| JOBS | E2 | job_id | user_id, type_code, lab_session_id, problem_id, cpu_millicores, ram_mib, vram_mib, time_limit_s, need_class, state, retry_count, submitted_at, payload_path | One row per request. Queue order is (need_class, submitted_at, job_id). |
| JOB_STATE_HISTORY | E4 | event_id | job_id, from_state, to_state, reason, actor, at | Append-only; one row per state change, written by the state-tracker trigger. |
| ATTEMPTS | E1 | attempt_id | job_id, attempt_no, sandbox_name, started_at, ended_at, end_reason, verdict | One row per execution attempt (attempt_no 1 to 3); at most one open attempt per job. |
| WORKER_LIVENESS | E4 | attempt_id | last_beat_at, status | Last heartbeat of each running attempt; lost after 6 s. Weak entity of ATTEMPT. |
| TEST_RESULTS | E1 | attempt_id, case_no | verdict, time_ms | Verdict of each test case for one attempt (from the many-to-many EVALUATES). |
| RESOURCES | E3 | resource_id | kind, gpu_index, total, reserved, allocated | One row for the CPU pool, one for the RAM pool and one per GPU. CHECK: 0 <= allocated <= total - reserved. |
| ALLOCATIONS | E3 | allocation_id | job_id, status, created_at, released_at | At most one open allocation per job (partial unique index). |
| ALLOCATION_ITEMS | E3 | allocation_id, resource_id | amount | How much of each resource one allocation holds (from the many-to-many CLAIMS). |
| ALLOCATION_AUDIT | E3 | audit_id | job_id, allocation_id, action, outcome, at | Append-only record of every allocate, refuse and release. |
 
### The 17 foreign keys
 
ALLOCATION_AUDIT (allocation_id, job_id) also references ALLOCATIONS as one composite key (change M2), so an audit row cannot name a different job than its allocation.
 
| Child | Column | References |
| --- | --- | --- |
| LOGIN_SESSIONS | user_id | USERS.user_id |
| LAB_SESSIONS | faculty_id | USERS.user_id |
| PROBLEMS | created_by | USERS.user_id |
| TEST_CASES | problem_id | PROBLEMS.problem_id |
| JOBS | user_id | USERS.user_id |
| JOBS | type_code | WORKLOAD_TYPES.type_code |
| JOBS | lab_session_id | LAB_SESSIONS.lab_session_id |
| JOBS | problem_id | PROBLEMS.problem_id |
| JOB_STATE_HISTORY | job_id | JOBS.job_id |
| ATTEMPTS | job_id | JOBS.job_id |
| WORKER_LIVENESS | attempt_id | ATTEMPTS.attempt_id |
| TEST_RESULTS | attempt_id | ATTEMPTS.attempt_id |
| ALLOCATIONS | job_id | JOBS.job_id |
| ALLOCATION_ITEMS | allocation_id | ALLOCATIONS.allocation_id |
| ALLOCATION_ITEMS | resource_id | RESOURCES.resource_id |
| ALLOCATION_AUDIT | job_id | JOBS.job_id |
| ALLOCATION_AUDIT | allocation_id | ALLOCATIONS.allocation_id |
 
### Manual modifications after the mapping (M1 to M4)
 
| No. | Change | Why |
| --- | --- | --- |
| M1 | TEST_RESULTS: problem_id removed; a trigger checks that the case belongs to the attempt's problem | removes the partial dependency (attempt_id determines problem_id); key (attempt_id, case_no); BCNF |
| M2 | ALLOCATION_AUDIT: composite foreign key (allocation_id, job_id) to ALLOCATIONS | an audit row can never name a different job than its allocation |
| M3 | RESOURCES.allocated kept as a stored derived value | it is locked and checked in one step; an invariant query compares it with the open allocation items |
| M4 | Constraints and triggers | CHECK on allocated; one open allocation and one open attempt per job; faculty-only check; allowed state transitions |
 
Also kept on purpose: jobs.need_class is fixed at submission, so it does not change if the session closes later.
 
Triggers in the design: append-only audit and history tables; the faculty-only check on lab_sessions.faculty_id and problems.created_by; a faculty member's role stays fixed while they have sessions or problems; the state-tracker trigger that allows only listed transitions and writes the history row; the TEST_RESULTS case-belongs-to-problem check.
 
## 12. API and security
 
| Request | Response |
| --- | --- |
| POST /api/auth/login {user_id, password} | 200 {token, role}; 401 login failed |
| POST /api/jobs {workload_type, problem_id, lab_session_id, cpu_millicores, ram_mib, vram_mib, time_limit_s, source} | 201 {job_id, state "queued", need_class}; 400 {field, error}; 401 or 403 |
| GET /api/jobs?owner=me; GET /api/jobs/{id} | 200 list, or one job with its history and verdicts; 403; 404 |
| POST /api/jobs/{id}/cancel | 202 {state "cancelling"}; 409 already finished; 403 not the owner |
| GET /api/resources; GET /api/queue; PUT /api/admin/policy {policy: fcfs or need} | 200 capacity and queue; policy changed (administrator only); 403 for others |
| WebSocket /ws/events | pushes job_state and capacity updates within 2 s |
 
Sample: POST /api/jobs with workload_type code_eval, problem 12, lab session 7, 1000 millicores, 512 MiB, 0 VRAM, 5 s limit and source prog.c returns 201 with job_id 1042, state queued, need_class 1. A bad ram_mib returns 400 with the field name and the allowed range (64 to 117964 MiB). The WebSocket later pushes job 1042 completed with verdict "Accepted 10/10".
 
Security: every API call needs a valid session token; passwords are only salted bcrypt hashes; each engine writes only its own tables and the application database role cannot update capacity or allocations directly; job code runs unprivileged in a sandbox with no network, a read-only root and no extra capabilities, so it cannot reach the database or other jobs; expected outputs stay outside the sandbox. Error codes: 400 with field name, 401 or 403 for login or role, 404 unknown job, 409 cancel of a finished job.
 
## 13. Test plan
 
- Unit tests per engine with pytest.
- E2: arrival order under FCFS and need order under priority; validation batch V1 of 1,000 requests (FR-02, NFR-03).
- E3: two requests for the last free unit, 100 runs, exactly one commits (FR-05; planned file test_e3_allocation.py); a forced failure between update and audit leaves nothing; a repeated release changes nothing (FR-13).
- E1: a 30 s job with a 5 s limit (FR-08, NFR-10); a job using twice its memory; reference set R1 of 20 programs (FR-09, NFR-03).
- E4: a killed worker is marked lost in 6 to 7 s (FR-12) and requeued within 10 s (FR-14, NFR-08); every role against every action (FR-15).
- System runs: B1 and B2 (B2 is the saturation benchmark, run under both policies), F1 and F2, each followed by the invariant query that compares resources.allocated with the open allocation items (NFR-05, NFR-06, NFR-07). The exact definitions of B1, F1 and F2 are in the S0 and S1 material, not restated here.
- Sprint 3 compares measured KPIs with the NFR targets.
Every one of the 15 FRs and 10 NFRs appears in at least one row of the requirements mapping and tests slide of the Sprint 1 deck.
 
## 14. Design decisions and the alternatives rejected
 
| Decision | Alternative | Reason |
| --- | --- | --- |
| Three layers: pages, one application server, database and execution host | Two tiers; one service per engine | clear separation; one server keeps every transaction local; separate services would add network failures on one host |
| One block per engine in the high-level diagram | Layers drawn by function with parts of several engines mixed | guide's review: engines must not be mixed |
| Engines as modules of one process | Four separate programs | hand-over through one database, one transaction per state change; no distributed transactions |
| PostgreSQL as the only shared state | MySQL, SQLite, Redis | row locks, CHECK, triggers, partial unique indexes; SQLite locks the whole database; Redis has no multi-table ACID |
| Pessimistic row locks in resource_id order | Optimistic version check with retry | few hot rows, one clear winner, no retry storms, fixed order prevents deadlock, zero over-allocation required |
| One sandbox container per job (cgroups v2, no network) | Plain process with setrlimit | kernel-enforced CPU, memory and process limits; setrlimit cannot cap the CPU share of a process tree |
| Heartbeat every 2 s, lost after 6 s | 1 s and 3 s; 5 s and 15 s | tolerates one late beat; detection at 6 to 7 s plus requeue fits the 10 s target with little load |
| Stop, confirm, release, then requeue | Release first, then stop | the same capacity can never be given twice |
| FIFO list, min-heap and hash map | One sorted list | O(log n) selection and O(1) lookup; FCFS stays available for the comparison |
| Need class from workload type and session | Priority by who submits | an exam must not wait behind practice work (Gate 1 review, FR-01) |
| REST for requests, WebSocket for live updates (FastAPI) | Polling every second; Flask; Node.js | push meets the 2 s dashboard target without polling load; OpenAPI docs |
| Integer units (millicores, MiB) | Decimal vCPU and GB | no rounding error in capacity sums |
| 10% of every resource kept as a reserve | No reserve | the host itself must stay healthy |
| Files by reference, not blobs in the database | Code and outputs as database blobs | no copying of large data; small database; read-only mount into the sandbox |
| user_id as the single login ID | Separate usn column plus staff ID | one identifier for every user; no empty USN for faculty |
| allocated stored, not computed | Computed on demand | checked and updated under one lock (M3) |
 
## 15. Status, plan and deliverables
 
Stages and gates: S0 Problem Discovery and S1 Requirements Baseline are complete (Gates 0 and 1). Sprint 1 covers S2 and S3 (Weeks 6 to 9) and ends at Gate 2 / Review 3. S4 to S7 follow.
 
| Sprint | Weeks and stage | Goal |
| --- | --- | --- |
| Sprint 1 | Weeks 6 to 9, S2 and S3, ends at Gate 2 | Complete, consistent design of the whole system and every engine, frozen with the guide; one small part started only if the guide approves (proposed: E3) |
| Sprint 2 | Proposed Weeks 10 to 12, S4 | Build each engine against the frozen interfaces with unit tests; E3 first, E2 and E1 in parallel on stubs, then the E4 API, login and pages. Done when one job runs from submission to release |
| Sprint 3 | Proposed Weeks 13 to 15, S5 to S7 | Integration, failure handling and recovery; runs V1, B1, B2, F1, F2 and R1 on the demo machine; corrections and final report |
 
Start order and why: E3 first, because every engine depends on its schema and its allocate and release calls; E4's state model is fixed first because all engines report states through it; E2 and E1 run in parallel against stubs of E3.
 
Gate 2 marking: 20 raw marks scaled to 10 ISA marks, in four areas of 5 marks each: system architecture and engine decomposition; data/state, interface and detailed design; algorithm, data structure and mechanism selection; design modelling, tool usage and traceability. Diagrams must not contradict one another.
 
Where the team stands now:
- Done: complete Sprint 1 design (architecture, interaction, engine architectures, sequence, ER and schema, state, activity, algorithms, API and wireframes, security and tests, requirements mapping, design decisions). Top-down database redesign finished.
- Early implementation of E3 (tables resources, allocations, allocation_items, allocation_audit with the allocate and release transactions): proposed, not started, awaiting guide approval.
- Google Sites portfolio covering work through Sprint 1, following the guide's template: in progress.
Files produced for the team:
- Sprint1_Review_Team10.pptx, 38 slides. Order: 1 title; 2 Sprint 1 plan; 3 High-Level Design divider; 4 overall architecture; 5 engine interaction; 6 lab-exam scenario; 7 Low-Level Design divider; 8 to 12 E1; 13 to 17 E2; 18 to 27 E3 (overview, architecture, database design, ER diagram, relational schema, ER to relational mapping, normalisation, allocate and release, dependencies and failures, complexity and decisions); 28 to 33 E4; 34 and 35 end-to-end sequence; 36 activity diagram; 37 requirements mapping and tests; 38 conclusion.
- ER_Diagram_and_Relational_Schema.docx, 2 pages: the Chen ER diagram with legend and the 15-relation schema.
- The HoD asked for "high level and low level design with suitable scenario" in the Sprint 1 deck. It was handled conservatively: a high-level section (three layers, interaction, one scenario) and a low-level section (inside every engine, the same scenario message by message), with one lab-exam scenario at both levels. Nothing else was restructured.
The lab-exam scenario used everywhere: a student logs in and submits a C program (1 vCPU, 512 MiB, 5 s) while an exam session is open. E2 validates it, assigns job 1042 and queues it with need class 1; in the next 0.5 s cycle E2 admits it; E3 reserves the resources in one locked transaction and writes an audit row; E1 runs it in a sandbox, judges it on 10 test cases and heartbeats every 2 s; E1 asks E3 to release and E3 wakes E2; E4 records every state change and shows Accepted 10/10 within 2 s. Variation: if the heartbeat stops for 6 s, E4 has E1 stop the job, E3 releases, and E2 requeues it (at most twice). Use cases touched: UC-10, UC-01 to UC-09.
 
## 16. Known gaps and outdated material (do not revive)
 
Outdated sources:
- The Gate 2 / Sprint 1 Word document predates the final database design. It shows users with a separate usn column, a login API using {usn, password}, a bottom-up normalisation table, "ON DELETE RESTRICT" on every foreign key, and the older column name jobs.cpu_milli (final name: cpu_millicores). Earlier ER diagrams also showed STUDENT and FACULTY tables, which no longer exist. The final design is section 11, and the Sprint 1 deck already follows it.
- The long top-down redesign document and the earlier Data Requirements document describe the older design.
- Any text with 27 FRs, 15 NFRs or an ID like FR-21 predates the merged baseline.
- A VRAM_POOL table was removed; the schema is authoritative.
Open items:
- The isolation level is not written into any design document. The working assumption is PostgreSQL's default READ COMMITTED, relying on the row locks and the re-check inside the lock (see section 17). It should be added to the E3 material if reviewers ask.
- The SQL implementation's state-change trigger currently only logs changes. It does not yet reject transitions outside the allowed list that the design requires. The SQL files are not stored in this project tab.
- No allocation-transaction latency target exists in the baseline. The nearest approved targets are NFR-01, NFR-07 and NFR-02. Figures such as "about 100 ms per allocation" or "50 simultaneous submissions" that may appear in drafts are proposals only and are not requirements.
- The demo machine is unconfirmed.
## 17. Reviewer questions already raised and the position to take
 
1. FR-05 (atomic allocation with row locks): which isolation level and lock assumptions, and how will deadlocks, partial allocations and the 10% reserve (NFR-06) be verified under high throughput (NFR-02)?
   - In the design: row locks with SELECT ... FOR UPDATE in resource_id order, so no deadlock; the capacity check is repeated inside the lock; the CHECK constraint stops over-allocation even if code is wrong; allocation, items, audit and state change are one transaction, so there are no partial allocations; one open allocation per job through a partial unique index.
   - Assumption to state: PostgreSQL READ COMMITTED. After a row lock is acquired the latest committed values are read, so a stale snapshot cannot cause over-allocation; a stronger isolation level is not needed for correctness and would only add serialisation failures.
   - Verification already planned: the last-unit test (100 runs, exactly one commit), the forced failure between update and audit, the repeated release, and the B1, B2, F1 and F2 runs each ending in the invariant query. Throughput target: NFR-02, at least 50 jobs per minute. Reserve: NFR-06, zero over-allocation and at most 90% allocated.
   - Reasonable additions if asked (proposals, not yet in the design): count deadlock errors in the database log during the runs (expect zero) and record allocation-transaction timings during B2.
2. The S0 benchmark machine (32 vCPU, 128 GB RAM) versus the stale-availability weakness: what quantitative targets, and how is over-allocation shown to be prevented at peak?
   - The hardware figure is the S0 benchmark configuration, not a target. The weakness (availability checked separately, stale at allocation time) is removed by the re-check inside the row lock.
   - Approved targets: scheduling wait at most 2 s and start within 5 s (NFR-01); at least 50 jobs per minute (NFR-02); utilisation at least 70% CPU, 60% RAM and 60% GPU under saturation (NFR-04); zero over-allocation and at most 90% allocated (NFR-06); release within 2 s (NFR-07); failure detected at 6 s and recovered within 10 s (NFR-08).
   - Proof method: the last-unit concurrency test (100 runs, exactly one commit), the B2 saturation run under both policies, and the invariant query after every system run.
## 18. Working conventions
 
- Naming: refer to the project as the Mini Project or by its artefact name. The team no longer uses an earlier working name; do not use it anywhere.
- Style of written deliverables: clean and properly formatted. No AI-style dashes or hyphens as punctuation, no filler, no clutter. Use plain sentences, tables where the reader scans or compares, and numbers written the way the sources write them.
- Baseline discipline: follow the team's modified S1 (FR-01 to FR-15, NFR-01 to NFR-10) plus S0 and the final database design. If an uploaded document conflicts with the latest project changes, ask before assuming.
- Conservative changes: when asked for targeted or visual-only edits, do not change content, diagrams, labels or meaning, and do not add anything new. When feedback is vague (for example the HoD's), interpret it, say how, and apply the smallest change that satisfies it.
- Terminology to keep exact: E1 to E4 (no E5); Job ID; resource vector (cpu_millicores, ram_mib, vram_mib); need class; admission cycle (0.5 s); terminal event; stop and confirm; reserve (10%); invariant query; user_id (never usn); queued, admitted, allocated, running, completed, failed, cancelled, released.
- Sprint 1 deck format (copied from the team's S1 Final PPT Ver 2): slide size 20 by 11.25 in; headings Times New Roman Bold 45 pt; subtitles 27 pt navy 1F3A68; body Times New Roman; table header fill 4472C4 with zebra rows E9EEF7; footer "Department of Computer Science and Engineering" with slide number; KLE Tech banner. The per-engine slide flow follows the Engine Architecture reference deck (overview with purpose, responsibilities, inputs, outputs; architecture and technology; internal working; dependencies and failure behaviour; complexity and design decisions). That reference deck belongs to another team (a library inventory project): use its flow only, never its content.
- Engine colours in diagrams: E1 green, E2 blue, E3 amber, E4 purple.