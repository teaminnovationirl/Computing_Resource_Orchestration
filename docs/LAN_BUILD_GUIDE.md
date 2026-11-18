# Wire five laptops to one sandboxed judge

Build the project as the Stage 2 design already describes it, then add only what the LAN and a real sandbox force. That means one FastAPI process and PostgreSQL 16 inside an Ubuntu 24.04 VM, with engines E1 to E4 as modules, the 15-table schema and the 8-state job machine. **Three changes carry most of the new work.** First, the per-job Docker container gives way to **IOI isolate boxes** behind the existing SandboxRunner interface, because isolate is the CMS standard, runs on cgroup v2 only and writes a meta file that maps almost one to one onto AC, WA, TLE, MLE, OLE and RE. Second, each laptop gets a small **Python lab agent**. It discovers the server over mDNS or UDP broadcast, holds a length-prefixed JSON session on TCP 7070, sends a heartbeat every 2 s and submits from the terminal. Browsers keep HTTPS REST and WSS on port 8443. Third, the VM sits on a **team-owned router with client isolation off and a bridged adapter**, because campus Wi-Fi isolation is the most likely way to lose the demo. Transaction safety stays where Stage 2 put it: row locks taken in resource_id order, a CHECK constraint as the backstop, a refusal path that commits an audit row (D-01), SKIP LOCKED in the liveness sweep, and a pg_notify trigger that feeds the dashboard. Execution stays on a single node, so the out-of-scope line Stage 0 drew holds. A killed worker is detected at 6 to 7 s and requeued at about 8 s, inside NFR-08. The plan opens with three spike days that settle the riskiest unknowns: whether isolate works in the VM, whether the laptops can reach it, and whether the schema runs. A thin end-to-end path follows by the end of Week 10, the recovery path by Week 12, and measured KPIs in Week 14. Claude Code runs Sonnet by default, uses Opus only for contract sessions, and works from spec files, short per-engine CLAUDE.md files and one worktree per owner. This guide records thirteen new decisions, D-18 to D-30. It confirms or extends the earlier D-01 to D-17 and contradicts none of them, apart from swapping the D-09 Docker implementation.

## Thirteen decisions extend Stage 2 without reopening it

