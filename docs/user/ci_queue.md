# CI Pipeline & Queue Scheduling Guide

When you run `cluster-run`, your job enters a queue and is automatically assigned to the best available GPU worker. This guide explains how jobs are scheduled and what rules apply.

---

## 1. Job Lifecycle

When you trigger a run using `cluster-run`, your job goes through these stages:

1.  **Submission**: Your code is pushed to GitHub, which notifies the cluster.
2.  **Queue**: The job enters a FIFO queue with a `pending` status.
3.  **Scheduling**: Every 5 seconds, the scheduler evaluates pending jobs and matches them to available workers.
4.  **Execution**: The selected worker prepares a Docker container, installs your dependencies, and runs your DVC pipeline.
5.  **Results**: Metrics and plots are committed back to your Git branch; large outputs are stored in the DVC cache.

You can monitor all these stages on the [Web Dashboard](dashboard.md).

---

## 2. Placement Rules

The scheduler must find a worker that satisfies **all** of the following constraints. If no worker qualifies, your job stays in the queue until one becomes available.

### Branch Exclusivity
Only **one job per repository + branch** can be active at a time. This prevents Git conflicts and data corruption.

### RAM Constraint
The cluster reserves 8 GB of RAM on each worker for the operating system. A worker is eligible only if:

> **Worker Total RAM − 8 GB ≥ your `REQUIRED_RAM`**

For example, a 128 GB worker can accept jobs requesting up to 120 GB. If you request more, the job stays in the queue indefinitely.

→ Set `REQUIRED_RAM` in your [`.cluster-ci` configuration](configuration.md).

### GPU VRAM Constraint
If you specify `REQUIRED_VRAM`, the worker must have a GPU with at least that much video memory. If omitted, the default is 0 (the job can run on CPU-only nodes).

→ Set `REQUIRED_VRAM` in your [`.cluster-ci` configuration](configuration.md).

### Worker Whitelist
If you specify `ALLOWED_WORKERS` in `.cluster-ci`, only workers whose hostname matches the list will be considered.

→ See [Configuration Reference](configuration.md) for details.

---

## 3. Worker Selection (Data Locality)

When multiple workers satisfy the placement rules, the scheduler picks the one that **already has your data cached locally**. This avoids re-downloading large datasets and speeds up execution.

??? note "How does data locality scoring work?"
    The scheduler parses your `dvc.lock` file, contacts each eligible worker to check which DVC-cached files they already have, and assigns a score based on cache hits. The worker with the highest score wins. The head node receives a small penalty (-1) to keep it free for scheduling duties. If the winning worker is missing some files, they are transferred directly from a peer worker over the internal network.

---

## 4. Runtime Limits & Auto-Cancellation

### Timeout Watchdog
Every job must specify `MAX_RUNTIME_HOURS` in its `.cluster-ci` file (maximum: 24 hours). If your job exceeds this limit, it is automatically terminated.

### Cancellation from the Dashboard
You can cancel a running job from the [Dashboard](dashboard.md) by clicking the **Stop** button. The worker detects the cancellation within seconds and releases its resources.

### Auto-Cancellation on New Submissions

=== "Draft Branches (`cluster-draft/*`)"
    *   **Rule**: Only **one active job per researcher** across the entire cluster.
    *   When you push a new draft run, all your previous pending or running jobs are cancelled automatically.

=== "Standard Branches (`main`, `feature/*`)"
    *   **Rule**: Only **one pending job per branch**.
    *   When you push a new commit, older pending jobs on the same branch are cancelled. Jobs already running are allowed to finish.

??? note "Internal Details"
    The cluster also runs automatic watchdogs for GPU memory leaks, zombie processes, and intermediate DVC backups. For technical details on Ollama VRAM Purge, JIT Host Cleaning, Double-Threshold GPU Memory Guard, and DVC Git Watchdog, see the [Infrastructure Internals](../admin/infrastructure_internals.md) documentation.
