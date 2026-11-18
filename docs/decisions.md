# Decisions

D-01 to D-17 are in docs/IMPLEMENTATION_PLAN.md section 17. D-18 to D-30 are proposals in docs/LAN_BUILD_GUIDE.md and apply only if accepted below. Format: decision, reason.

- D-31 (accepted): Package names follow plan section 3.4 (app/e1_execution and so on). Reason: the plan, schema and slice prompts all use these names.
- D-32 (accepted): transition() publishes live updates after commit, and the pg_notify triggers of guide D-25 are not built. Reason: using both would send every event twice.
- D-33 (accepted): Liveness and heartbeat SQL uses clock_timestamp(), never now(). Reason: project rule, and now() stays frozen inside a transaction.
- D-34 (PENDING, decide before slice A-2): add MLE and OLE verdicts and problems.memory_limit_mib (guide D-24). Reason: it changes the schema, so the whole team must agree.
- D-35 (PENDING, decide after the day 2 isolate check, before slice E1-3): sandbox is Docker (plan D-09) or isolate (guide D-19).
- D-01 confirmation (PENDING, team): a refused allocate writes the refused audit row and the admitted to queued transition, then commits.
- D-36 (accepted): The == pins in requirements.txt and requirements-dev.txt were generated with pip freeze on Python 3.13.5 (Windows) and must be re-checked on the VM's Python. Reason: the build targets Linux or WSL2, where the Python version and the available wheels can differ.