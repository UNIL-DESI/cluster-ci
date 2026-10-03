# Concurrency Management & Signal Propagation

This document describes how Cluster-CI handles GitHub Actions job concurrency, enforces branch exclusivity, and ensures compute resources across single or multiple workers are correctly freed.

---

## 1. Concurrency Model

Cluster-CI uses a **dual-mode concurrency strategy** based on branch type:

### Draft Branches (`cluster-draft/*`) — Aggressive Cancel

Used by `cluster-run` for fast iteration. A new submission **immediately cancels** any active job (pending, assigned, or running) for the same user/branch.

- GHA: `cancel-in-progress: true` → kills the old workflow run instantly.
- `submit_job.py`: signal handler propagates full cancellation to all active workers.
- Headnode: auto-cancels all active jobs matching the same user or draft branch.
- Workers: each active worker receives `/cancel/<job_id>` → eradicates process tree, purges VRAM, frees RAM.

### Non-Draft Branches (`main`, `feature/*`, etc.) — Detach & Queue

Used for production pipelines. A new submission **does not cancel** the running job.

- GHA: `cancel-in-progress: true` → the old **monitoring workflow** is replaced, but...
- `submit_job.py`: signal handler sends `detach_gha=True` to headnode instead of cancelling. This clears `gh_run_id` so `clean_ghosts` won't kill the still-running worker job.
- Headnode: only cancels **pending** jobs on the same branch (not running/assigned). Enforces "only one pending per branch".
- Workers: running jobs **continue uninterrupted** without any cancellation signal.

**Example scenario on `main`:**
1. Job A is **running** on `main`. GHA workflow #1 monitors it.
2. User pushes → GHA workflow #2 starts → GHA kills workflow #1 (concurrency).
3. `submit_job.py` (workflow #1) detaches GHA from job A → workers keep running.
4. `submit_job.py` (workflow #2) submits job B → headnode queues it as **pending**.
5. User pushes again → GHA workflow #3 starts → kills workflow #2.
6. `submit_job.py` (workflow #2) detaches from job B → headnode cancels job B (pending, replaced by C).
7. Job A finishes → Job C starts executing.

---

## 2. Branch Exclusivity vs Intra-Job Parallelism (v3)

A core tenet of Cluster-CI is preventing conflicting writes on the same Git branch:

* **Inter-Job Exclusivity**: Two distinct jobs targeting the same repository and branch cannot execute simultaneously. A second job on the same branch must wait in the queue until the active job completes or is cancelled.
* **Intra-Job Parallelism (`PARALLEL_STAGES=true`)**: In parallel DAG mode, multiple worker nodes (up to 8) can operate concurrently on the **same job** and branch. Concurrent Git pushes of metrics and `dvc.lock` are harmonized by the automated `dvc.lock` merge driver and exponential backoff pull/rebase routines.

---

## 3. Signal Propagation & Multi-Worker Cancellation

When a job cancellation is initiated (via GHA workflow cancellation, terminal `Ctrl+C`, or Dashboard **Stop**):

```text
GitHub Actions / Client -> [SIGTERM] -> submit_job.py
                                              │
                                   [POST /api/jobs/<id>/stop]
                                              │
                                      Headnode Service
                                              │
                                 ┌────────────┴────────────┐
                                 ▼                         ▼
                        Worker A (/cancel/<id>)   Worker B (/cancel/<id>)
                                 │                         │
                        [Kill Process Tree]       [Kill Process Tree]
                        [Purge VRAM / RAM]        [Purge VRAM / RAM]
```

<!-- v3: à vérifier contre l'implémentation : endpoint /api/jobs/<id>/stop et diffusion à active_workers -->

1. The cancellation request notifies the headnode.
2. The headnode marks remaining unexecuted stages as `blocked`.
3. The headnode iterates over all active workers assigned to the job (`active_workers`) and dispatches `POST /cancel/<job_id>` to each worker agent.
4. Each worker kills its container and process hierarchy, purging VRAM and returning to idle within seconds.

---

## 4. Headnode Auto-Cancellation Rules

On every `/submit_job` call, the headnode scans active jobs and applies cancellation rules:

| Branch Type | Pending | Assigned | Running |
|-------------|---------|----------|---------|
| `cluster-draft/*` | ✅ Cancel | ✅ Cancel | ✅ Cancel |
| Non-draft (`main`, etc.) | ✅ Cancel | ❌ Preserve | ❌ Preserve |

Cancelled job IDs are injected into the new job's environment via `CLUSTER_CANCELLED_RUNS`, so the worker can log which runs were replaced.