The precedence rule follows the baseline. S1 requirement IDs and targets come first (FR-01 to FR-15, NFR-01 to NFR-10, UC-01 to UC-10). S2P naming and schema facts come next: user_id rather than usn, cpu_millicores rather than cpu_milli, and per-FK delete rules. The earlier guide's concrete decisions D-01 to D-17 follow, and this guide's D-18 to D-30 add to them ([Stage 2 deck](file:///mnt/user-data/uploads/1c294ae2-Stage2_PPT.pptx); [earlier guide](https://claude.ai/artifact/G17ngUSQWSuwFvTpU6QFRE)). Where Stage 2 says "a container per job", this guide's isolate boxes still meet the stronger statement in the Stage 2 architecture text: **one sandbox per job, cgroups v2, no network, unprivileged user, read-only system files** ([Stage 2 handbook](file:///mnt/user-data/uploads/f9bb8033-Stage2_Document.docx)).

### Engine owners and what each now builds

| Engine | Owner (Stage 2 label) | Course focus | Baseline scope (unchanged) | New work from this guide |
|---|---|---|---|---|
| E1 Process Execution & Control | Abhishek Kuri (Student D) | OS | FR-07 to FR-10, NFR-02, NFR-03, NFR-10 | IsolateRunner, verdict classifier, CgroupRunner for synthetic loads |
| E2 Workload Scheduling | Sudhanv Kulkarni (Student C) | DSA, DMS | FR-01 to FR-03, NFR-01, NFR-03, NFR-04 | Load generator, synthetic workload scripts, benchmark driver |
| E3 Transaction-Safe Allocation | Sagar Narasannavar (Student A) | DBMS | FR-05, FR-06, FR-13, NFR-05 to NFR-07 | NOTIFY triggers, KPI SQL, VM and DB provisioning scripts |
| E4 Monitoring & Recovery | Aniruddh Naik (Student B) | WT, CN | FR-04, FR-11, FR-12, FR-14, FR-15, NFR-05, NFR-08, NFR-09 | Agent gateway (TCP 7070), discovery, lab agent, presence tiles |

### D-18 Stack: keep Python and FastAPI in one process

**Context.** Stage 2 Annex A already picked REST plus WebSocket with FastAPI over Flask and Node.js, with PostgreSQL as the only shared state. The earlier guide then wrote all four engines in Python 3.11+ with psycopg 3, raw SQL, pydantic v2 and `uvicorn --workers 1` (D-16). A friend's project used Spring Boot. The load is small: NFR-02 asks for 50 jobs per minute, under one job per second, from four or five laptops.

**Options.** Spring Boot, FastAPI, Go with chi, NestJS, Rust axum. The team scored them on its own judgement; the matrix in the research notes is reasoning, not measured data. Spring Boot scored best on declarative transactions and STOMP WebSockets and worst on keeping fundamentals visible. FastAPI scored best on learning curve, subprocess and `/proc` access, and speed to a demo.

**Decision.** Keep **Python 3.12 (3.11+ acceptable) with FastAPI, one uvicorn worker, psycopg 3 and raw SQL**. Write the lab agent in Python too, so server, agent and tests share one language and one set of pydantic message models.

**Consequences.** A single async worker can serve thousands of WebSocket connections as long as nothing blocks the event loop, and in-process state is correct only with one worker. Multiple workers would need Redis pub/sub ([websocket.org FastAPI guide](https://websocket.org/guides/frameworks/fastapi/)). D-16 therefore becomes load-bearing: the in-memory WebSocket hub and agent presence map are both correct because there is exactly one process. Compilation and test runs happen in isolate child processes. Blocking engine calls from async handlers go through `asyncio.to_thread`. Spring Boot is rejected because its in-memory STOMP broker targets one instance and needs an external broker to scale ([websocket.org Spring Boot guide](https://websocket.org/guides/frameworks/spring-boot/)). The deeper reason is that `@Transactional` and JPA locking hide the explicit `SELECT ... FOR UPDATE` that the DBMS viva has to show. Switching would also throw away the earlier guide's code-level design.

### D-19 Sandbox: isolate for untrusted code, an own cgroup runner for trusted load

**Context.** Stage 2 and D-09 planned one Docker container per job behind a SandboxRunner interface. The flags were `--cpus`, `--memory` equal to `--memory-swap`, `--pids-limit 64`, `--network none`, `--read-only`, `--cap-drop ALL`, `no-new-privileges` and user 65534. A Codeforces-style verdict needs CPU time, wall time, peak memory, an OOM flag and the terminating signal for every test case. Current isolate "requires v2", sets per-box CPU, wall, memory and process limits, gives the box only a loopback interface unless `--share-net` is passed, and writes a meta file with `time`, `time-wall`, `max-rss`, `cg-mem`, `cg-oom-killed`, `exitsig`, `killed` and `status` values RE, SG, TO and XX. Its man page also states that running isolate inside containers is "not recommended" ([isolate man page source](https://github.com/ioi/isolate/blob/master/isolate.1.txt)). Judge0 1.13.1 still expects cgroup v1 and needs a GRUB change to run on cgroup v2 hosts ([self-hosting write-up](https://hashnode-hub.vercel.app/posts/68506cdae5e5dd22feb6abe6)). In 2024 it shipped three sandbox escapes rated CVSS 9.1 to 10.0, made worse by a privileged container ([The Hacker News](https://thehackernews.com/2024/04/sandbox-escape-vulnerabilities-in.html)).

**Options.** Docker per job (the Stage 2 plan); isolate; nsjail; an own fork/setrlimit/cgroup runner only; self-hosted Judge0.

**Decision.** **IsolateRunner** implements SandboxRunner for every piece of untrusted code: the compile step and every test case. **CgroupRunner** is E1's own runner. It writes cgroup v2 files directly and runs only the team's trusted synthetic workloads (cpu_bound, mem_bound, mixed), which need a multi-core `cpu.max` quota that isolate does not offer as a flag. **DockerRunner** from the earlier guide stays as the fallback if isolate fails the day-2 check. Judge0 is rejected.

**Consequences.** Verdicts come straight from the meta file, MLE becomes a first-class verdict, and the VM needs no Docker daemon. isolate needs a systemd-delegated cgroup subtree (`isolate.service` running `isolate-cg-keeper`) and Linux 5.19 or newer for memory reporting ([isolate man page source](https://github.com/ioi/isolate/blob/master/isolate.1.txt); [isolate-cg-keeper(8)](https://www.ucw.cz/isolate/isolate-cg-keeper.8.html)). The FR-04 health guard switches from "Docker not answering" to "isolate probe box fails". The earlier guide's rule never to infer MLE from exit 137 alone carries over as "trust `cg-oom-killed`". The OS story stays visible because CgroupRunner shows fork, setpgid, setrlimit, `cgroup.procs`, `cpu.max`, `memory.max`, `pids.max` and `cgroup.kill` in the team's own code.

### D-20 Client design: browser for the UI, a lab agent for the network

**Context.** The brief needs a client program on four or five laptops. The laptops probably run Windows, and both isolate and DMOJ grade only on Linux ([DMOJ judge](https://github.com/dmoj/judge); [ioi/isolate](https://github.com/ioi/isolate)). Browsers cannot do mDNS, UDP broadcast or raw TCP. The WT marks need pages and the CN marks need networking you can see.

**Options.** Browser only; a desktop app (Electron, Tauri, JavaFX); a CLI agent only; laptops as remote workers; a hybrid.

**Decision.** Hybrid. The earlier guide's **plain HTML/CSS/JS pages** (index, submit, jobs, job, monitor, admin), served by FastAPI, remain the whole UI. A **lab agent** on each laptop discovers the server, logs in, holds a TCP session with 2 s heartbeats so the faculty dashboard shows each laptop online or offline, accepts `submit` and `cancel` commands at a console prompt, prints live verdict events and opens the browser at the discovered URL. **Laptops never execute jobs.**

**Consequences.** Browser submissions and agent submissions enter E4 by different routes but must reach the same E2 validation (message 2), so validation lives in E2 only. The agent is optional for students, so a broken agent cannot block the demo. Packaging the agent as a single `.exe` with PyInstaller is unverified; the fallback is to run it from source with Python installed. For the code editor, use a plain `<textarea>` or a locally vendored CodeMirror build, because the demo LAN has no internet and a CDN-hosted Monaco would fail (this vendoring is unverified). React plus Vite was considered and rejected: it adds a build step the earlier guide deliberately avoided.

### D-21 Protocol: HTTPS and WSS for pages, framed TCP for agents

**Context.** Stage 2 message 1 is REST over HTTPS and message 12 is WebSocket push within 2 s. The earlier guide put TLS on port 8443. The agent link and discovery are new.

**Options.** For the agent link: REST polling, WebSocket, gRPC streaming, or custom framed TCP. For discovery: manual IP, mDNS/DNS-SD, or UDP broadcast.

**Decision.** Pages keep **HTTPS REST and WSS on 8443** (`/ws/events`, authenticated by the first message per D-15). Agents use **framed JSON over TCP 7070**: a 4-byte big-endian length followed by a UTF-8 JSON object. Discovery tries **mDNS service `_labjudge._tcp.local.` through python-zeroconf first, then a UDP broadcast on 7071, then a manually typed IP**. The agent link becomes a new **message 13 (Agent and E4, both directions)**, numbered after the twelve Stage 2 messages so none of them change. gRPC is rejected because protobuf code generation and HTTP/2 hide the framing the CN rubric rewards.

**Consequences.** E4 owns the gateway, so framing, partial reads, keep-alive, reconnect with backoff, and multicast against broadcast against unicast all become things the team can demonstrate. Discovery only works if the VM is bridged: under VirtualBox NAT "the guest does not reliably receive UDP broadcasts" ([VirtualBox 7.0 manual](https://docs.oracle.com:443/en/virtualization/virtualbox/7.0/user/networkingdetails.html)). The agent link runs as plaintext in Sprint 2 and gets wrapped in TLS in Sprint 3 (D-27).

### D-22 Single execution node: the server VM

**Context.** Stage 0 put "multi-node cluster and distributed transactions" out of scope, and the Stage 2 execution host is one server. The research proposed a `node` table, remote workers and leases. That would earn CN marks, but at a cost: verdict times would vary between laptops, laptops sleep and roam networks, and Windows cannot run isolate anyway.

**Options.** A single node; a full laptop worker pool; a hybrid where the VM judges graded work and laptops run practice jobs.

**Decision.** **One execution node, the server VM.** All isolate boxes run there, the `resources` rows describe that VM, and no `node` or `node_resource` table is added.

**Consequences.** Verdict timing stays on uniform hardware, which protects NFR-03. FR-07 dispatch and FR-12 and FR-14 recovery are real at the process level: worker child processes, supervisor threads and isolate boxes are actually killed and recovered. The networked heartbeat story moves to the agents. Remote Linux workers become named future work: they could speak the same framed protocol through the same SandboxRunner seam without touching the schema.

### Smaller decisions D-23 to D-30

| ID | Decision | Relation to baseline | Reason |
|---|---|---|---|
| D-23 | Keep the 8 states and 13 transitions. Add no TERMINATING or RETRYING state. "Stopping" lives on the attempt (open attempt, liveness row), and the API's 202 `{state: "cancelling"}` is a derived label. | Rejects a research proposal | Stage 2 already writes completed, failed or cancelled only after E1 confirms the stop, and `released` proves the release. The trigger and invariants are already written and tested. |
| D-24 | Per-case verdicts become AC, WA, TLE, MLE, OLE, RE. CE and IE stay at attempt level. Add `problems.memory_limit_mib` (default 256), used as the per-case memory cap. For code_eval, `jobs.ram_mib` must be at least that cap. | Extends D-05 to memory. Widens the test_results CHECK and adds one column; the table count stays 15. | Codeforces-style MLE and OLE are expected by students and evaluators. Memory now follows the same split as time: the problem limit gives a verdict, the job limit gives a failure. |
| D-25 | AFTER triggers on `jobs` and a statement trigger on `resources` call `pg_notify`. E4's hub LISTENs and fans out to WSS and to agent EVENT frames. | Extends the hub feed; `job_state_track` is unchanged | Every committed change, including those from the reconciler or psql, reaches the dashboard in commit order. |
| D-26 | The demo VM uses `CAPACITY_PROFILE=host`, which reads nproc and MemTotal and subtracts a system share. `GPU_MODE=none` unless KVM passthrough works. The S0 profile is used only in tests. | Uses the earlier guide's profiles | "Real, not simulated" requires real capacity rows. |
| D-27 | Replace the bare self-signed certificate with an mkcert local CA certificate for the VM IP. Copy `rootCA.pem` to the laptops. Add TLS to the agent link in Sprint 3. | Extends the earlier guide's TLS on 8443 | Browsers trust the certificate, so there is no warning page on demo day. |
| D-28 | The agent logs in through `POST /api/auth/login` and sends that token in HELLO. Presence lives in E4 memory. No new table. | Reuses `login_sessions` | Presence is soft state, and the 15-table BCNF story stays intact. |
| D-29 | PostgreSQL 16 is installed natively in the VM and bound to 127.0.0.1. Docker is used only on development laptops and for the DockerRunner fallback. | Narrows "Postgres in Docker" to development only | One less layer in the VM, and port 5432 is never on the LAN. |
| D-30 | Worker heartbeat stays at 2 s, lost at 6 s, sweep every 1 s. The sweep uses `FOR UPDATE SKIP LOCKED`. | Keeps Stage 2 and D-08 | NFR-08 literally says "after 6 s", and the acceptance window is 6 to 7 s. The research's 1.5 s / 5 s / 0.5 s timing would detect earlier than the requirement allows. |

**D-01 should be confirmed as written.** Allocation re-checks capacity before writing anything, so the refusal path can write the `refused` audit row and the admitted to queued transition in the same transaction and commit it. The Stage 2 pseudocode ("ROLLBACK; audit refused") could never keep that audit row.

### Status of the earlier decisions

| Earlier decision | Status under this guide |
|---|---|
| D-01 refusal path | Recommend confirming; SQL below |
| D-03 admit until nothing fits; D-13 FIFO by (submitted_at, job_id) | Kept |
| D-04 never-started recovery via allocated to cancelled | Kept in reconciliation |
| D-05 per-case time limit gives TLE, job limit gives failure | Kept; D-24 applies the same split to memory |
| D-06 sandbox start failure ends cancelled, no requeue | Kept; an isolate `--init` failure counts as a start failure |
| D-07 worker is a child process; D-08 the alive to closed/lost race decides the owner | Kept; D-30 adds SKIP LOCKED to the sweep |
| D-09 Docker CLI behind SandboxRunner | Interface kept; implementation superseded by D-19, Docker becomes the fallback |
| D-12 READ COMMITTED; D-14 multipart part `source`; D-15 WS auth by first message; D-16 one uvicorn worker; D-17 seed | Kept |
| D-02, D-10, D-11 | Untouched |

## One VM, three listeners, five laptops: the end-to-end architecture

The system has one server process with three network listeners, and one database that holds all shared state. This is the shape CMS uses at the IOI: a web front end, a queue that "dispatches jobs to workers", sandboxed workers and a heartbeat checker, with "the state of the contest wholly kept on a PostgreSQL database" so services can restart without losing consistency ([CMS documentation](https://cms.readthedocs.io/en/v1.5/Introduction.html)).

```
  Team router: SSID LABJUDGE, client isolation OFF, subnet 192.168.50.0/24
  |
  +-- Laptop 1..5 (Windows or Linux)
  |     browser  --- HTTPS REST + WSS :8443 ------------------+
  |     lab agent --- mDNS :5353 / UDP :7071 discovery -------+
  |               --- framed JSON over TCP :7070 (msg 13) ----+
  |                                                           |
  +-- Workstation (wired) -> hypervisor (KVM or VirtualBox)   |
        +-- Ubuntu 24.04 VM, bridged, fixed IP 192.168.50.10 <-+
              uvicorn, ONE process (D-16)
                E4: REST, WSS hub, agent gateway, discovery, liveness sweep, recover()
                E2: admission loop thread (every 0.5 s and on capacity changed)
                E3: allocate / release transactions
                E1: supervisor threads -> worker child processes -> isolate boxes 0..N-1
              PostgreSQL 16 on 127.0.0.1:5432 (LISTEN job_events, capacity)
```

### Who does what

| Component | Runs on | Responsibilities | Never does |
|---|---|---|---|
| Browser pages | Laptops | Login, problem list, submit (multipart `source`), my jobs, job detail with per-case verdicts, faculty monitor, admin policy switch | Hold state beyond the session token |
| Lab agent | Laptops | Discover server, log in over HTTPS, HELLO with token, heartbeat every 2 s, console `submit`, `cancel`, `jobs`, print EVENTs, open browser, reconnect with backoff | Compile or run code; talk to PostgreSQL |
| E4 gateway and hub | VM | Authenticate HELLO, track presence, mark agents offline after 6 s, route SUBMIT to E2, push EVENTs, fan out NOTIFY to WSS by role | Change job state outside `transition()` |
| E2 scheduler | VM | Validate, assign Job ID, need_class, FIFO plus heap plus map, FCFS or need policy with 60 s guard, admit | Preempt, or change declared resources |
| E3 allocator | VM | Snapshot, atomic allocate and release, best-fit GPU, audit, capacity changed | Allocate from the 10% reserve, or release before a confirmed stop |
| E1 executor | VM | Supervisor thread per job, worker child per job, isolate compile and tests, verdicts, job timer, stop and confirm | Start without an allocation, or run a second attempt before the first is confirmed stopped |
| PostgreSQL | VM | All durable state, triggers, CHECKs, NOTIFY | Listen on the LAN |

### The life of one submission

A student submits `prog.c` for problem 12 in exam session 7, either from the browser or by typing `submit prog.c 12 7` into the agent. E4 authenticates the request and passes it to E2 (message 2). E2 validates it, inserts the job as `queued` with need_class 1 because the exam is open, and the `jobs` insert trigger NOTIFYs the hub. Within 0.5 s the admission cycle reads E3's snapshot (message 3), finds the job fits and calls `admit` (message 4). E3 locks the resource rows, commits the allocation with its audit row and the admitted to allocated transition, and only after commit hands the job to E1 (message 5). E1 starts a supervisor thread, which forks a worker child; once the child is up, the supervisor moves the job to `running` (the child itself never touches the database, per D-07). The child compiles inside box *B*, runs each case, prints `{"t": "beat"}` every 2 s (message 6), and reports per-case results to the supervisor. E1 writes `test_results`, closes the attempt, moves the job to `completed` with a headline such as "Wrong Answer on test 4 (7/10)", and calls release (message 7). E3 commits `released` and signals capacity changed (message 8), which starts another admission cycle. Every state change NOTIFYs the hub, which pushes it to the student's WSS connection and agent session and to the faculty monitor, inside the 2 s NFR-09 budget.

### Repository layout

Keep the earlier guide's server package names if its section 3 tree differs from the folder names below. The new top-level folders are `agent/`, `protocol/` and `deploy/`.

```
repo/
  CLAUDE.md                      shared rules, under 80 lines (template in the Claude Code section)
  .claude/settings.json          model, permissions, project-scope plugins
  .claude/agents/explore.md      cheap read-only Explore override (model: haiku)
  docs/specs/                    one spec per slice, written before code
  docs/protocol.md               message 13 contract: framing, types, examples
  docs/adr/                      D-18 .. D-30 as one file each
  db/                            roles.sql, schema.sql, triggers.sql, seed.sql, invariants.sql, kpi.sql, migrations/
  protocol/                      pydantic models shared by server and agent (one source of field names)
  server/app/
    common/                      tx(engine), after_commit, Clock, ports.py (E1Port..E4Port), logging
    e1_exec/    CLAUDE.md        supervisor, worker_child, isolate_runner, cgroup_runner, docker_runner, verdict, checker
    e2_sched/   CLAUDE.md        validation, JobQueue, select_fcfs, select_need, AdmissionLoop
    e3_alloc/   CLAUDE.md        snapshot, allocate, release, placement
    e4_monitor/ CLAUDE.md        auth, transition, liveness, recover, health, hub, gateway, discovery
    api/                         FastAPI routers (E4 owns)
    static/                      HTML, CSS, JS pages (no build step)
  agent/        CLAUDE.md        labagent.py, discovery.py, framing.py (imports protocol/)
  workloads/                     synthetic cpu_bound / mem_bound / mixed scripts (E2), reference programs R1 (E1)
  bench/                         load generator, chaos scripts, report.py
  deploy/                        vm_setup.sh, isolate check, ufw rules, mkcert steps, systemd units, run.sh
  tests/                         e1/ e2/ e3/ e4/ system/  (pytest markers: sandbox, lan)
```

## A 4-byte length prefix carries every agent message

TCP delivers a byte stream, not messages. One `recv` can return half a frame or two frames, which is the point the CN viva will probe. The framing below fixes message boundaries explicitly, rejects oversized frames, and uses `readexactly` so the code itself shows how partial reads are handled.

```python
# protocol/framing.py  (shared by server gateway and agent)
import asyncio, json, struct

MAX_FRAME = 1 << 20                      # 1 MiB; source files are capped at 64 KiB by E2 anyway

async def read_frame(reader: asyncio.StreamReader) -> dict:
    header = await reader.readexactly(4)              # loops internally until 4 bytes arrive
    (length,) = struct.unpack("!I", header)           # network byte order, unsigned 32-bit
    if not 0 < length <= MAX_FRAME:
        raise ValueError(f"bad frame length {length}")
    return json.loads((await reader.readexactly(length)).decode("utf-8"))

async def write_frame(writer: asyncio.StreamWriter, msg: dict) -> None:
    body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    writer.write(struct.pack("!I", len(body)) + body)
    await writer.drain()                              # back-pressure: wait if the socket buffer is full
```

Every frame carries an envelope `{"v": 1, "type": ..., "seq": n, "ts": ...}`. Here `seq` increases per connection and `ts` is informational only, because all lease arithmetic uses the server clock.

### Message 13 types

| Type | Direction | Body fields | Server behaviour |
|---|---|---|---|
| HELLO | agent to server | `token, machine_id, hostname, agent_version` | Must arrive within 5 s of connect, or the server closes the socket (mirrors the WS 4408 rule). Token checked against `login_sessions` by hash. |
| WELCOME | server to agent | `session_id, user_id, role, hb_interval_ms: 2000, lost_after_ms: 6000, server_time` | Presence entry created; faculty monitor gets a `presence` event |
| HB | agent to server | `seq, cpu_pct, mem_pct` | Updates last_seen with the server clock; replies HB_ACK |
| HB_ACK | server to agent | `seq, server_time` | Lets the agent detect a dead server and estimate clock offset |
| SUBMIT | agent to server | `client_req_id, workload_type, problem_id, lab_session_id, cpu_millicores, ram_mib, vram_mib, time_limit_s, filename, source` | Calls the same E4 to E2 path as `POST /api/jobs` |
| ACCEPTED | server to agent | `client_req_id, job_id, state: "queued", need_class` | Same content as the 201 response |
| REJECTED | server to agent | `client_req_id, field, error` | Same content as the 400 response |
| CANCEL | agent to server | `job_id` | Same role checks as `POST /api/jobs/{id}/cancel`; answers CANCEL_ACK or ERROR 403/409 |
| EVENT | server to agent | `type: "job_state", job_id, state, at, verdict` | Only the agent user's own jobs (FR-15) |
| ERROR | server to agent | `code, error` | Codes mirror HTTP: 400, 401, 403, 409 |
| BYE | either | `reason` | Clean close; presence goes offline at once |

Three idempotency rules keep retries harmless. The agent stamps every SUBMIT with a fresh `client_req_id`. The server keeps a per-user map from recent `client_req_id` to `job_id`, so a SUBMIT resent after a reconnect returns the original ACCEPTED instead of creating a duplicate job. The map lives in memory and resets on restart, which is acceptable because restarts are rare and visible. EVENTs are full state snapshots, not deltas, so a duplicate or reordered event cannot corrupt the agent's view. On reconnect the agent sends HELLO and then asks for current state rather than replaying old events, because NOTIFY delivers nothing durable to catch up from ([PostgreSQL NOTIFY](https://www.postgresql.org/docs/current/interactive/sql-notify.html)).

### Session lifecycle and presence

The agent connects, sends HELLO, receives WELCOME and then sends HB every 2 s. A 1 s sweep over E4's in-memory presence map marks an agent `offline` when no frame has arrived for 6 s, mirroring the worker numbers so one rule explains both. TCP keepalive can be switched on but is not relied upon: operating-system default keepalive timers are far longer than 6 s, so the application heartbeat is the real failure detector. If HB_ACKs stop for 6 s, or the socket errors, the agent reconnects with exponential backoff (1, 2, 4, 8 s, capped at 8) and runs discovery again after three failures.

The gateway lives inside the FastAPI process, started from the lifespan hook. Engine calls are synchronous (psycopg pool, threads), so the async handler hands them to a thread:

```python
# server/app/e4_monitor/gateway.py
@asynccontextmanager
async def lifespan(app):
    gw = await asyncio.start_server(handle_agent, host="0.0.0.0", port=7070)
    disco = await start_discovery(https_port=8443, gw_port=7070)   # zeroconf + UDP 7071 responder
    listener = start_notify_listener(hub)                           # D-25, thread with its own connection
    yield
    gw.close(); await gw.wait_closed(); await disco.close(); listener.stop()

async def handle_agent(reader, writer):
    hello = await asyncio.wait_for(read_frame(reader), timeout=5)
    user = await asyncio.to_thread(auth.user_for_token, hello["token"])   # 401 -> ERROR + close
    ...
    while True:
        msg = await asyncio.wait_for(read_frame(reader), timeout=6)       # silence = offline
        presence.touch(user, hello["machine_id"])
        if msg["type"] == "SUBMIT":
            result = await asyncio.to_thread(e4.submit_from_agent, user, msg)  # same path as REST
            await write_frame(writer, result)
```

### Discovery: multicast first, broadcast second, unicast always

The server registers `_labjudge._tcp.local.` with python-zeroconf, a pure-Python mDNS implementation whose `register_service` probes for name conflicts and whose `ServiceBrowser` fires callbacks as services appear and disappear ([python-zeroconf API](https://python-zeroconf.readthedocs.io/en/latest/api.html)). The TXT properties carry `https=8443`, `gw=7070` and `v=1`. `ServiceInfo.addresses` returns IPv4 only; use `parsed_addresses()` for anything else ([python-zeroconf API](https://python-zeroconf.readthedocs.io/en/latest/api.html)). If nothing answers within 2 s, the agent sets `SO_BROADCAST` and sends `LABJUDGE?1` to `255.255.255.255:7071`. The server's asyncio datagram endpoint replies by unicast with `{"host": "192.168.50.10", "https": 8443, "gw": 7070}`. After three failed tries the agent asks for the IP, which is printed on a slide. All three paths need a bridged VM and a network without client isolation. Whether Windows prompts about UDP 5353, and how python-zeroconf coexists with Windows' own mDNS responder, are **unverified** and must be tested on the actual laptops in the spike days.

### HTTP additions

The REST surface stays the Stage 2 API (S2P naming). This guide adds `GET /api/health`, which is unauthenticated and returns version and admission state for connectivity checks, and `GET /api/agents`, which returns the presence list for faculty and administrators only. The WSS hub gains two event types: `presence` (faculty and administrator) and `worker` (liveness tiles).

## Row locks, a CHECK and one trigger make over-allocation impossible

E3's guarantees come from three layers that each work alone. The application re-checks capacity under a row lock. A CHECK constraint rejects any write that would exceed `total - reserved`. Locks are always taken in `resource_id` order, which breaks circular wait (the OS viva can name the Coffman condition). The isolation level is READ COMMITTED (D-12). Under READ COMMITTED, a `SELECT ... FOR UPDATE` that meets a locked row waits and then re-reads the committed version, so "lock, then decide" is safe without SERIALIZABLE and never raises 40001. That re-check behaviour is background knowledge from the research and is **unverified against the PostgreSQL "Explicit Locking" chapter**, which could not be fetched; check it before quoting it in the viva. The `tx()` helper still retries 40P01 and 40001 up to 3 times, matching the documented advice to "retry the complete transaction, including all logic that decides which SQL to issue" ([PostgreSQL serialization failure handling](https://www.postgresql.org/docs/18/mvcc-serialization-failure-handling.html)).

### The resources table and its backstop

This is the shape of the earlier guide's `resources` definition. That guide's `schema.sql` is authoritative for exact types.

```sql
CREATE TABLE resources (
  resource_id  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind         text    NOT NULL CHECK (kind IN ('cpu', 'ram', 'gpu')),
  gpu_index    integer,
  total        bigint  NOT NULL CHECK (total > 0),          -- millicores or MiB
  reserved     bigint  NOT NULL,
  allocated    bigint  NOT NULL DEFAULT 0,
  CONSTRAINT reserve_is_10pct   CHECK (reserved = (total + 9) / 10),
  CONSTRAINT no_over_allocation CHECK (allocated BETWEEN 0 AND total - reserved),
  CONSTRAINT gpu_index_iff_gpu  CHECK ((kind = 'gpu') = (gpu_index IS NOT NULL)),
  UNIQUE NULLS NOT DISTINCT (kind, gpu_index)
);
-- Demo (as superuser, since orch_app cannot update capacity directly):
-- UPDATE resources SET allocated = total WHERE kind = 'cpu';
-- ERROR: new row ... violates check constraint "no_over_allocation"   (SQLSTATE 23514)
```

`allocated >= 0` also catches a double release. `allocated` is the one deliberate stored derived value (M3 in S2P). It is guarded by `invariants.sql`, which must return zero rows after every run.

### Allocation transaction (E3, message 4 in, message 5 out)

```sql
BEGIN;                                             -- READ COMMITTED (D-12)
SET LOCAL ROLE e3_role;
SELECT set_config('app.actor', 'E3', true);
SELECT set_config('app.reason', 'allocation committed', true);

-- 1. One lock statement, fixed order (hard invariant 2)
SELECT resource_id, kind, gpu_index, total, reserved, allocated
FROM resources
WHERE resource_id = ANY(%(candidate_ids)s)          -- cpu row, ram row, every gpu row if vram_mib > 0
ORDER BY resource_id
FOR UPDATE;

-- 2. In Python, on the locked rows: allocated + need <= total - reserved for cpu and ram;
--    best-fit GPU = least free VRAM that still fits, ties to lower resource_id.
--    Any miss: take the refusal path below. Nothing has been written yet.

-- 3. Apply; the CHECK is the backstop if step 2 is wrong
UPDATE resources SET allocated = allocated + %(cpu_millicores)s WHERE resource_id = %(cpu_id)s;
UPDATE resources SET allocated = allocated + %(ram_mib)s        WHERE resource_id = %(ram_id)s;
-- plus the chosen GPU row when vram_mib > 0

INSERT INTO allocations (job_id, status) VALUES (%(job_id)s, 'open')
RETURNING allocation_id;                            -- partial unique index: one open per job
INSERT INTO allocation_items (allocation_id, resource_id, amount)
VALUES (%(a)s, %(cpu_id)s, %(cpu_millicores)s), (%(a)s, %(ram_id)s, %(ram_mib)s);
INSERT INTO allocation_audit (job_id, allocation_id, action, outcome)
VALUES (%(job_id)s, %(a)s, 'allocate', 'committed');

UPDATE jobs SET state = 'allocated' WHERE job_id = %(job_id)s;   -- via transition(); trigger checks admitted -> allocated
COMMIT;
-- after_commit: E1.dispatch(job_id, allocation_id, limits, file_ref)   (nothing leaves while locks are held)
```

```sql
-- Refusal path (D-01): same transaction, still holding the locks, nothing else written
SELECT set_config('app.reason', 'refused: capacity taken first', true);
INSERT INTO allocation_audit (job_id, allocation_id, action, outcome)
VALUES (%(job_id)s, NULL, 'allocate', 'refused');
UPDATE jobs SET state = 'queued' WHERE job_id = %(job_id)s;      -- admitted -> queued, submitted_at unchanged
COMMIT;                                                           -- returns REFUSED; E2 keeps its place
```

### Release transaction (messages 7 and 10 in, message 8 out)

```sql
BEGIN;
SET LOCAL ROLE e3_role;
SELECT set_config('app.actor', 'E3', true);
SELECT allocation_id FROM allocations
WHERE job_id = %(job_id)s AND status = 'open'
FOR UPDATE;                                         -- 0 rows: COMMIT and return ALREADY_RELEASED (UC-04)

SELECT resource_id FROM resources
WHERE resource_id IN (SELECT resource_id FROM allocation_items WHERE allocation_id = %(a)s)
ORDER BY resource_id
FOR UPDATE;                                         -- same global order as allocate

UPDATE resources r SET allocated = r.allocated - ai.amount
FROM allocation_items ai
WHERE ai.allocation_id = %(a)s AND r.resource_id = ai.resource_id;
UPDATE allocations SET status = 'released', released_at = clock_timestamp() WHERE allocation_id = %(a)s;
INSERT INTO allocation_audit (job_id, allocation_id, action, outcome)
VALUES (%(job_id)s, %(a)s, 'release', 'committed');
UPDATE jobs SET state = 'released' WHERE job_id = %(job_id)s;     -- completed/failed/cancelled -> released
COMMIT;
-- after_commit: E2.capacity_changed()   (message 8 starts an admission cycle)
```

### SKIP LOCKED where waiting would be wrong

E2's queue lives in memory (FIFO, min-heap keyed by need_class, submitted_at and job_id, and a hash map), so the job queue does not need SKIP LOCKED. The liveness sweep does. E1 may be closing an attempt (alive to closed) at the same moment E4 decides it is lost (alive to lost), and D-08 says whichever transition commits first owns the attempt. With SKIP LOCKED the sweeper never waits behind E1. It skips the row and sees `closed` on the next sweep. This matches the general pattern in which "rows that cannot be locked immediately are skipped", which is exactly what a work-claiming query wants ([PgQueuer SKIP LOCKED reference](https://janbjorge.github.io/pgqueuer/reference/skip-locked/)).

```sql
-- E4 sweep, every 1 s
BEGIN;
SET LOCAL ROLE e4_role;
SELECT wl.attempt_id, a.job_id
FROM worker_liveness wl JOIN attempts a USING (attempt_id)
WHERE wl.status = 'alive'
  AND wl.last_beat_at < now() - interval '6 seconds'
ORDER BY wl.attempt_id
FOR UPDATE OF wl SKIP LOCKED;
UPDATE worker_liveness SET status = 'lost'
WHERE attempt_id = ANY(%(ids)s) AND status = 'alive';
COMMIT;                                              -- then recover(job) for each, outside this transaction

-- Heartbeat relay from E1's supervisor (0 rows updated = late heartbeat, ignored)
UPDATE worker_liveness SET last_beat_at = now()
WHERE attempt_id = %(attempt_id)s AND status = 'alive';
```

If the team ever runs two scheduler processes, the queue claim takes the same shape: `SELECT job_id FROM jobs WHERE state = 'queued' ORDER BY need_class, submitted_at, job_id FOR UPDATE SKIP LOCKED LIMIT 1`. Treat that as a viva talking point, not a build item.

### NOTIFY from triggers (D-25)

`pg_notify` called inside a transaction is delivered only if and when the transaction commits, in commit order. The payload must stay under 8000 bytes, so send keys and let readers query ([PostgreSQL NOTIFY](https://www.postgresql.org/docs/current/interactive/sql-notify.html)).

```sql
CREATE FUNCTION notify_job_state() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('job_events', json_build_object(
    'type', 'job_state', 'job_id', NEW.job_id, 'user_id', NEW.user_id,
    'state', NEW.state, 'at', clock_timestamp())::text);
  RETURN NULL;
END $$;
CREATE TRIGGER job_insert_notify AFTER INSERT ON jobs
  FOR EACH ROW EXECUTE FUNCTION notify_job_state();
CREATE TRIGGER job_state_notify AFTER UPDATE OF state ON jobs
  FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state) EXECUTE FUNCTION notify_job_state();

CREATE FUNCTION notify_capacity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN PERFORM pg_notify('capacity', ''); RETURN NULL; END $$;   -- identical payloads fold into one
CREATE TRIGGER capacity_notify AFTER UPDATE OF allocated ON resources
  FOR EACH STATEMENT EXECUTE FUNCTION notify_capacity();
```

The hub holds one dedicated connection outside the pool. It runs `LISTEN job_events` and `LISTEN capacity`, iterates `conn.notifies()` in a thread and calls `loop.call_soon_threadsafe(hub.publish, payload)`. On a `capacity` signal it re-reads the snapshot. Students receive only events whose `user_id` matches; faculty and administrators receive everything. The psycopg 3 `notifies()` generator is **unverified** and must be checked against the installed psycopg version. Keep transactions short, because listeners only see a notification after the sending transaction ends ([PostgreSQL NOTIFY](https://www.postgresql.org/docs/current/interactive/sql-notify.html)).

### The write-skew demonstration for the DBMS viva

Open two `psql` sessions against a scratch copy of the schema in which allocation inserts an `allocation_items` row after computing free capacity as a `SUM`, with no lock on `resources`. Both sessions read "1000 millicores free", both insert, both commit, and the invariant query now reports over-allocation. Rerun at SERIALIZABLE and one session fails with SQLSTATE 40001, the failure the documentation says applications "must be prepared to retry" ([PostgreSQL serialization failure handling](https://www.postgresql.org/docs/18/mvcc-serialization-failure-handling.html)). Then show the production path: lock rows, re-check, CHECK backstop. The S1 acceptance test "two concurrent requests for the last unit, exactly one succeeds", run 100 times, is the automated version.

### Scheduling stays exactly as Stage 2 specified

E2 runs the admission cycle every 0.5 s and immediately on capacity changed. FCFS admits the head only if it fits ("otherwise the head waits"). The need-based policy takes the first candidate in heap order that fits, unless the oldest job has waited 60 s, in which case only that job may be admitted. D-03 repeats admission until nothing fits. The complexities are O(log n) heap insert, O(1) map lookup and O(k log n) first-fit past k. What makes scheduling "real" here is the input: the snapshot reflects the VM's actual cores and memory (D-26), and an admitted job actually runs. Slurm computes priority as a weighted sum of age, fair-share, partition and QOS factors and backfills small jobs "in the shadow" of large ones ([Slurm priority, University of Luxembourg HPC](https://hpc-docs.uni.lu/jobs/priority)). DRF picks "the user with the lowest dominant share" ([Ghodsi et al., NSDI 2011](https://people.eecs.berkeley.edu/~alig/papers/drf.pdf)). Both are comparisons for the DSA viva, not changes to FR-01. One DSA point is worth keeping: with a single aging rate r, the key `base - r * arrival_time` is time-independent and fits a plain heap. Stage 2's 60 s guard is the simpler bounded-starvation equivalent. For the OS viva, all-or-nothing allocation breaks hold-and-wait, so the Banker's safety check reduces to "request at most available, measured against the 90% cap".

## isolate turns a C file into a Codeforces verdict

E1 owns the riskiest technical step, so it gets verified first. The isolate apt packages cover stable Debian and "the last two Ubuntu LTS releases" on amd64. The package ships `isolate-check-environment`, and the build dependencies are libcap, libseccomp and libsystemd ([ioi/isolate](https://github.com/ioi/isolate)). Whether Ubuntu 24.04 is still in that two-release window in November 2026 is **unverified**. If it is not, build from source.

### Day-2 sandbox check on the VM

```bash
stat -fc %T /sys/fs/cgroup            # expect: cgroup2fs
uname -r                              # expect >= 5.19 (memory reporting); 24.04 ships 6.8 (unverified)
sudo swapoff -a                       # and remove swap from /etc/fstab; isolate advises no swap
sudo systemctl enable --now isolate.service
sudo isolate-check-environment        # fix every warning it prints, or record why not
isolate --cg -b 0 --init              # prints the box path
# copy hello.c into <box>/box/, compile and run it as below, then read the meta file
isolate --cg -b 0 --cleanup
```

The flags below come from the current man page source on GitHub ([isolate man page source](https://github.com/ioi/isolate/blob/master/isolate.1.txt)): `-b/--box-id`, `--init`, `--run`, `--cleanup`, `--cg` (required with all three), `--cg-mem` in KB, `-t/--time` CPU seconds, `-w/--wall-time`, `-x/--extra-time`, `-p/--processes` (default 1), `-f/--fsize` in KB, `-k/--stack`, `-n/--open-files`, `-d/--dir` rules, `-i/--stdin`, `-o/--stdout` and `-M/--meta`. The packaged version may lag the master branch, so **check every flag against `man isolate` on the VM**. Also **unverified**: the stderr option (`-r/--stderr`), the environment option (`-E/--env`), the default directory rules (whether `/usr` and `/lib` are visible for gcc), the per-box UID, the box path, the privilege model of the packaged binary, and whether `--cleanup` kills processes still in a box.

### IsolateRunner algorithm (code_eval)

```text
run_code_eval(job, attempt, box B):
  isolate --cg -b B --cleanup                         # leftovers from a crash (also at server start)
  path = isolate --cg -b B --init                     # failure here = sandbox start failure (D-06)
  write prog.c into path/box/                         # expected outputs NEVER enter the box

  # Compile: generous limits, many processes (gcc spawns cc1, as, collect2, ld)
  isolate --cg -b B --run --processes=16 --time=10 --wall-time=20 \
          --cg-mem=<jobs.ram_mib * 1024> --fsize=65536 --meta=compile.meta \
          --stdout=compile.out --stderr=compile.err --env=PATH=/usr/bin:/bin \
          -- /usr/bin/gcc -std=gnu11 -O2 -pipe -o main prog.c -lm
  if compile failed (exit != 0 or status in TO, SG, RE): attempt verdict "Compilation Error",
       store first 64 KiB of compile.err, go to finish

  for case in test_cases ORDER BY case_no:
      put input as path/box/in.txt
      isolate --cg -b B --run --processes=1 \
              --time=<problems.time_limit_ms / 1000> --extra-time=0.5 \
              --wall-time=<2 * time + 1> --cg-mem=<problems.memory_limit_mib * 1024> \
              --fsize=<output cap KB> --stdin=in.txt --stdout=out.txt --meta=case.meta -- ./main
      v = classify(meta, isolate exit code, compare(expected, path/box/out.txt))
      INSERT INTO test_results (attempt_id, case_no, verdict, time_ms) ...  ON CONFLICT DO NOTHING
  finish:
      headline = "Compilation Error" | "Accepted 10/10" | "<first non-AC verdict> on test k (p/10)"
      isolate --cg -b B --cleanup
```

The job-level timer runs in the supervisor thread. Its limit is `jobs.time_limit_s` over the whole attempt (D-05). When it fires, E1 follows the Stage 2 stop order: SIGTERM to the worker child's process group, wait 2 s, SIGKILL, then `isolate --cleanup`. The job fails with `end_reason = timeout`, which is what NFR-10 counts. The confirm step is what FR-08 and FR-13 hinge on: **no process owned by the box UID remains** (`pgrep -u <box uid>` is empty) and cleanup succeeded. Only then does E1 report "stopped, confirmed" (message 9 reply) or call release (message 7). Test this on day 2 by running `while(1);`, killing the worker child with `kill -9` and checking `ps`.

### Verdict precedence

| Order | Evidence in the meta file | Per-case verdict | Notes |
|---|---|---|---|
| 1 | isolate exit code not 0 or 1, or `status:XX` | IE (attempt-level) | Internal error; the attempt ends `crash` and the job fails with headline "Internal Error" |
| 2 | `status:TO` | TLE | Record whether CPU or wall time tripped (NFR-10 asks for correct classification) |
| 3 | `cg-oom-killed` present | MLE | Check before RE: an OOM kill also arrives as a signal |
| 4 | `status:SG` with `exitsig:25` (SIGXFSZ on x86-64) | OLE | Output passed `--fsize`; `RLIMIT_FSIZE` raises SIGXFSZ ([setrlimit(2)](https://www.man7.org/linux/man-pages/man2/setrlimit.2.html)) |
| 5 | `status:SG` (any other signal) or `status:RE` | RE | Store the signal name or exit code: SIGSEGV, SIGFPE, SIGABRT, NZEC |
| 6 | Normal exit | AC or WA from the checker | CE comes only from the compile step |

The meta fields and status codes are cited from the man page; treat the precedence itself as a design choice to defend in the viva. Judge0 has no distinct MLE status (its 14 statuses run from In Queue to Exec Format Error, with six Runtime Error variants) ([Judge0 status enum](https://huggingface.co/spaces/AdityaSharmaTech/Judge0Tsting/blob/main/app/enumerations/status.rb)), which is one more reason to own the classifier. The `test_results.verdict` CHECK widens to `('AC','WA','TLE','MLE','OLE','RE')` under D-24.

### Checker and safe file handling

The default checker compares whitespace-separated tokens, which tolerates trailing spaces, CRLF and a missing final newline. Add a float checker (absolute or relative error 1e-6) only if a problem needs it. Read at most cap + 1 bytes of contestant output. Open every file inside the box with `os.open(path, os.O_RDONLY | os.O_NOFOLLOW)`, because the 2024 Judge0 escapes began with a symlink planted in the sandbox directory ([The Hacker News](https://thehackernews.com/2024/04/sandbox-escape-vulnerabilities-in.html)). Never `chown -R` box contents. Expected outputs stay outside the box, as Stage 2 requires.

### Languages and limits

C is required (Stage 2 says "a C program"). C++ is the open item from the earlier guide: add `g++ -std=gnu++17 -O2 -pipe` with the same compile limits if the team says yes. Java is out of scope for the demo. If it is ever added, it needs OpenJDK 17+ for cgroup v2 awareness, an explicit `-Xmx` below the cgroup limit, a cgroup memory limit rather than an address-space limit, and about 64 processes for JVM threads ([Red Hat Developer](https://developers.redhat.com/articles/2025/11/27/how-does-cgroups-v2-impact-java-and-nodejs-openshift-4); [Judge0 CE API docs](https://ce.judge0.com/)). Set per-case time limits at 2 to 3 times the reference solution measured on the VM, not on a laptop.

### CgroupRunner: the OS fundamentals in the team's own code

Synthetic workloads come from the team's own scripts, so they can run under a lighter runner that sets real `cpu.max` quotas for multi-core requests. The cgroup v2 interface supplies everything needed: `memory.max` invokes the OOM killer at the limit, `memory.events` counts `oom_kill`, `cgroup.kill` sends SIGKILL to the whole tree, and `cgroup.events` reports `populated 0` once it is empty ([kernel cgroup v2 documentation](https://docs.kernel.org/6.16/admin-guide/cgroup-v2.html)).

| Step | Action | OS concept |
|---|---|---|
| 1 | Once: `mkdir` a delegated parent such as `.../orch`, then write `+cpu +memory +pids` to its `cgroup.subtree_control`. The parent holds no processes ("no internal process" rule). | cgroup hierarchy, controllers |
| 2 | Per job: `mkdir job-<id>`; write `cpu.max` = `<cpu_millicores*100> 100000`, `memory.max` = ram_mib in bytes, `memory.swap.max` = 0, `memory.oom.group` = 1, `pids.max` = 64 | CFS bandwidth, memory limits, fork-bomb guard |
| 3 | `fork()`; child: `setpgid(0,0)`, write own PID to `cgroup.procs`, `setrlimit` CORE 0 and NOFILE 64, drop to an unprivileged UID, `execve` | process creation, rlimits inherited across exec |
| 4 | Parent: `wait4` with a monotonic-clock deadline; overrun means write `1` to `cgroup.kill` | signals, zombies, rusage |
| 5 | Always write `1` to `cgroup.kill`, then poll `cgroup.events` until `populated 0` | confirmed stop before release (FR-13) |
| 6 | Read `cpu.stat usage_usec`, `memory.peak`, `memory.events oom_kill`, `pids.events max`; then `rmdir` (which only succeeds when empty) | accounting |

The research notes also offer two finer variants: `clone3(CLONE_INTO_CGROUP)`, which closes the window between fork and joining the cgroup, and `pidfd_open` plus `poll`. Both are **unverified** for this guide. How the delegated parent is created is also **unverified**: `Delegate=yes` in the server's systemd unit is the expected route. If delegation fails, synthetic jobs run inside isolate boxes with CPU requests rounded up to whole pinned cores, and the report states that limitation.

### Throughput, sizing and the GPU question

On an 8 vCPU VM, the host profile gives `total = 7000` millicores after a one-core system carve-out, so 6300 are allocatable: six concurrent 1000-millicore judge jobs. If a typical submission takes 2 to 3 s (compile plus ten short cases), that is roughly 120 jobs per minute against NFR-02's 50. This is an estimate to be confirmed by measurement, since no isolate overhead benchmark was found. Size the VM at **8 vCPU, 12 GB RAM, 60 GB disk** on a workstation, or 4 vCPU and 6 to 8 GB on a laptop fallback. To keep TLE verdicts stable, pin vCPUs and never oversubscribe host cores; isolate's own reproducibility advice adds disabling swap, transparent huge pages and turbo ([isolate man page source](https://github.com/ioi/isolate/blob/master/isolate.1.txt)). If the workstation's GPUs are RTX A6000s, they cannot be partitioned. MIG is limited to A100, A30, Hopper and some Blackwell parts ([NVIDIA MIG supported GPUs](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/latest/supported-gpus.html)), and the A6000 datasheet lists only vPC, vApps and RTX vWS virtualisation ([RTX A6000 datasheet](https://www.nvidia.com/content/dam/en-zz/Solutions/products/workstations/nvidia-rtx-a6000-datasheet.pdf)). VFIO passthrough of one card to a KVM guest is possible but **unverified and fiddly**, and VirtualBox cannot do it. Under D-26, GPU stays `none` unless passthrough works by the end of Week 11. If it does, VRAM is a whole-device reservation (the earlier guide already treats VRAM as a reservation, not an enforced limit), and occupancy comes from NVML memory info. NVML's `utilization.memory` is the wrong field for occupancy because it measures the fraction of time memory was being read or written ([NVML nvmlUtilization_t](https://docs.nvidia.com/deploy/archive/R535/nvml-api/structnvmlUtilization__t.html)).

## Heartbeats at 2 s and a 1 s sweep recover a killed worker in about 8 s

Recovery follows the Stage 2 order: **stop, confirm, release, requeue**. Each step is idempotent, so a crash halfway through can be replayed at startup.

| Step | Mechanism | Budget from the kill |
|---|---|---|
| Last beat | The worker child prints `{"t": "beat"}` every 2 s; the supervisor relays it, updating `last_beat_at` | 0 to 2 s before the kill |
| Detection | 1 s sweep finds `last_beat_at` older than 6 s; alive becomes lost; running becomes failed (reason "worker lost") | 4 to 7 s |
| Stop and confirm (message 9) | Kill the worker process group, `isolate --cleanup`, confirm the box UID has no processes | under 1 s (measure) |
| Release (message 10) | E3 release transaction; failed becomes released | tens of ms (measure) |
| Requeue (message 11) | released becomes queued, `retry_count + 1`, `submitted_at` kept; after 2 retries the job ends | immediate |
| Re-admission | capacity changed (message 8) triggers a cycle | under 0.5 s |

The worst case is about 8.5 s from kill to requeued, under NFR-08's 10 s. Define "recovery" in the report as **kill to `queued`**, since the requirement says "recovery and requeue within 10 s". A late heartbeat after `lost` updates zero rows and is ignored. A late result from a fenced attempt is rejected because its attempt is closed. The database clock is used for all of this arithmetic, never a laptop clock.

**Startup reconciliation (FR-14)** runs before admission resumes. It cleans every isolate box ID. It applies the earlier guide's reconciliation table: admitted jobs go back to queued; never-started allocated jobs go to cancelled then released (D-04); running jobs without a live worker go through failed, released, then queued if `retry_count < 2`. It then runs `invariants.sql` and logs counts. Run it after `systemctl restart` during the demo.

**Health guard (FR-04)** reads `/proc/meminfo` every second. It pauses admission when MemAvailable drops below 10% of MemTotal or when the isolate probe box fails to `--init`, and resumes after 3 consecutive healthy reads. That keeps the earlier guide's hysteresis and replaces only the Docker probe. While paused, intake keeps validating, jobs stay queued (FR-03), and the monitor shows a banner with the reason.

## Bridged networking and a team-owned router remove the riskiest variable

Wireless client isolation "prevents wireless clients from communicating with one another", and NAT-mode SSIDs block Layer 2 discovery entirely ([Cisco Meraki](https://documentation.meraki.com/MR/Firewall_and_Traffic_Shaping/Wireless_Client_Isolation)). Campus guest Wi-Fi commonly turns it on. Only a bridged VM is reachable from other LAN hosts without port forwarding ([VirtualBox 7.0 manual](https://docs.oracle.com:443/en/virtualization/virtualbox/7.0/user/networkingdetails.html)).

| Step | Do this | Check |
|---|---|---|
| 1 Router | Bring a cheap travel or home router with its own SSID; turn AP or client isolation off; DHCP pool `.100` to `.199`; keep a phone hotspot as backup (check it does not isolate clients) | Two laptops can ping each other |
| 2 Workstation | Wire it to the router; enable VT-x or AMD-V in the BIOS | Hypervisor starts a 64-bit guest |
| 3 Hypervisor | Linux host: KVM with virt-manager, bridge `br0` (macvtap bridge mode cannot reach the host itself). Windows host: VirtualBox with a Bridged Adapter on the wired NIC. Avoid WSL2: it is NAT by default and needs portproxy or mirrored mode ([Microsoft Learn](https://learn.microsoft.com/en-us/windows/wsl/networking)) | VM gets an address from the team router |
| 4 Fixed IP | Reserve the VM's MAC in the router's DHCP table as `192.168.50.10` | Same IP after a reboot |
| 5 Guest OS | Ubuntu Server 24.04, 8 vCPU, 12 GB, 60 GB; swap off; isolate check (previous section) | `isolate-check-environment` clean |
| 6 Database | `apt install postgresql-16` (24.04 default version **unverified**); `listen_addresses = 'localhost'`; apply roles, schema, triggers, seed | `invariants.sql` returns 0 rows |
| 7 Firewall | `ufw default deny incoming`; `ufw allow from 192.168.50.0/24 to any port 8443 proto tcp`; same for `7070/tcp`, `5353/udp`, `7071/udp`; `22/tcp` from team laptops only | `ss -tlnp` shows 5432 on 127.0.0.1 only |
| 8 TLS (D-27) | On the VM: `mkcert -install`, then `mkcert 192.168.50.10 labjudge.local`; uvicorn uses that cert and key on 8443. Copy only `rootCA.pem` to each laptop and run `mkcert -install` there with `CAROOT` set. Never share `rootCA-key.pem` ([mkcert](https://github.com/FiloSottile/mkcert)) | `https://192.168.50.10:8443/api/health` shows no warning |
| 9 Laptops | Network profile Private; allow the agent through Windows Defender Firewall (exact `netsh` rule **unverified**); install Python and the agent | Agent discovers the server |
| 10 Services | systemd units for the server and isolate; `deploy/run.sh` prints IP, ports and discovery status | Survives `reboot` |
| 11 Snapshot | Snapshot the VM once all checks pass; export an OVA to USB | Restore tested once |

If bridging is impossible (for example, the host is on Wi-Fi only and the AP drops frames from unknown MACs), fall back to NAT with port forwarding of 8443 and 7070 and a manually typed IP. Discovery is then lost, because NAT guests do not reliably receive broadcasts ([VirtualBox 7.0 manual](https://docs.oracle.com:443/en/virtualization/virtualbox/7.0/user/networkingdetails.html)). Bridging over Wi-Fi works for IPv4 only, because most wireless adapters lack promiscuous mode and VirtualBox rewrites MAC addresses ([VirtualBox 7.0 manual](https://docs.oracle.com:443/en/virtualization/virtualbox/7.0/user/networkingdetails.html)).

## Day 1 to demo: a sequenced plan for four engine owners

Stage 2 set the order (E3 first, E2 and E1 in parallel on stubs, then the E4 API and pages). It put Sprint 2 in Weeks 10 to 12 and Sprint 3 in Weeks 13 to 15 ([Stage 2 handbook](file:///mnt/user-data/uploads/f9bb8033-Stage2_Document.docx)). The plan below keeps that order and the earlier guide's slice IDs, and puts the three environment spikes first because they decide whether any later code can run. If Gate 2 is already behind the team and the calendar is past Week 10, compress Phase 0 into two days and keep the sequence.

| Window | E1 Abhishek (OS) | E2 Sudhanv (DSA, DMS) | E3 Sagar (DBMS) | E4 Aniruddh (WT, CN) | Exit check |
|---|---|---|---|---|---|
| Phase 0, days 1 to 3 | VM up with E4; isolate installed and checked; hello.c compiled and run in box 0; meta parsed; kill -9 cleanup test | JobQueue (FIFO, heap, map) with unit tests; validation ranges; load generator skeleton against a stub | Apply the earlier guide's roles, schema, seed and invariants on PostgreSQL 16 in the VM; last-unit test x100; CHECK demo | Router and bridged VM; every laptop reaches `/api/health` on 8443; zeroconf and UDP discovery from a Windows laptop; framing echo server | All four spikes green or a fallback chosen; `docs/protocol.md`, `ports.py` and OpenAPI stubs frozen |
| Week 10 (walking skeleton) | IsolateRunner compile plus cases with AC/WA; supervisor thread, worker child, beats | FCFS AdmissionLoop on real E3 snapshot and allocate | allocate, release, audit, D-01 refusal; NOTIFY triggers (D-25) | Login, `POST /api/jobs`, job page, WSS via LISTEN | A C file submitted from a laptop browser reaches `released` with its verdict on screen |
| Week 11 | Full verdict set (TLE, MLE, OLE, RE, CE); job timer (D-05); stop and confirm; R1 reference set | Need policy, 60 s guard, D-03; admin policy switch | Idempotent release; invariants in CI; write-skew script; capacity host profile (D-26) | Role matrix; liveness sweep with SKIP LOCKED; `recover()`; agent HELLO and HB; presence tiles | `kill -9` on a worker requeues within 10 s, by hand |
| Week 12 (Sprint 2 exit) | CgroupRunner for synthetic jobs; cancel stops (FR-10) | Synthetic workload scripts; load generator posting from laptops; V1 run | `kpi.sql`; systemd units; `run.sh`; snapshot procedure | Agent SUBMIT, CANCEL, EVENT; `monitor.html`; health guard (FR-04) | Each engine passes its own tests; LAN rehearsal 1 with four laptops |
| Week 13 | Box hygiene at startup; GPU decision final | FCFS against need comparison runs | Reconciliation queries and checks | Startup reconciliation; TLS on 7070; mkcert on all laptops | F1 and F2 pass once end to end |
| Week 14 | R1 and T1 runs | B1 and B2 under both policies | `report.py` writes `docs/kpi_report.md` | V1, F1 and F2 runs; NFR-09 measurement | Every NFR has a measured value |
| Week 15 | Freeze | Freeze | Freeze | Freeze | Three timed rehearsals on the demo router; snapshot and OVA; backup video recorded |

Integration goes through the frozen ports and contracts, not through reading each other's code. Each engine owner tests against fakes of the other ports (for example, E2 against an in-memory E3), and the walking skeleton in Week 10 proves the real wiring early. Use the earlier guide's definitions for the measured runs; the purpose of each is summarised in the NFR table further down.

## A twelve-minute demo that kills a worker on purpose

| Time | Who | Action | What the room sees | Proves |
|---|---|---|---|---|
| 0:00 | E4 | Topology slide: router, VM IP, ports | Clear picture of two programs talking over the LAN | Architecture |
| 0:45 | All | Start the agent on four laptops | Agents discover the server by mDNS; faculty monitor tiles turn online | D-20, D-21, CN |
| 1:45 | E1 | Faculty opens exam session 7; students submit an AC solution in the browser and a WA one from the agent | Live state changes; "Wrong Answer on test 4 (7/10)" printed in the agent console | FR-09, FR-11, NFR-09 |
| 3:00 | E1 | Submit an infinite loop, a 1 GB malloc, a null dereference, a syntax error, a fork bomb and a printing loop | TLE, MLE, RE (SIGSEGV), CE, contained fork bomb, OLE | NFR-03, NFR-10, sandbox |
| 4:30 | E2 | Load generator bursts 60 submissions; switch policy FCFS to need while exam and practice jobs queue | Queue drains; exam jobs overtake practice; nothing waits past 60 s | FR-01, NFR-01, NFR-02 |
| 6:00 | E3 | psql: two sessions race for the last unit; then attempt a direct over-allocation; run `invariants.sql` | One commits and one is refused (audit row); ERROR 23514; 0 rows | FR-05, FR-06, NFR-05, NFR-06 |
| 7:30 | E4 | Start a 30 s cpu_bound job; `kill -9` its worker child | Heartbeat stops; lost at about 6 s; released; requeued (retry 1); runs again; history timeline with timestamps | FR-12, FR-14, NFR-08 |
| 9:30 | E4 | Close one laptop's agent | Tile turns offline after 6 s | Presence heartbeat |
| 10:00 | E3 | `systemctl restart` the server mid-queue | Reconciliation log, then admission resumes and jobs finish | FR-14 at platform start |
| 11:00 | All | KPI table from `docs/kpi_report.md` | Measured against target for every NFR | Evaluation |

Keep a recorded video of the 7:30 segment and the OVA on a USB stick. If the router misbehaves, run the whole demo from one laptop's browser against the VM over a direct cable.

## Every NFR measured from the database's own timestamps

`job_state_history` already records `from_state`, `to_state` and `at` for every transition, so the latency NFRs need no extra instrumentation.

```sql
-- NFR-01: scheduling wait (queued -> admitted) and start latency (queued -> running), run B1 (not saturated)
WITH t AS (
  SELECT job_id,
         MIN(at) FILTER (WHERE to_state = 'queued')   AS q,
         MIN(at) FILTER (WHERE to_state = 'admitted') AS a,
         MIN(at) FILTER (WHERE to_state = 'running')  AS r
  FROM job_state_history GROUP BY job_id)
SELECT percentile_cont(ARRAY[0.5, 0.95, 0.99]) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM a - q)) AS wait_s,
       percentile_cont(ARRAY[0.5, 0.95, 0.99]) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM r - q)) AS start_s
FROM t WHERE a IS NOT NULL AND r IS NOT NULL;

-- NFR-02: completed jobs per minute
SELECT date_trunc('minute', at) AS minute, count(*) FROM job_state_history
WHERE to_state = 'completed' GROUP BY 1 ORDER BY 1;

-- NFR-07: terminal state -> released, per cycle
SELECT percentile_cont(ARRAY[0.5, 0.95]) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM rel.at - t.at)) AS release_s,
       max(EXTRACT(EPOCH FROM rel.at - t.at)) AS worst_s
FROM job_state_history t
CROSS JOIN LATERAL (
  SELECT at FROM job_state_history r
  WHERE r.job_id = t.job_id AND r.event_id > t.event_id AND r.to_state = 'released'
  ORDER BY r.event_id LIMIT 1) rel
WHERE t.to_state IN ('completed', 'failed', 'cancelled');
```

| NFR | Target | Run | Method | Owner |
|---|---|---|---|---|
| NFR-01 | wait at most 2 s; start within 5 s | B1 | Query above. Under saturation, measure from capacity changed to admitted, since queue wait is unbounded by definition | E2 |
| NFR-02 | at least 50 jobs/min | B1 (60/min mixed, 10 min, from laptops) | Per-minute counts; report the minimum minute | E1 |
| NFR-03 | at least 99% validation and verdict accuracy | V1 (1,000 requests), R1 (20+ programs covering all verdicts) | Expected against actual table | E2, E1 |
| NFR-04 | CPU 70%, RAM 60%, GPU/VRAM 60% (when GPU active) | B2 under both policies | Sample `/proc/stat` and `/proc/meminfo` every second to CSV during the window | E2 |
| NFR-05 | zero inconsistent records; states at least 99% accurate | After every run | `invariants.sql` returns 0 rows; leaks query 0 | E3 |
| NFR-06 | zero over-allocation; at most 90% | B2 | CHECK makes it structural; sample `allocated / (total - reserved)` per row each second, max at most 1.0 | E3 |
| NFR-07 | release at most 2 s, zero leaks | All runs | Query above; `SELECT count(*) FROM allocations WHERE status = 'open'` = 0 after drain | E3 |
| NFR-08 | detect after 6 s; recover within 10 s | F1 (kill x10) | Chaos script records `SELECT clock_timestamp()` just before `kill -9`; detection = `failed` event at; recovery = next `queued` at; report max | E4 |
| NFR-09 | dashboard within 2 s | Any run | Page records receive time minus payload `at`, corrected by an offset estimated on connect (NTP-style ping over WSS); report p95 | E4 |
| NFR-10 | more than 99% of overruns stopped and classified | T1 | Count `end_reason = 'timeout'` over jobs designed to overrun; also kill latency past the limit | E1 |

Present one table (NFR, target, p50, p95, max, pass or fail, run ID), one latency CDF, one time series of allocated against utilised against the 90% line, and one timeline of a fault run. Keep the CSVs and SQL in `bench/` so an evaluator can rerun them. Do not reuse the "S1 dry run" utilisation figures from Stage 2 as evidence; they predate any code.

## Risks with fallbacks already wired in

| Risk | Early signal | Fallback |
|---|---|---|
| Campus Wi-Fi isolates clients | Laptops cannot ping each other | Team router (default plan); phone hotspot backup |
| VM bridging fails | VM gets no address on the router subnet | Wired NIC; else NAT with 8443 and 7070 forwarded and manual IP |
| isolate fails in the VM (delegation, kernel, packaging) | Day-2 check fails | Build from source; else DockerRunner (earlier guide design); nsjail as a third option |
| Kernel too old for memory reporting | `uname -r` below 5.19 | Ubuntu 24.04 or the 22.04 HWE kernel |
| No workstation provided | Mentor cannot confirm by Week 10 | Team laptop with a 4 vCPU, 6 to 8 GB VM and 2 or 3 boxes |
| GPU passthrough impossible | No VFIO-capable IOMMU group or no KVM | `GPU_MODE=none` or simulated; state it plainly in the report |
| TLE flapping in the VM | R1 times vary by over 20% | Pin vCPUs; do not oversubscribe; limits at 2 to 3 times the reference |
| Recovery over 10 s | F1 maximum above 9 s | Pre-initialise boxes; sweep at 0.5 s (detection is still "after 6 s") |
| Windows blocks discovery | No mDNS answer on laptops | UDP broadcast; manual IP printed on the slide |
| Agent `.exe` flagged or broken | PyInstaller build fails a test | Run from source with Python installed |
| Integration slips | No walking skeleton by end of Week 10 | Cut CgroupRunner and agent TLS; never cut the recovery path |
| Claude usage limits mid-sprint | `/usage` bars near full | Sonnet default, staggered Opus sessions (next section) |
| Evaluator asks "is running student code safe?" | Viva question | Layered answer: VM boundary, isolate box, no network, unprivileged UID, ufw, no Postgres on the LAN; Judge0's escapes show why privileged containers were avoided |

## Claude Code: Sonnet by default, specs first, one worktree per owner

The official guidance calls the context window "the most important resource to manage". Every request re-sends the whole conversation, so a one-line question in an all-day session "still draws usage for the whole conversation" ([best practices](https://code.claude.com/docs/en/best-practices.md); [manage costs](https://code.claude.com/docs/en/costs.md)). The levers below add to the earlier guide's section 0 (slice loop, CLAUDE.md); they do not replace it.

### Model and effort

Opus 5.5 is the default model on Pro, Max, Team and Enterprise ([model configuration](https://code.claude.com/docs/en/model-config.md)). The cost guidance says Sonnet "handles most coding tasks well and costs less than Opus" and that high spend "usually traces back to long sessions that were never cleared or to Opus left as the default model" ([manage costs](https://code.claude.com/docs/en/costs.md)). Set **`"model": "sonnet"`** in the shared settings. Switch to `/model opus` only for the few design sessions: freezing contracts, the sandbox security review, and the recovery race analysis. `opusplan` (Opus in plan mode, Sonnet for execution) suits "plan once, execute long", but each toggle switches model and "starts a fresh cache" ([prompt caching](https://code.claude.com/docs/en/prompt-caching.md)). Pick the model and effort at the start of a session. The 5.5 models default to `medium` effort, and changing effort on them keeps the cache ([model configuration](https://code.claude.com/docs/en/model-config.md); [prompt caching](https://code.claude.com/docs/en/prompt-caching.md)).

```json
{
  "model": "sonnet",
  "effortLevel": "medium",
  "permissions": {
    "allow": [
      "Bash(./scripts/t:*)",
      "Bash(git status)",
      "Bash(git diff:*)",
      "Bash(psql -d orch_dev:*)"
    ],
    "deny": [
      "Bash(sudo:*)",
      "Read(./.env)"
    ]
  }
}
```

`model` and `effortLevel` are documented setting names. The permission rule syntax is **unverified**; confirm it with `/permissions`, and let `/plugin install --scope project` write plugin entries rather than hand-editing them.

### CLAUDE.md layout

Keep each CLAUDE.md under 200 lines. Nested CLAUDE.md files in subdirectories "load on demand" when Claude touches files there. `.claude/rules/*.md` with `paths:` frontmatter loads only for matching files. Block-level HTML comments are stripped before injection. The root CLAUDE.md is re-read after compaction ([memory](https://code.claude.com/docs/en/memory.md)). The skeleton:

```markdown
# Lab resource allocation and safe monitoring system
One FastAPI process (uvicorn --workers 1) + PostgreSQL 16 in an Ubuntu 24.04 VM.
Engines are modules: server/app/e1_exec, e2_sched, e3_alloc, e4_monitor. Agent: agent/.
Contracts win over code: docs/specs/, docs/protocol.md, db/schema.sql, server/app/common/ports.py.

## Commands
- Tests: ./scripts/t <path or -m marker>   (never bare pytest; output is filtered)
- DB reset: ./scripts/db_reset.sh   Server: ./scripts/run_dev.sh   Sandbox tests (VM only): ./scripts/t -m sandbox

## Hard invariants (tests enforce these)
1. Over-allocation impossible: lock resources ORDER BY resource_id FOR UPDATE, re-check, CHECK backstop.
2. Allocate and release are single transactions; nothing leaves the process until after_commit.
3. Release only after E1 confirms the stop; order: stop, confirm, release, requeue. Release is idempotent.
4. Job state changes only through transition(); the trigger checks the 13 legal pairs.
5. Each engine writes only its own tables (SET LOCAL ROLE eN_role).
6. Integer units; reserved = (total + 9) / 10. At most 2 retries; requeue keeps submitted_at.
7. Untrusted code runs only through IsolateRunner. Read box files with O_NOFOLLOW.
## Names
user_id (never usn), cpu_millicores, ram_mib, vram_mib, need_class.
States: queued admitted allocated running completed failed cancelled released.
## Rules of work
- Stay in your engine folder. Contract changes: separate PR touching only docs/, db/, protocol/.
- One slice per session. Before /clear, append a 5-line handoff to docs/NEXT.md.
- Paste test output as evidence before saying a slice is done.
## Compact instructions
Keep: slice ID, files changed, failing tests, open questions. Drop exploration output.
```

Each engine folder gets 6 to 10 lines of its own: owner, test command, owned tables, and the two or three rules a newcomer would break. For E1, for example: "verdict order IE, TLE, MLE, OLE, RE, checker"; "stop order: kill group, cleanup, confirm box UID empty, then report"; "expected outputs never enter the box".

### Session hygiene

| Situation | Command | Why |
|---|---|---|
| Switching to an unrelated slice | `/clear` (after `/rename` if you may come back) | Free; compaction is not |
| Corrected Claude twice on the same issue | `/clear` and a sharper prompt | Official advice ([best practices](https://code.claude.com/docs/en/best-practices.md)) |
| Natural break inside one long slice | `/compact Keep slice ID, failing tests` | Better than auto-compaction mid-task ([prompt caching](https://code.claude.com/docs/en/prompt-caching.md)) |
| Abandoning a wrong path | `/rewind` or Esc twice | Returns to an already-cached prefix |
| Quick side question | `/btw` | Answer never enters history |
| Watching cost | `/usage`, `/context`, a status line | Attribution by skill, subagent, plugin and MCP server ([manage costs](https://code.claude.com/docs/en/costs.md)) |

On a subscription the main conversation gets a one-hour cache and subagents five minutes ([prompt caching](https://code.claude.com/docs/en/prompt-caching.md)). A coffee break is cheap; resuming a bloated session the next morning is not. Prefer `/clear` plus `docs/NEXT.md`.

### Subagents, hooks and output filtering

The built-in Explore subagent runs on Opus under a subscription. Defining a project subagent named `Explore` with `model: haiku` makes exploration cheap ([subagents](https://code.claude.com/docs/en/sub-agents.md)).

```markdown
---
name: Explore
description: Read-only codebase search. Use to find where something is defined or used.
model: haiku
tools: Read, Grep, Glob
---
Search the repository and return file paths with one-line summaries. Never edit files.
```

Route verbose work (test runs, `dmesg`, cgroup dumps, isolate meta files) through a subagent or a filter, so that "verbose output stays in the subagent's context while only a summary returns" ([manage costs](https://code.claude.com/docs/en/costs.md)). The documented hook example rewrites test commands through `grep` and `head`, cutting context "from tens of thousands of tokens to hundreds". A wrapper script plus the CLAUDE.md rule "never bare pytest" gets the same effect without depending on the hook schema, which is **unverified** here:

```bash
#!/usr/bin/env bash
# scripts/t : run pytest quietly, show only failures
pytest -q --no-header -rfE "$@" 2>&1 | grep -E -A 8 '(FAILED|ERROR|Error|assert)' | head -120
echo "exit=${PIPESTATUS[0]}"
```

Use the bundled `/code-review` (a fresh subagent reviewing the current diff) before each PR. **Avoid agent teams**: they use "approximately 7x more tokens" ([manage costs](https://code.claude.com/docs/en/costs.md)).

### Plugins and MCP servers

| Install at project scope | Why |
|---|---|
| `pyright-lsp` (pyright binary installed separately) | "A single 'go to definition' call replaces ... a grep followed by reading multiple candidate files" ([manage costs](https://code.claude.com/docs/en/costs.md)) |
| `commit-commands` | Consistent commits across four people |
| `security-guidance` | Reviews each change for common vulnerabilities; worth its cost around the sandbox and auth ([discover plugins](https://code.claude.com/docs/en/discover-plugins.md)) |
| `clangd-lsp`, only if E1 writes a C helper | Same benefit for C |

| Avoid | Instead |
|---|---|
| `github` plugin or MCP | The `gh` CLI; the docs say CLI tools "are still more context-efficient than MCP servers" ([manage costs](https://code.claude.com/docs/en/costs.md)) |
| Slack, Notion, Linear and other integration plugins; output-style plugins | Nothing; they add no value to this build |
| A Postgres MCP by default | `psql` through Bash. If schema introspection is wanted, DBHub with a read-only role is the documented example ([MCP](https://code.claude.com/docs/en/mcp.md)) |
| Context7 as an always-on MCP | Its CLI skill mode (`npx ctx7 setup --claude`) when FastAPI or psycopg docs are needed ([upstash/context7](https://github.com/upstash/context7)) |

MCP tools are now deferred by default ("only tool names and server instructions load at session start"), so an idle server costs less than it once did ([MCP](https://code.claude.com/docs/en/mcp.md)). Still check each plugin's "Context cost" before installing and prune anything flagged "Not used recently" ([discover plugins](https://code.claude.com/docs/en/discover-plugins.md)). The `feature-dev` plugin did not appear on the current official list; its existence is **unverified**.

### Spec-first slices in per-owner worktrees

The documented spec-first flow has Claude interview you and "write a complete spec to SPEC.md", then "start a fresh session to execute it". Good specs "name the files and interfaces involved, state what is out of scope, and end with an end-to-end verification step" ([best practices](https://code.claude.com/docs/en/best-practices.md)). Here that means one Opus session per contract in Phase 0 (`docs/protocol.md`, `ports.py`, the D-24 and D-25 schema deltas), then Sonnet sessions per slice. A typical prompt: "Implement `release()` in @server/app/e3_alloc/release.py per @docs/specs/E3-2.md; run `./scripts/t tests/e3`." Write the failing test first for every invariant. Each owner works in their own worktree: `claude -w e3-alloc` creates `.claude/worktrees/e3-alloc/` on branch `worktree-e3-alloc`. Add `.claude/worktrees/` to `.gitignore` ([worktrees](https://code.claude.com/docs/en/worktrees.md)). Worktrees isolate edits but do not share prompt cache, because different directories build different prefixes ([prompt caching](https://code.claude.com/docs/en/prompt-caching.md)).

### Budget across four people

Pro usage resets on a five-hour session window and is shared between claude.ai chat and Claude Code ([Pro plan usage](https://support.claude.com/en/articles/8324991-about-claude-s-pro-plan-usage)). Long brainstorming in the chat app therefore eats coding budget. Four individual plans have four independent windows, so stagger the Opus sessions across owners. Claude Code weekly limits have been 25% above pre-promotion levels since September 14, 2026 ([support article](https://support.claude.com/en/articles/15910845-claude-code-may-july-2026-weekly-limits-promotion)). No individual student discount is verified. Claude for Education is sold to institutions, so ask the university ([Anthropic](https://www.anthropic.com/news/introducing-claude-for-education?)). GitHub's free Copilot Student plan offers models "through auto model selection only", so it can cover inline boilerplate but cannot substitute for Claude Code ([GitHub Copilot plans](https://docs.github.com/en/copilot/get-started/plans)). Several commands above are version-gated (`/usage` attribution and cache lines, `/autocompact`), so run `claude --version` and update first.

## Verify these on the real VM in week one

| Item | Check | If it fails |
|---|---|---|
| isolate flags, stderr and env options, default dir rules, box UID, `--cleanup` kill behaviour | `man isolate`; compile hello.c in a box; kill -9 test | Adjust IsolateRunner; else DockerRunner |
| isolate apt package for Ubuntu 24.04 | `apt policy isolate` after adding the repository | Build from source |
| Kernel at least 5.19 and cgroup2fs | `uname -r`; `stat -fc %T /sys/fs/cgroup` | HWE kernel or 24.04 |
| PostgreSQL 16 from 24.04 apt; READ COMMITTED re-check semantics | `psql --version`; read the "Explicit Locking" chapter | PGDG repository |
| systemd `Delegate=yes` for CgroupRunner | Create `job-test` under the delegated path as the service user | Whole-core rounding inside isolate |
| psycopg 3 `notifies()` | 10-line LISTEN script | Async connection variant |
| python-zeroconf and UDP on Windows laptops | Discover from each laptop | Broadcast or manual IP |
| PyInstaller agent build | Run the `.exe` on a clean laptop | Run from source |
| `netsh` firewall rule syntax | Apply on one laptop | Allow interactively on first run |
| KVM bridge or macvtap; VFIO passthrough | `ip a` in the guest; `nvidia-smi` in the guest | NAT and manual IP; `GPU_MODE=none` |
| Claude Code permission syntax, hook schema, `feature-dev` | `/permissions`, `/hooks`, `/plugin` | Wrapper scripts; skip the plugin |
| Earlier guide claim that the schema ran on PostgreSQL 16.15 with 25 checks | Run it on the VM | Fix and record |

## Conclusion

The research changes less of the design than the brief expected, and that is the useful finding. The Stage 2 structure (one process, a database-enforced state machine, ordered row locks, stop before release) is exactly how production judges such as CMS stay consistent. The real gaps were at the edges: a sandbox that reports verdict evidence rather than just exit codes, a client program with a genuine network job, and a network that does not quietly block both. Putting isolate, the agent and the router under the existing interfaces lets every course show its fundamentals in the team's own code. OS shows in CgroupRunner and the stop-confirm path, DBMS in the allocation transaction and the CHECK, DSA in the queue, and CN and WT in the framed protocol and the WSS hub. The safety-critical mechanics are delegated to the kernel and PostgreSQL, which the viva can defend as a deliberate choice.

The schedule risk now lies in environment facts the team does not yet control: whether isolate's cgroup delegation works in the chosen hypervisor, whether Windows laptops answer mDNS, and whether a workstation, and which GPU, actually arrives. That is why the plan spends its first three days on checks rather than code, and why every one of those checks has a named fallback. A team that finishes Phase 0 with all checks green can treat the rest as ordinary engineering. A team that skips it will discover the same problems in Week 14, when there is no time left to change direction.
