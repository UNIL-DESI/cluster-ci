# Cluster CI

A minimalist, decentralized GitOps orchestrator for data processing and model training on a **heterogeneous multi-architecture cluster** (ARM64 Blackwell workers + AMD x86_64 dual-mode scheduler/executor headnode).
**Current Status**: Operational system. The heterogeneous cluster natively supports ARM64 and AMD64 architectures through automatic host architecture detection (`uname -m`) and dynamic Docker image selection (`DOCKER_IMAGE_AMD64` / `DOCKER_IMAGE_ARM64`). The unified NGC container `nvcr.io/nvidia/pytorch:26.05-py3` (Python 3.12, PyTorch 2.12, CUDA 13.2) is used across both architectures with automatic injection of the `--platform` flag. Integration of the native Docker init system (`--init`) structurally eradicates zombie DVC processes `[dvc] <defunct>` in the main container, native real-time lossless line-by-line log streaming (via active polling and direct linear streaming to eliminate ANSI/tmux noise and prevent word truncation), robust 5-second remote log buffer drainage (ppng.io) upon run completion to prevent final trace loss, an interactive and transparent queue dashboard for researchers (queue position, detailed running job status per researcher with RAM/duration, and automated physical RAM diagnostics), real-time intermediate automatic synchronization of metrics, plots, and `dvc.lock` locally after each successful DVC stage (preventing progress loss during execution), complete inter-worker homogenization (SSH RSA pairing and automatic model cache synchronization), optimized asynchronous backup of large DVC data files via the Lazy Garbage Collector to preserve network bandwidth at the end of each CI job, an advanced auto-cancellation system by branch category (cluster-draft vs normal) with cross-repo per-user cancellation for cluster-run (only one active cluster-run per researcher) and an intelligent queue with waiting reasons, highly resilient live log streaming against network blips (with automatic exponential backoff reconnection, line-based deduplication, and elimination of synchronous CLI blocking via strict timeouts) eliminating GitHub Actions workflow hang-ups, a sovereign centralized Watchdog guaranteeing strict enforcement of execution timeouts with a 5-minute grace period, a robust exponential backoff retry mechanism for the worker agent resisting transient Headnode API blips during restart/GitOps phases, complete host self-healing via strict Single Instance Enforcement coupled with ultra-deterministic JIT (Just-In-Time) purging of zombie containers and orphaned runner processes (protecting active GHA runners in delegation mode during dual-mode headnode-as-worker), and an **ultra-fast physical resource reconciliation system** (freeing physical RAM and Ollama VRAM on Blackwell in under 5 seconds) propagating external cancellation signals end-to-end for optimal queue efficiency and responsiveness.

Asynchronous continuous integration system for research pipelines, designed as a pull-based replacement for the legacy SlurmRay push-based architecture. This repository hosts the scripts necessary to configure a GitHub Actions Self-Hosted Runner on the target Ubuntu machine, orchestrating `uv run dvc repro` executions in local environments and managing silent authentication with Google Drive. It also provides the client script allowing any research repository to interface with this cluster.

## Cluster Hardware Specifications

| Property | Workers (ARM64) | Headnode (AMD x86_64, dual-mode) |
|----------|----------|-------|
| **Role** | Executor | Scheduler + Executor (dual-mode) |
| **GPU** | NVIDIA GB10 (Blackwell) | 2× NVIDIA RTX 3090 (48 GB VRAM) |
| **CPU** | ARM64 — Cortex-X925 + Cortex-A725 | AMD Ryzen 9 3900X (24 threads) |
| **RAM** | 128 GB unified memory | 125 GB (discrete) |
| **OS** | Ubuntu 24.04.4 LTS | Ubuntu 20.04 |
| **Docker Image** | `nvcr.io/nvidia/pytorch:26.05-py3` (`DOCKER_IMAGE_ARM64`) | `nvcr.io/nvidia/pytorch:26.05-py3` (`DOCKER_IMAGE_AMD64`) |
| **Python** | 3.12 | 3.12 |
| **PyTorch** | 2.12 (CUDA 13.2) | 2.12 (CUDA 13.2) |
| **Storage** | ~3.2 TB | ~938 GB |

