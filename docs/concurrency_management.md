# Concurrency Management & Signal Propagation

This document describes how Cluster-CI handles GitHub Actions job concurrency and ensures that compute resources on Workers are correctly freed.

## Concurrency Model

Cluster-CI uses a **dual-mode concurrency strategy** based on branch type:

### Draft Branches (`cluster-draft/*`) — Aggressive Cancel

Used by `cluster-run` for fast iteration. A new submission **immediately cancels** any active job (pending, assigned, or running) for the same user/branch.

- GHA: `cancel-in-progress: true` → kills the old workflow run instantly.
- `submit_job.py`: signal handler propagates full cancellation to the worker.
- Headnode: auto-cancels all active jobs matching the same user or draft branch.
- Worker: receives `/cancel/<job_id>` → eradicates process tree, purges VRAM, frees RAM.

### Non-Draft Branches (`main`, `feature/*`, etc.) — Detach & Queue

Used for production pipelines. A new submission **does not cancel** the running job.

- GHA: `cancel-in-progress: true` → the old **monitoring workflow** is replaced, but...
- `submit_job.py`: signal handler sends `detach_gha=True` to headnode instead of cancelling. This clears `gh_run_id` so `clean_ghosts` won't kill the still-running worker job.
- Headnode: only cancels **pending** jobs on the same branch (not running/assigned). Enforces "only one pending per branch".
- Worker: the running job **continues uninterrupted** without any signal.

**Example scenario on `main`:**
1. Job A is **running** on `main`. GHA workflow #1 monitors it.
2. User pushes → GHA workflow #2 starts → GHA kills workflow #1 (concurrency).
3. `submit_job.py` (workflow #1) detaches GHA from job A → worker keeps running.
4. `submit_job.py` (workflow #2) submits job B → headnode queues it as **pending**.
5. User pushes again → GHA workflow #3 starts → kills workflow #2.
6. `submit_job.py` (workflow #2) detaches from job B → headnode cancels job B (pending, replaced by C).
7. Job A finishes → Job C starts executing.

## GHA Concurrency Configuration

In `.github/workflows/cluster-ci.yml`:
```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.actor }}-${{ github.ref_name }}
  cancel-in-progress: false
```

The concurrency group is scoped per branch (`github.ref`), ensuring that different branches/PRs never interfere with each other. The actual non-cancellation logic is handled at the application level in `submit_job.py`.

## Signal Propagation

### Draft Branches (Full Cancel)

```text
GitHub Actions -> [SIGTERM] -> submit_job.py
                                     |
                      [propagate cancellation]
                                     |
                          Worker (/cancel/<job_id>)
                                     |
                          [Kill Process Tree] -> RAM Free
```

### Non-Draft Branches (Detach & Continue)

```text
GitHub Actions -> [SIGTERM] -> submit_job.py
                                     |
                      [detach_gha=True to headnode]
                      [clear gh_run_id in DB]
                                     |
                          Worker: (no signal, job continues)
```

## Headnode Auto-Cancellation Logic

On every `/submit_job` call, the headnode scans active jobs and applies cancellation rules:

| Branch Type | Pending | Assigned | Running |
|-------------|---------|----------|---------|
| `cluster-draft/*` | ✅ Cancel | ✅ Cancel | ✅ Cancel |
| Non-draft (`main`, etc.) | ✅ Cancel | ❌ Preserve | ❌ Preserve |

Cancelled job IDs are injected into the new job's environment via `CLUSTER_CANCELLED_RUNS`, so the worker can log which runs were replaced.
