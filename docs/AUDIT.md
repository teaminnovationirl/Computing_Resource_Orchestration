# Audits

The audit slices of D-130 add their sections here: Q-1 after E2-5, Q-2 after C-3, Q-3 before the demo. Each
section lists the scope, the tools, one triage row per finding and what each verdict led to: the test that
proves a fix, the reason for a reject, or the slice a deferral went to.

## Q-1: checkpoint audit (2026-11-23)

Scope: the code built before the new reviewers existed. That is app/common/db.py and logging.py,
app/config.py, app/e4_monitoring/state.py, app/e3_allocation/, app/e2_scheduling/validation.py and queue.py
(E2-1 and E2-2), scripts/capacity.py and both reset scripts, db/schema.sql and db/roles.sql, the tests of
all these, and pyright on the whole repository.

Tools, in the prompt's order:

- backend-design:audit on db.py, state.py, E3 and E2.
- pr-review-toolkit:silent-failure-hunter, in three runs: steps 1 and 2, steps 3 and 4, steps 5 and 6.
- backend-design:schema-reviewer.
- backend-design:security-reviewer, in one run covering validation.py, capacity.py and the roles and grants.
- pr-review-toolkit:pr-test-analyzer. Only gaps it rated 8 to 10 count.
- pyright 1.1.414.

Every finding was checked against the code before triage. The zip64, directory-flood and DATABASE_URL
findings were reproduced; the flood was also measured.

No finding in: E3 (placement, queries, service), queue.py, both reset scripts, and the roles and grants
under the security review.

Lines are as audited, before the fixes. Severity: must is a wrong result, a lost or silent failure, or an
invariant or security break; should is a real gap with a cheap fix.

