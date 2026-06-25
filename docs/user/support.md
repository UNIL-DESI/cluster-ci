# Support, Error Resolution, and Contributions

This section provides troubleshooting guidelines for runtime errors, explains the Git pre-flight check system, and describes how to request support or contribute new features.

---

## 1. Support & Contribution Rules

The Cluster-CI system is designed, implemented, and maintained by **hjamet**. If you encounter bugs, performance bottlenecks, or want to suggest new features, you are encouraged to open a GitHub Issue.

### The Golden Rule of the Roadmap
To keep the project roadmap structured and aligned with development targets:
> ⚠️ **"No GitHub Issue = No line in the Roadmap"**

Before adding any task to the Roadmap section of the project's README, a corresponding GitHub Issue must be created on the repository.

### Mandatory GitHub Issue Template
Every issue must follow this structure:
```markdown
# [Task Title]

## 1. Contexte & Discussion (Narratif)
- Detailed summary of why we need this feature or how the bug occurs.
- Decision history.

## 2. Fichiers Concernés
- List of files to modify or inspect (e.g. `src/scheduler/worker_agent.py`).

## 3. Objectifs (Definition of Done)
- High-level deliverables.
- Focus on end results, not implementation plans or pseudo-code.
```

---

## 2. Error Resolution Reference Table

When a job fails, the CLI client and Web Dashboard display an exit code. Use the table below to diagnose and resolve common failures:

| Exit Code | Classification | Cause | Resolution |
| :--- | :--- | :--- | :--- |
| **137** | **Out of Memory (OOM)** | The execution process exceeded its RAM allocation. Docker or the kernel terminated the container. The worker agent automatically prints `dmesg` kernel logs on stderr. | 1. Increase `REQUIRED_RAM` in your `.cluster-ci` configuration file (within worker physical limits).<br>2. Check your Python script for memory leaks or excessive batch sizes. |
| **-15 / -1** | **Forced Cancellation** | The job was terminated by a signal. Typically caused by: local Ctrl+C interrupt, clicking "Stop" on the dashboard, or exceeding `MAX_RUNTIME_HOURS`. | 1. If it timed out, increase `MAX_RUNTIME_HOURS` in `.cluster-ci` (maximum 24h).<br>2. If cancelled manually, resubmit a clean execution run. |
| **-99** | **Worker Offline** | The scheduler stopped receiving heartbeat signals from the worker executing your job, indicating the worker crashed or went offline. | 1. The scheduler automatically marks the job as failed.<br>2. Contact the administrator to check the worker's daemon state: `sudo systemctl status cluster-worker`. |
| **-98** | **Worker Startup Crash** | A heartbeat race condition or conflicting agent startup occurred on the worker. | 1. The worker's Single Instance Lock prevents conflicting agents from running concurrently.<br>2. The worker agent is configured to auto-recover and reboot. Re-submit the job if it was not rescheduled. |

---

## 3. Local Git Pre-Commit Scanner (Pre-Flight Checks)

To prevent broken environments or incompatible dependencies from reaching the cluster workers, the client installation injects a pre-commit hook (`.git/hooks/pre-commit`) that runs `.cluster-ci-tools/validate_pyproject.py` locally.

Before Git accepts any new commit, the pre-flight scanner validates the following:

### A. Python Version Compatibility
*   Checks that the `requires-python` specification in `pyproject.toml` accepts and supports **Python 3.12** (which is the target version of the cluster's NVIDIA NGC container).

### B. PyTorch Version Pinning Guard
To utilize the pre-installed, GPU-optimized packages inside the container:
*   The script scans your dependencies for `torch`, `torchvision`, and `torchaudio`.
*   It ensures **no strict pinning (`==`)** is used on these libraries (e.g. `torch==2.1.2` is blocked; use `torch>=2.0` or leave it unpinned).

### C. Cross-Platform Compilation Simulation
*   The pre-commit scanner simulates dependency resolution for the target cluster architecture.
*   It runs a silent compilation check using:
    ```bash
    uv pip compile --os linux --arch aarch64 pyproject.toml
    ```
*   This verifies that all requested third-party packages can be resolved and built successfully for **ARM64 Linux** before the commit is created, preventing remote worker crashes.
