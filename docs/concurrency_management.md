# Concurrency Management & Signal Propagation

This document describes how Cluster-CI handles GitHub Actions job concurrency and ensures that compute resources on Workers are correctly freed.

## Concurrency Model

Cluster-CI uses a **dual-mode concurrency strategy** based on branch type:

### Draft Branches (`cluster-draft/*`) — Aggressive Cancel

Used by `cluster-run` for fast iteration. A new submission **immediately cancels** any active job (pending, assigned, or running) for the same user/branch.

- GHA: `cancel-in-progress: true` → kills the old workflow run instantly.
- Headnode: auto-cancels all active jobs matching the same user or draft branch.
- Worker: receives `/cancel/<job_id>` → eradicates process tree, purges VRAM, frees RAM.

### Non-Draft Branches (`main`, `feature/*`, etc.) — Queue & Replace

Used for production pipelines. A new submission **does not cancel** the running job.

- GHA: `cancel-in-progress: false` → the new workflow is **queued** until the active one finishes. GHA itself enforces "only one pending per concurrency group" — any older pending run is cancelled.
- Headnode: only cancels **pending** jobs on the same branch (not running/assigned). This enforces "only one pending per branch" at the scheduler level too.
- Worker: the running job completes normally without interruption.

**Example scenario on `main`:**
1. Job A is **running** on `main`.
2. User pushes → Job B is submitted → joins the queue as **pending**.
3. User pushes again → Job B (pending) is **cancelled** and replaced by Job C.
4. Job A finishes → Job C starts executing.

## GHA Concurrency Configuration

In `.github/workflows/cluster-ci.yml`:
```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: ${{ startsWith(github.ref_name, 'cluster-draft/') }}
```

The concurrency group is scoped per branch (`github.ref`), ensuring that different branches/PRs never interfere with each other.

## Signal Propagation (Draft Branches)

When GHA cancels a workflow run (draft branches only), the signal propagation chain is:

1. **GHA → Headnode**: GitHub sends `SIGTERM` to the runner process executing `submit_job.py`.
2. **Headnode → Worker**: `submit_job.py` intercepts the signal and sends a POST to the Worker's `/cancel/<job_id>` endpoint.
3. **Worker**: Eradicates the entire process tree (host PID SIGKILL), removes Docker containers (`docker rm -f`), and purges Ollama VRAM.

```text
GitHub Actions -> [SIGTERM] -> Headnode (submit_job.py)
                                     |
                                     v
                          Worker (/cancel/<job_id>)
                                     |
                                     v
                          [Kill Process Tree] -> RAM Free
```

## Headnode Auto-Cancellation Logic

On every `/submit_job` call, the headnode scans active jobs and applies cancellation rules:

| Branch Type | Pending | Assigned | Running |
|-------------|---------|----------|---------|
| `cluster-draft/*` | ✅ Cancel | ✅ Cancel | ✅ Cancel |
| Non-draft (`main`, etc.) | ✅ Cancel | ❌ Preserve | ❌ Preserve |

Cancelled job IDs are injected into the new job's environment via `CLUSTER_CANCELLED_RUNS`, so the worker can log which runs were replaced.