| id | file:line | tool | severity | problem | verdict | outcome |
|---|---|---|---|---|---|---|
| Q1-01 | app/config.py:69 | silent-failure-hunter | must | MAX_RETRIES=3 passed startup, so every third requeue failed on the retry_count CHECK and the job stayed released | fix (D-131) | tests/test_config.py, test_bad_value_is_rejected_naming_the_variable[MAX_RETRIES-3] |
| Q1-02 | app/common/db.py:172 | silent-failure-hunter | should | after a COMMIT or ROLLBACK sent by hand, any statement began a new transaction as orch_app; tx() committed it and ran the hooks of rolled-back work | fix (D-134, amends D-48) | tests/common/test_tx.py, test_an_end_sent_by_hand_then_a_read_still_skips_the_hooks (3 cases) |
| Q1-03 | app/common/logging.py:73 | silent-failure-hunter | should | psycopg and pool warnings (refused or broken connections) never reached server.log | fix (D-136) | tests/common/test_logging.py, test_psycopg_warnings_reach_the_log_and_its_info_lines_do_not |
| Q1-04 | app/common/logging.py:68 | silent-failure-hunter | should | a wrong DATA_DIR passes startup, since configure() creates a fresh folder tree | defer to C-1 | SUMMARY gotcha: main.py refuses to start unless DATA_DIR and its five folders exist |
| Q1-05 | app/common/logging.py:73 | silent-failure-hunter | skip | lines keep going to a deleted or renamed server.log | reject | X-5 plans the rotation and must rotate through logging's own rotating handler, which reopens the file itself; Windows cannot delete an open file |
| Q1-06 | app/e4_monitoring/state.py:63 | silent-failure-hunter | should | a finished Tx whose connection served another thread's tx() passed the guard, and the move ran inside that transaction | fix (D-135) | tests/e4/test_transition.py, test_transition_refuses_a_finished_tx_whose_connection_serves_another |
| Q1-07 | app/e2_scheduling/validation.py:226 | silent-failure-hunter | must | on CPython 3.12.12+ and 3.13.9+ every zip64 archive over 128 KiB got 400, because the end copy lost the file's offsets | fix (D-132, amends D-128) | tests/e2/test_validation.py, test_a_zip64_archive_larger_than_the_end_copy_is_accepted, red and green on 3.12.15 |
| Q1-08 | scripts/capacity.py:199 | silent-failure-hunter | should | a malformed DATABASE_URL gave a traceback (with the password for some inputs) and exit 1, not 2 | fix (D-137) | tests/schema/test_capacity.py, test_a_malformed_database_url_is_a_bad_setting_and_its_value_is_not_printed (2 cases) |
| Q1-09 | scripts/capacity.py:199 | silent-failure-hunter | should | --check-database compares the database name but not the host and port | defer to E1-3 | SUMMARY gotcha: a lab PC can run a native PostgreSQL and Docker's on different ports |
| Q1-10 | db/schema.sql:305 | schema-reviewer | should | e1_role's DELETE on attempts cascaded into E4's worker_liveness | fix (D-138) | tests/schema/test_grants.py: the grant matrix, and an E1 DELETE refused with 42501 |
| Q1-11 | db/schema.sql:208 | schema-reviewer | should | require_faculty reads users without a lock, so demoting a faculty member while that member creates a problem can leave a student owner | reject | needs both acts within the same milliseconds; the fix would be a SECURITY DEFINER trigger |
| Q1-12 | db/schema.sql:307 | schema-reviewer | should | e3_role has UPDATE and DELETE on allocation_items, which no code uses | reject | E3's own table, so invariant 6 holds, and no code path uses them; the matching REVOKE DELETE on resources would break capacity.py |
| Q1-13 | db/schema.sql:129 | schema-reviewer | should | nothing in the schema stops worker_liveness going from lost back to alive | already planned in E4-5 | plan 10.5 and test_liveness: a late beat after lost returns False and changes nothing |
| Q1-14 | app/e2_scheduling/validation.py:231 | security-reviewer | should | ZipFile() parsed every record inside the declared directory size: a 10 MB upload declaring 1 entry cost 3.4 s and 83 MiB before its 400 | fix (D-133) | tests/e2/test_validation.py, test_an_end_record_that_understates_its_entries_is_refused_before_the_directory_is_parsed; the flood now costs 0.02 s and 10 MiB |
| Q1-15 | app/e3_allocation/queries.py:87 | pr-test-analyzer (8) | should | a planted SKIP LOCKED or FOR SHARE passed the two-release race | fix (tests) | tests/helpers.py race() fails a racer that ends before the gate opens; tests/e3/test_release.py asserts no tx_retry line; both plants were caught, then removed |
| Q1-16 | app/e2_scheduling/validation.py:240 | pr-test-analyzer (8) | should | deleting the entry check that runs after parsing passed, because every test zip had an honest end record | fix (tests) | the Q1-14 test: an understated end record is now refused by the count, and the check after parsing stays as a backstop |
| Q1-17 | db/schema.sql:84 | pr-test-analyzer (8) | should | loosening the retry_count CHECK passed, because no test wrote 3 | fix (tests) | tests/schema/test_constraints.py, test_a_job_holds_at_most_two_retries; a planted retry_count >= 0 was caught, then removed |
| Q1-18 | tests/schema/test_constraints.py:154 | pyright | should | fetchone()[0] on an Optional row, the only error in the repository | fix (D-139) | plain pyright: 0 errors on 53 files; pyright -p .claude/hooks: 0 errors on 2 files |

Found while fixing:

- pyright's command line never analyzed .claude/hooks, even when that folder was named on it (D-139).
- The D-128 tail copy had been measured on Python 3.13.5 only. The laptop's 3.13.5 predates the zipfile
  fix, so the laptop and the lab PC must run the same Python patch line before C-4 (D-36). This is a
  SUMMARY gotcha.

The code review of the fixes (one reviewer, verdict "with fixes") led to these changes:

- D-138 broke the E1 case of tests/common/test_tx.py, whose own write was a DELETE on attempts. That
  write is now an UPDATE.
- The first record count read 76 bytes past the directory. It refused an archive whose last entry is a
  stored zip (an .npz or .pt, which ends with records of its own) at exactly the cap. The count now reads
  exactly what ZipFile() reads (D-133). Test: test_a_zip_stored_as_the_last_entry_adds_no_count, classic
  and zip64; it was red on 3.13.5 and 3.12.15 before the change.
- A spy test now makes the D-132 fix fail on the laptop's 3.13.5 too:
  test_the_end_record_reader_finds_the_archive_at_its_file_offsets, red on a planted plain copy.

Open proposals from the review, not applied:

- Tx.savepoint() on a finished Tx still opens a savepoint on the pooled connection, inside whatever
  transaction holds it now. No caller does this. A `finished` guard there would close the path at its
  root; it changes app/common, so it needs the owner's OK.
- init_pool() raises psycopg's ProgrammingError for a DATABASE_URL that does not parse, and the message
  can quote the password. This is a gotcha for C-1, whose main.py calls it.