## Cluster-CI v3 : Parallel DAG & Granular Resource Scheduling

Cluster-CI v3 introduces distributed DAG branch execution and fine-grained resource definitions:
* **Per-Stage Resources (`meta.cluster`)**: Declare CPU, RAM, VRAM, storage, custom Docker images, and worker whitelists per stage directly in `dvc.yaml` ([Documentation](docs/user/stage_resources.md)).
* **Parallel DAG Scheduling (`PARALLEL_STAGES=true`)**: Independent branches execute concurrently across up to 8 cluster machines ([Documentation](docs/user/parallel_execution.md)).
* **Fair-Share Multi-Machine Balancing**: Each job receives a guaranteed Home Worker; additional machines are dynamically rebalanced at stage boundaries without preemption ([Documentation](docs/user/ci_queue.md)).
* **Automatic Hardware Telemetry**: Workers self-report compute, RAM, VRAM (per-GPU and unified Grace Blackwell pools), and disk availability with zero headnode reconfiguration.
* **Conflict-Free Git Synchronization**: Concurrent stage outputs are committed and merged transparently via an automated `dvc.lock` merge driver.

See the complete documentation at [https://unil-desi.github.io/cluster-ci/](https://unil-desi.github.io/cluster-ci/).

# Installation

### 1. Client Installation (Research Project)

Run this command at the root of your Git repository for automatic integration:

```bash
curl -H 'Cache-Control: no-cache, no-store' -sSL "https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh?v=$(date +%s)" | bash
```

> [!IMPORTANT]
> **Windows User Note**: Execute this command using a **Git Bash** terminal. Executing it directly in PowerShell will fail because `curl` is aliased to `Invoke-WebRequest`, which handles headers differently. Alternatively, run: `bash -c "curl -H 'Cache-Control: no-cache, no-store' -sSL \"https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh?v=\$(date +%s)\" | bash"`.

This script injects:
1. The GitHub Actions workflow (`.github/workflows/cluster-ci.yml`)
2. The DVC control file (`.cluster-ci`)
3. **The agent guidelines file (`AGENTS.md`)** containing cluster architecture constraints (Python 3.12, PyTorch 2.12, CUDA 13.2) to prevent AI dependency errors on this repository.
4. **The Pre-flight Scanner (Git Hook)**: An interactive pre-commit hook that validates local compatibility with the ARM64 cluster and proposes automated fixes (with robust Python binary detection avoiding dummy Windows Store stubs on Windows).
5. **The `cluster-run` CLI**: Local command to submit and track jobs directly from your terminal (see below).

#### `cluster-run` Command

The `cluster-run` command is **100% compatible with Windows (PowerShell/CMD), Linux, and macOS**. It uses "Shadow Push" to submit your local changes (including uncommitted and untracked files) to the remote cluster without polluting your Git history.

- **On Linux / macOS**: After running the `install.sh` script above, the binary is available in `~/.local/bin/cluster-run`.
- **On Windows (Native)**: The wrapper scripts `cluster-run.bat` and `cluster-run.ps1` are directly available at the root of your research repository. You can run `.\cluster-run` under PowerShell or `cluster-run` under CMD seamlessly. To access it globally, simply add your repository directory to your Windows `PATH`.

| Command | Description |
|---|---|
| `cluster-run` | Submits a job and streams execution logs line-by-line in real time to your original terminal without data loss |
| `cluster-run --local` | Direct execution on Headnode for sensitive/confidential data (zero GitHub push, local ingestion, HTTP 400 validation) |
| `cluster-run list` | Lists recent runs |
| `cluster-run view [run_id]` | Displays logs for a run (latest by default) |
| `cluster-run cancel [run_id]` | Cancels a run and cleans up the branch |
| `cluster-run sync` | Manually retrieves results (metrics, plots, dvc.lock) from the cluster |

> [!NOTE]
> To process confidential or NDA-bound data without any upload to GitHub, see the guide **[Sensitive Data & Local Execution](docs/user/sensitive_data.md)**.

**Robustness**: Partial results are automatically synchronized locally regardless of run outcome (success, failure, Ctrl+C). In case of local process force-kill, the next `cluster-run` invocation automatically detects and cleans up the orphaned run on GitHub Actions. Complete logs are redirected to a local `.cluster-ci-logs/` folder (automatically excluded via `.gitignore`), with full console output and complete duplication to a local log file (with automatic rotation keeping only the 5 most recent files). To avoid unnecessary console noise, internal infrastructure files (files under `.dvc-viewer/hashes/` and `dvc.lock`) are retrieved completely silently, while user metrics and plots are explicitly listed upon completion.

### Cluster Deployment (Headnode & Workers)

Installation is done via a "One-Liner" curl command that automatically configures the environment and systemd services.

#### 1. Install the Headnode (Scheduler)
The Headnode manages the job queue and ephemeral runners. The script will ask for your **GitHub PAT** and the target to monitor.
```bash
curl -H 'Cache-Control: no-cache, no-store' -sSL "https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh?v=$(date +%s)" | bash -s -- headnode
```

#### 2. Install a Worker (Executor)
Once the Headnode is installed, it will provide a ready-to-use command to run on your Workers. Alternatively, you can start the installation manually:
```bash
curl -H 'Cache-Control: no-cache, no-store' -sSL "https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh?v=$(date +%s)" | bash -s -- worker
```
The script will ask for the **Headnode URL** and the **Cluster Token** generated during Headnode installation.

#### Post-Installation Configuration
Once installed, you can add secrets (GCP, HuggingFace) to the `.env.secrets` file located in the installation folder (default `~/cluster-ci`).

To cleanly uninstall everything (systemd services, local cleanup):
```bash
cd ~/cluster-ci
./src/cluster/uninstall_runner.sh owner/repo
```

# Detailed Description

Cluster CI is based on GitOps principles. Instead of the agent trying to maintain a continuous interactive session on the remote machine (a structural issue with the Joules Agent on long research jobs), execution is delegated to a self-hosted GitHub Actions runner installed as a `systemd` service on the machine.

**Execution Flow**:
1. **Pull Request**: Joules (the coding agent) pushes changes to a GitHub branch or tag.
2. **CI Trigger**: GitHub Actions triggers when the `cluster-run` tag is pushed to the repository (via the local `cluster-run` CLI).
3. **Orchestration**: The setup script switches to an untracked local cache directory (`repositories/$ORG/$REPO_NAME`), performs a `git fetch` and a forced `git checkout` of the tag/branch (to keep DVC state intact across branches).
4. **Execution**: The orchestrator detects the `.cluster-ci` file, prepares the environment via `uv sync`, and runs `uv run dvc repro` with the provided arguments.
5. **Authentication**: The runner silently injects credentials (Google Drive) by sourcing the global cluster `.env` and `.env.secrets` files.
6. **CI Feedback**: Joules receives native failure and success notifications via GitHub PR integration.
7. **`.cluster-ci` Configuration**: Jobs requiring scheduling can declare the following parameters at the root:
    - `REQUIRED_RAM=16GB`: RAM placement constraint (default: 10GB).
    - `REQUIRED_VRAM=24GB`: VRAM placement constraint **per GPU** (default: 0, no constraint). Two 24GB GPUs do not satisfy a 32GB request. The watchdog monitors the most loaded card, without summing cards; on GB10 nodes, it monitors system RAM. See [watchdog limits and tests](docs/gpu-watchdog.md).
    - `MAX_RUNTIME_HOURS=24`: Maximum execution duration (**MANDATORY**, max 24h) to avoid zombie processes.
    - `EXPOSED_PORT=8501`: Enables routing to a web GUI (e.g. Streamlit, Gradio) on the specified port.
   Once allocated, the container has access to 100% of host RAM to avoid artificial limits.
8. **Agent Resilience and Robustness**: To prevent thread crashes from isolating a worker (a historical issue during network blips with the Headnode or SQLite locks), the agent worker loop incorporates a global exception handler with emergency auto-cleanup. All physical release operations (container destruction by isolating host PID and offloading Ollama VRAM) execute unconditionally in `finally` blocks or asynchronous cleanup daemons, ensuring a clean hardware reset in under 5 seconds.
# Key Results

- **Status**: Operational & secured against zombie processes, featuring robust client/server log streaming heartbeats and auto-reconnection watchdogs (Last updated: 29 June 2026 - resolved original draft branch from cluster-run tag for auto-cancellation). Includes hardware-level VRAM purging and a clean codebase free from legacy debugging/testing artifacts.

# Documentation Index

| Title (Link) | Description |
|--------------|-------------|
| [User Documentation Index (Home)](docs/index.md) | Main entry point for researchers: Onboarding, CLI Client, DVC, CI Queue, Dashboard, and Support |
| [Architecture Index](docs/index_architecture.md) | Architecture specifications and design notes |
| [Dashboard Index](docs/index_dashboard.md) | Specifications for the premium monitoring dashboard and bidirectional artifact explorer |
| [Pre-flight Index](docs/index_preflight.md) | Validation scanner and pre-commit logic |
| [Scheduler Index](docs/index_scheduler.md) | Resilience, JIT hardware reconciliation (<5s VRAM purge), chaos-engineering, and scheduler robustness |
| [Security Index](docs/index_security.md) | Security, risk analysis, and known vulnerability audit |
| [Tasks Index](docs/index_tasks.md) | Specifications index and development task tracking |
| [vLLM Index](docs/index_vllm.md) | Technical resolution of C++ ABI incompatibilities under NVIDIA NGC PyTorch containers |

# Repository Layout

```text
cluster-ci/
├── docs/           # Documentation, Index, and Task Specifications
├── install.sh      # Client-side installation script
├── scripts/        # Operational scripts (deployment)
└── src/            # Runner and Orchestrator scripts
    ├── cluster/    # Local runner setup and management (systemd)
    ├── runner/     # GitOps Orchestrator (run_research_pipeline.sh)
    └── scheduler/  # Headnode API, Worker Agent, and Persistence (SQLite)
```

# Main Entry Scripts

| Command | Description |
|----------|-------------|
| `install.sh` | Injects the GitHub Actions workflow and `.cluster-ci` file into a client repository |
| `src/cluster/setup_runner.sh` | Installs and configures the GitHub Actions runner as a `systemd` service |
| `src/cluster/uninstall_runner.sh` | Completely uninstalls the runner (Systemd, GitHub, local) |

# Secondary Executables & Utility Scripts

| Command | Description |
|----------|-------------|
| `src/scheduler/submit_job.py` | Client-side script (CLI) to manually submit a job, track queue status with interactive dashboard & resource diagnostics |
| `src/scheduler/headnode_service.py` | Headnode HTTP API exposing scheduler routes (including public `/scheduler_status`) |
| `src/scheduler/runner_manager.py` | Manages the lifecycle of ephemeral GitHub Actions runners (slot1, slot2) |
| `update_cluster.sh` | Updates the Headnode and Workers via SSH, uses an `.env` file to store credentials |
| `scripts/get_worker_details.py` | Audit and collection of hardware and software specs from remote workers via SSH |
| `uv run --with mkdocs-material mkdocs build` | Build the documentation site locally |
| `uv run --with mkdocs-material mkdocs serve` | Serve the documentation site locally with live reload |

# Roadmap

**Phase 1 (Foundation — Completed)**
- [x] [Orchestrator Runner Setup](docs/tasks/setup_orchestrator.md)
- [x] [Local Deployment & Runner Test](docs/tasks/deploy_local_cluster.md)
- [x] [Silent DVC Authentication](docs/tasks/dvc_auth.md)
- [x] [Client Installation Script](docs/tasks/client_script.md)
- [x] [Per-Repository Concurrency Management](docs/tasks/concurrency_management.md)

**Phase 2 (Reliability & UX — In Progress)**
- [x] Automated Deployment (`update_cluster.sh`) with E2E tests
- [x] Standard build configuration (`pyproject.toml`)
- [x] GitHub OAuth support for the Dashboard (with reverse proxy and IPv4 fallback support)
- [x] Dashboard UX improvement (date formatting, DVC path corrections under systemd, historical DVC run fixes)
- [x] Migration to Docker Worker execution (NVIDIA/ARM support)
- [x] Real-time Log Streaming via Headnode & Live Direct Terminal Stream (lossless direct streaming without interactive sub-terminal)
- [x] Resolution of GHA log buffering: Native direct real-time watch streaming without tmate/SSH dependency
- [x] End-to-end authentication token (GH_TOKEN) propagation in Delegation mode
- [x] Migration to modern NGC container (Python 3.12, PyTorch 2.12, CUDA 13.2)
- [x] [Cluster-CI Pre-flight Scanner & Pre-commit Validator](https://github.com/UNIL-DESI/cluster-ci/issues/55)
- [x] [Auto-generation of ARM64 constraints via CI](https://github.com/UNIL-DESI/cluster-ci/issues/56)
- [x] [Smart Environment Shims & Dynamic Client Sync](https://github.com/UNIL-DESI/cluster-ci/issues/57)
- [x] [Native GitHub Secrets Injection](https://github.com/UNIL-DESI/cluster-ci/issues/58)
- [x] [Strict Python environment isolation and GC integration](https://github.com/UNIL-DESI/cluster-ci/issues/59)
- [x] [Full Monitoring Dashboard & Real-time Logs](https://github.com/UNIL-DESI/cluster-ci/issues/60)
- [x] Smart Dependency Caching (hash-based skip of `uv pip install` when `pyproject.toml` unchanged)
- [x] Fix false-positive Exit Code -98 (Heartbeat/Worker crash detection race condition)
- [x] Resolution of DVC P2P Pull failure (residual files) in persistent cache
- [x] Resolution of HTTP 404 error for Live DVC Viewer behind reverse proxy (relative paths & `<base href>`)
- [x] DVC Historical Extraction: Dynamic credential injection (GITHUB_PAT) into local Git mirrors for `dvc get`
- [x] Text file preview truncation at 100 lines in Dashboard for UI optimization
- [x] Restrict *Live Viewer* to "Read-Only" mode and fix detection of DVC stages running inside worker Bash wrappers.
- [x] [Robust Docker Container Lifecycle and Orphan Process Eradication](https://github.com/UNIL-DESI/cluster-ci/pull/66)
- [x] [Hybrid Liveness Watchdog — JIT Zombie Detection](https://github.com/UNIL-DESI/cluster-ci/pull/67)
- [x] Fix Scheduler assigning jobs to busy workers (single-threaded worker exclusion)
- [x] Reversal of DVC/P2P order (Pull before Hash) and removal of Docker deletion errors.
- [x] Segmented Pipeline Logs: Interactive modal with per-stage navigation (Setup, DVC stages, Sync/GC), stage deduplication, cumulative status colors, progressive lazy loading, line indicators, robust clipboard copy with insecure HTTP fallback, smart "Last Error" button targeting failed sections, running stage animation, and ☠️ emoji with kill reasons
- [x] Fix Bug: `submit_job.py` read `.cluster-ci` from cluster-ci CWD instead of target repo → RAM always at 2GB in Delegation mode. Fixed via shallow clone of remote `.cluster-ci`.
- [x] Fix Bug: Jobs hung in infinite `pending` when RAM requested exceeded worker physical capacity (fail-fast implemented).
- [x] Fix Bug: `dvc-viewer` connection refused (port binding explicitly forced to 0.0.0.0 to bypass Docker IPv6/loopback isolation).
- [x] Fix Bug: Frontend UI prematurely showed `Post-Run` instead of `System/Logs` during pipeline execution.
- [x] Removed obsolete `SHARED_MEMORY` option (rendered unnecessary by `--ipc=host` which automatically allocates 50% of host RAM to `/dev/shm`) and added live OOM detection in `submit_job.py` for GitHub Actions.
- [x] Fix Bug: Ghost Workers — Scheduler automatically marks workers offline after 120s without heartbeat, preventing dashboard desynchronization.
- [x] Hardening: Added explicit `timeout=10` on all worker agent HTTP requests to prevent silent TCP deadlocks (university firewall).
- [x] Universal Windows Support & Automatic PATH (PowerShell/CMD): Automatic detection and registration of `~/.local/bin` into Windows User PATH via PowerShell, native wrappers, terminal freeze fix, and instant task completion.
- [x] Queue Transparency (Interactive Queue Dashboard): Queue position, live interactive logs of running tasks per researcher with RAM/duration, and automated physical RAM diagnostics in `submit_job.py`.
- [x] Inter-Worker Homogenization: Robust passwordless SSH RSA inter-worker link and automated Ollama model cache synchronization (20 GB Gemma-4-31B) via rsync.
- [x] Fix Bug: Definitive Ghost Jobs resolution via explicit timeouts and purge daemon thread.
- [x] [Global Execution Timeout](docs/tasks/global_timeout.md): Prevent worker hang on stalled jobs (clean Docker stop and researcher notification).
- [x] [Silent cgroups OOM: Add explicit error message when exceeding REQUIRED_RAM](https://github.com/UNIL-DESI/cluster-ci/issues/91)
- [x] [Architecture: Implement Asynchronous Watchdog for incremental DVC backups](https://github.com/UNIL-DESI/cluster-ci/issues/92)
- [x] [Architecture: Implement live log streaming for asynchronous jobs (cluster-run view)](https://github.com/UNIL-DESI/cluster-ci/issues/93)
- [x] [Systemic Guardrails: Prevention and eradication of orphaned processes and zombie containers](https://github.com/UNIL-DESI/cluster-ci/issues/94)

**Phase 3 (Stability & Correctness — In Progress)**
- [x] [cluster-run CLI: Ghost merge, inverted DAG display, and Docker stdout leak](https://github.com/UNIL-DESI/cluster-ci/issues/105)
- [x] [Orchestrator: Silent fallback of HEADNODE_URL and missing network feedback](https://github.com/UNIL-DESI/cluster-ci/issues/109)
- [x] [Docker: Container throttled to 2 GB RAM on 128 GB machine (False alarm)](https://github.com/UNIL-DESI/cluster-ci/issues/107)
- [x] [Fix(logs): Resilient streaming, reconnection, and false infrastructure error resolution at job completion](https://github.com/UNIL-DESI/cluster-ci/issues/111)
- [x] [DVC Runner: Double stage execution and misleading commit message](https://github.com/UNIL-DESI/cluster-ci/issues/106)
- [ ] [Web Interface Bugs: Random date and time sorting in DVC History](https://github.com/UNIL-DESI/cluster-ci/issues/101)
- [x] Fix Zombie Jobs: Branch-level guard in scheduler, headnode-aware cancellation in `cluster-run`, auto-cancel in deployed version, and HTTP 500 crash fix on `/api/jobs/{id}/stop`
- [x] Fix Scheduling cluster-run: Cross-repo per-user cancellation for draft branches (one cluster-run per user across all repos), max 1 pending policy per repo+branch for normal branches, and queue display with wait reasons on web dashboard
- [x] Fix Dashboard: Real-time multi-branch artifact scan (intermediate commit watchdog), UTC +2h timezone correction for elapsed times, and launch date added to Active Cluster Runs
- [x] VRAM Tracking & Headnode-as-Worker: Automatic GPU/VRAM detection via `nvidia-smi`, `REQUIRED_VRAM` constraint in `.cluster-ci`, headnode registered as dual-mode worker (scheduler + executor), GPU/VRAM display in dashboard and queue diagnostics
- [x] Fix Runner: Suppressed noisy errors (`Checkout failed`) from `dvc checkout` in Best-Effort mode
- [x] Fix Runner: Infinite runner stall on sync phase following stage failure (added timeouts on watchdog cleanup, git push/pull, and docker exec sync)
- [x] Job Execution Timeout: GitHub Actions timeout increased from 6h to 24h (via `timeout-minutes: 1440` in workflow and `install.sh`)
- [ ] [Multi-GPU Scheduling: Multi-slot support and GPU hardware isolation](https://github.com/UNIL-DESI/cluster-ci/issues/110)

