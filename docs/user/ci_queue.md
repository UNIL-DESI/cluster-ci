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

### Double-Threshold GPU Memory Guard & Host Watchdog
To prevent jobs from freezing the operating system or starving other tasks on unified memory platforms, `cluster-ci` implements a double-threshold memory protection system.

1.  **Process-Level CUDA Hard-Cap**: The script `src/runner/gpu_memory_guard.py` is injected into the container's `PYTHONSTARTUP` environment variable. Before user code executes, it checks `CLUSTER_CI_VRAM_LIMIT_GB`. If set, it calculates the VRAM fraction and hard-caps PyTorch's memory allocation via:
    `torch.cuda.set_per_process_memory_fraction(fraction, device=0)`
2.  **Unified Memory (Grace Blackwell) Fallback**: On unified systems (e.g. Grace Blackwell GB10), standard CUDA device queries for total memory return `0` or `[N/A]`. The guard detects this and falls back to reading system RAM from `/proc/meminfo` (`MemTotal:`), which serves as the shared CPU-GPU pool.
3.  **Host-Level Dual Threshold Watchdog**: The host script `src/runner/gpu_watchdog.sh` monitors the running container every 2 seconds:
    *   **Discrete GPU Mode**: Queries `nvidia-smi --query-gpu=memory.used`.
    *   **Unified Memory Mode**: Fallback to `/proc/meminfo` (`MemTotal - MemAvailable`).
    *   **Soft VRAM Threshold (User-declared)**: If memory exceeds this limit, it flags a violation. If the violation persists for `SOFT_THRESHOLD = 2` consecutive checks (4 seconds grace period), the watchdog kills the container (`docker kill`).
    *   **Hard RAM Threshold (90% System RAM)**: If memory usage exceeds 90% of total system RAM, the watchdog immediately kills the container on the very first check to protect the host OS from kernel panic/OOM lockups.

### Zombie GC (Multi-dimensional Inactivity)
To prevent orphaned or stuck container processes from indefinitely consuming cluster resources, `src/runner/gc_orchestrator.py` runs a **Zombie Garbage Collection** daemon via `run_zombie_gc()`.

1.  **Container Filtering**: The GC scans for running Docker containers matching the name pattern `cluster-job-*`.
2.  **Multi-Dimensional Inactivity Check**: For each container, it evaluates activity across three dimensions:
    *   **Logs**: Checks the modification timestamp (`st_mtime`) of `job_logs/{job_id}.log`. If the log file has not been modified since the last check, it counts as inactive.
    *   **CPU & Network**: Queries `docker stats` for CPU percentage and network IO bytes. If CPU usage is `<= 0.1%` and network traffic is identical to the previous check, it counts as inactive.
    *   **GPU Utilization**: Runs `nvidia-smi` to verify if the sum of GPU utilization percentages is `0%`.
3.  **10-Minute Timeout Termination**: If inactivity is detected across all three dimensions, a persistent timer is incremented in `zombie_registry.json`. If this inactivity exceeds `ZOMBIE_TIMEOUT_MINUTES = 10` minutes, the container is forcibly removed (`docker rm -f`), and any associated viewer containers or host `dvc-viewer` processes are killed.

### DVC Git Watchdog (Incremental Backups)
The cluster implements an asynchronous watchdog (`src/runner/dvc_watchdog.sh`) to perform intermediate Git commits of metrics and plots during long-running training stages.

1.  **Lock File Monitoring**: The watchdog runs as a host-level background daemon, polling the project's `dvc.lock` file every 2 seconds.
2.  **DVC Status Interlocking**: When `dvc.lock` is modified, the script waits 2 seconds for disk writes to settle. It inspects `.dvc/tmp/iterative-status.json`. If the JSON file reports `running: true`, it defers the synchronization to prevent file locking conflicts.
3.  **Intermediate Commits**: If no stage is actively writing, it executes `dvc_git_helper.py sync` inside the container:
    *   **Staging changes**: Stages `dvc.lock` if modified. It reads `dvc.yaml` to identify all metrics/plots files configured with `cache: false`.
    *   **Size Constraint**: If a metric file is `< 5 MiB` and has local modifications, it is staged via `git add -f <path>`. Files `>= 5 MiB` are skipped to protect repository size.
    *   **Push Reconciliation**: Commits the files under `cluster-ci-bot` (`bot@cluster-ci.io`) with `[skip ci]` tags. It then runs `git push origin HEAD`. If the push fails, it executes `git pull --rebase` to resolve remote updates, pushes again, and aborts (`git rebase --abort`) if conflicts cannot be resolved.

### Aggressive Auto-Cancellation Rules
To optimize cluster resources, Cluster-CI applies the following cancellation rules upon new submissions:

=== "Draft Branches (`cluster-draft/*`)"
    *   **Rule**: **One active job per researcher** across the entire cluster.
    *   **Behavior**: When a researcher pushes a new draft commit, the scheduler searches for *any* active job (pending or running) associated with that user's GitHub username across **all repositories**. All previous runs are cancelled and terminated.

=== "Standard Branches (`main`, `feature/*`)"
    *   **Rule**: **One pending job per branch**.
    *   **Behavior**: When a new commit is pushed, the scheduler identifies older jobs on the **same repository and same branch** that are still in the `pending` queue. These pending jobs are cancelled. Jobs that have already started running are allowed to complete.
