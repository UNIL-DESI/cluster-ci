# Support & Troubleshooting

This section provides troubleshooting guidelines for runtime errors and explains the Git pre-flight check system.

---

## 1. Error Resolution Reference Table

When a job fails, the CLI client and Web Dashboard display an exit code. Use the table below to diagnose and resolve common failures:

| Exit Code | Classification | Cause | Resolution |
| :--- | :--- | :--- | :--- |
| **137** | **Out of Memory (OOM)** | The execution process exceeded its RAM allocation. Docker or the kernel terminated the container. The worker agent automatically prints `dmesg` kernel logs on stderr. | 1. Increase `REQUIRED_RAM` in your `.cluster-ci` configuration file (within worker physical limits).<br>2. Check your Python script for memory leaks or excessive batch sizes. |
| **-15 / -1** | **Forced Cancellation** | The job was terminated by a signal. Typically caused by: local Ctrl+C interrupt, clicking "Stop" on the dashboard, or exceeding `MAX_RUNTIME_HOURS`. | 1. If it timed out, increase `MAX_RUNTIME_HOURS` in `.cluster-ci` (maximum 24h).<br>2. If cancelled manually, resubmit a clean execution run. |
| **-99** | **Worker Offline** | The scheduler stopped receiving heartbeat signals from the worker executing your job, indicating the worker crashed or went offline. | 1. The scheduler automatically marks the job as failed.<br>2. Contact the administrator to check the worker's daemon state: `sudo systemctl status cluster-worker`. |
| **-98** | **Worker Startup Crash** | A heartbeat race condition or conflicting agent startup occurred on the worker. | 1. The worker's Single Instance Lock prevents conflicting agents from running concurrently.<br>2. The worker agent is configured to auto-recover and reboot. Re-submit the job if it was not rescheduled. |

---

## 2. Local Git Pre-Commit Scanner (Pre-Flight Checks)

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
