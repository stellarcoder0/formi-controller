# Load test — public_03_load (one simulated day at full scale)

**Why one day, not five:** this laptop had 1.9 GB of free disk. A full public_03 run (7 history days + 5
days, ~850k events/day at ~400 bytes each) writes about 4 GB of event files before a baseline. So the test
uses `scripts/load/public_03_load_1day.yaml`: the same 12 workflows, volumes, policies and truth, with
`history_days: 1, days: 1`. That is exactly the SRE ask ("one simulated day of public_03_load in under 10
wall-clock minutes"). Not measured: multi-day behaviour at load (state growth over 5 days, a full
experiment cycle at load scale).

Command: `scripts/load_test.sh` (simulator + `./run.sh` + `python -m controller.audit health`).
Machine: Apple M-series laptop, 8 cores, 8 GB RAM, Python 3.10, simulator running on the same machine.

| Measure | Result |
|---|---|
| Events ingested (1 history day + D0) | 1,337,409 in 17 chunks |
| Dials on D0 (12 workflows) | 604,516 |
| Controller wall-clock for the whole run, including the simulator simulating it | **33 s** |
| Controller's own time (ingest + decide), summed over all steps | 18.8 s |
| Ingest throughput | ~80,000 events/s (single process, json.loads + SQLite upsert of hourly counters) |
| Decision lag per hourly step (ingest that hour's events + decide for 12 workflows) | median 0.77 s, worst 1.4 s; first step 6.7 s (history backlog) |
| Decide time per step, all 12 workflows | ≤ 0.09 s |
| Peak RSS (controller) | 816 MB (`/usr/bin/time -l`) |
| SQLite file after the day | 26 MB |
| Actions taken | 35 (auto-applies and approval requests) |

Against the 10-minute budget: one simulated day takes ~20 s of controller time, ~30× headroom.

## Where it breaks at 10× (~6.5 M dials/day, ~120 workflows)

1. **Memory, first.** Peak RSS is dominated by holding a whole 250k-event chunk as text plus parsed
   objects, and the lead → features map (leads of the last 5 days). At 10× the lead map alone is roughly
   2–3 GB. Fix: stream chunks line by line instead of `.text.splitlines()`, and keep lead features in
   SQLite (indexed) with a small LRU in front.
2. **Single-process ingest.** ~80k events/s is ~110 s per day at 10×: still inside the budget, but one
   core. The peak hour (500k dials) would take ~6 s per hourly step.
3. **Per-workflow HTTP calls.** Each step makes ~3 GETs per workflow; 1,200 workflows means 3,600 calls per
   step against one API.
4. **One SQLite writer.** Fine for one process. For more, partition.

**Partitioning plan:** shard by `workflow_id` (hash → N workers). Each worker owns its workflows' counters,
state and proposals, so no cross-worker locking is needed; nothing in the loop needs another workflow's
data. Tenant-level fairness (credits, approver attention) becomes a small shared budget table. In
production the store would be the existing Postgres (same tables), not a new piece of infra.
