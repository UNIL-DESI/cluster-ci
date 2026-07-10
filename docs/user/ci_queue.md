# CI Pipeline and Queue Scheduling Guide

Cluster-CI orchestrates workload execution through an automated CI pipeline and a central queue scheduler. This guide explains how jobs are queued, evaluated, allocated to workers, and safely cleaned up.

---

## 1. Job Lifecycle in the CI Queue

When you trigger a run using the `cluster-run` client, the job flows through the following stages:

![Scheduling Queue](../assets/images/scheduling_queue.png)

```
[Local Client] -> (Shadow Push) -> [GitHub Actions Runner] -> (API Submit) -> [Headnode Queue]
                                                                                   |
                                                                           (Scheduler Loop)
                                                                                   |
[Post-Run Sync] <- (Git Push) <- [Worker Job Container] <- (JIT Clean) <- [Worker Selected]
```

1.  **Submission**: The client pushes a shadow commit to GitHub. This triggers the GitHub Actions workflow, which sends an API request to the headnode's `/submit_job` endpoint containing job metadata.
2.  **Queue Placement**: The job is stored in a database queue with a `pending` status.
3.  **Scheduling Evaluation**: Every 5 seconds, the headnode scheduler runs an evaluation loop. It matches pending jobs against available online workers based on hard constraints and data locality scores.
4.  **Worker Dispatch**: The selected worker downloads the job parameters, runs a series of JIT sanitization steps, starts the Docker execution container, and streams the logs.
5.  **Teardown & Push**: Once completed, the worker commits updated metrics/plots, pushes them back to Git, and releases its resources.

---

## 2. Hard Constraints (Eligibility Rules)

A worker must satisfy all the following hard constraints to be eligible for a job. If no worker meets these requirements, the job remains in the queue.

### A. Branch Exclusivity
To prevent race conditions, write conflicts on the DVC remote, and Git push collisions, **only one active job (either running or assigned) is allowed per repository + branch combination**. 

### B. Physical RAM Headroom (OS Reserve)
Memory footprint evaluation is strict. A worker is eligible only if:
$$\text{Total Worker RAM (GB)} - 8.0 \ge \text{Requested RAM (GB)}$$

*   **The 8 GB Headroom Rule**: Cluster-CI strictly reserves 8 GB of RAM for the worker's host operating system, Docker daemon, and monitoring agents. This headroom is crucial on system architectures with unified memory (like NVIDIA Grace Blackwell GB10) to prevent kernel out-of-memory (OOM) freezes.
*   **Example**: If a worker has 128 GB of total RAM, the maximum allocatable RAM for a single job is $128 - 8 = 120\text{ GB}$. If you request `REQUIRED_RAM=125GB` in your `.cluster-ci` file, the job will be blocked in the queue indefinitely.

### C. Physical VRAM Allocation
The target worker must have a physical GPU with:
$$\text{Worker VRAM (GB)} \ge \text{Requested VRAM (GB)}$$
If you do not specify a VRAM constraint, it defaults to 0 (allowing execution on CPU-only nodes if available).

### D. Allowed Workers Whitelist
If your `.cluster-ci` configuration specifies `allowed_workers` (a list of hostnames), the scheduler filters out any worker whose hostname is not in the whitelist. This lets you target specific GPU architectures (e.g. GB10 Blackwell vs RTX 3090).

---

## 3. Soft Constraints (Data Locality & Scheduling Scores)

When multiple workers satisfy the hard constraints, the scheduler computes a score to select the best worker.

1.  **DVC Cache Locality Scoring**:
    - The headnode inspects your job's `dvc.lock` and parses the list of required MD5 hashes.
    - It queries each eligible worker's `/check_cache` endpoint.
    - The worker counts how many of those MD5 files are already present in its local DVC cache (`.dvc/cache/files/md5`).
    - The worker's locality score is equal to the number of cache hits. The worker with the highest score wins.
2.  **Headnode Malus**:
    - A penalty of **-1** is subtracted from the headnode's locality score.
    - This deprioritizes scheduling jobs on the node hosting the scheduler, preserving headnode CPU cycles for API handling and database management.
3.  **P2P Remote Injection**:
    - If the winning worker lacks some of the dataset files, the scheduler searches for the best peer worker (holding a score > 0).
    - It injects the peer worker's `/fetch_artifact` endpoint into the executing worker's environment as `DVC_REMOTE_P2P_URL`.
    - The worker pulls missing files directly from its neighbor at high speed over the LAN instead of pulling from Google Drive.

---

## 4. JIT Worker Sanitization (VRAM & Resource Recovery)

Before starting the Docker container for a job, the worker agent executes automated host cleaning routines:

### Ollama VRAM Purge
On Blackwell GB10 workers, researchers often run local large language model (LLM) instances using Ollama. To prevent VRAM fragmentation and guarantee that 100% of the GPU memory is available to your job:
*   The worker agent queries any active Ollama service on the default ports (11434 and 11435).
*   It issues an unload command to the Ollama API (`/api/generate`) with `keep_alive: 0`.
*   This immediately unloads all active LLMs from VRAM in **less than 5 seconds**, freeing up all VRAM before the job container starts.

### JIT Host Cleaning
The agent scans the host system and forcefully terminates:
*   Zombie or orphaned containers named `cluster-job-*` or `cluster-viewer-*`.
*   Orphaned host processes associated with previous runs (such as residual `dvc-viewer` instances or `gc_orchestrator` tasks).
*   Any process binding to custom ports requested by previous jobs.

---

## 5. Security Watchdogs & Auto-Cancellation

### MAX_RUNTIME_HOURS Watchdog
Every job must specify `MAX_RUNTIME_HOURS` in its `.cluster-ci` configuration (maximum value: 24).
*   A runtime watchdog tracks the job's duration.
*   If the execution exceeds this limit, the worker agent kills the Docker container and stops the job.

### Self-Healing Watchdog
The worker agent runs a background loop that polls the headnode's `/job_status/{job_id}` endpoint every few seconds.
*   If the job's state is changed to `cancelled` or `stopped` on the headnode (e.g. by a user clicking "Cancel" on the dashboard), the worker's watchdog detects it.
*   It kills the local Docker container, stops the processes, runs JIT cleanup, and releases the VRAM/RAM allocation immediately.

The cluster also runs automatic watchdogs for GPU memory leaks, zombie processes, and intermediate DVC backups. These mechanisms are transparent to the researcher and require no configuration.

!!! note "Internal Details"
    For technical details on the Double-Threshold GPU Memory Guard, Zombie GC, and DVC Git Watchdog, see the [Infrastructure Internals](../admin/infrastructure_internals.md#4-runtime-watchdogs) documentation.

### Aggressive Auto-Cancellation Rules
To optimize cluster resources, Cluster-CI applies the following cancellation rules upon new submissions:

=== "Draft Branches (`cluster-draft/*`)"
    *   **Rule**: **One active job per researcher** across the entire cluster.
    *   **Behavior**: When a researcher pushes a new draft commit, the scheduler searches for *any* active job (pending or running) associated with that user's GitHub username across **all repositories**. All previous runs are cancelled and terminated.

=== "Standard Branches (`main`, `feature/*`)"
    *   **Rule**: **One pending job per branch**.
    *   **Behavior**: When a new commit is pushed, the scheduler identifies older jobs on the **same repository and same branch** that are still in the `pending` queue. These pending jobs are cancelled. Jobs that have already started running are allowed to complete.
