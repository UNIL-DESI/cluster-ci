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

---

## 3. Actionable Error Messages Catalogue (A17)

Cluster-CI v3 enforces explicit, actionable error reporting. Every submission or runtime rejection clearly identifies the file, line, cause, and concrete remedy.

### A. Resource & Schema Errors (`meta.cluster`)

#### 1. Unknown Key under `meta.cluster`
```text
File dvc.yaml, stage '<stage_name>': unknown key(s) under 'meta.cluster': ['<key>'].
Cause: the specified key(s) are not part of the Cluster-CI v3 resource schema.
Remedy: modify or remove this key under meta.cluster in dvc.yaml.
Valid allowed keys: ['cpus', 'gpus', 'image', 'image_amd64', 'image_arm64', 'ram_gb', 'storage_gb', 'vram_gb', 'workers'].
```
* **Cause**: Typo or deprecated field name in `dvc.yaml`.
* **Remedy**: Fix the key to match one of the 9 allowed resource fields.

#### 2. VRAM Requested Without GPU
```text
File dvc.yaml, stage '<stage_name>': resource inconsistency between 'meta.cluster.vram_gb' (X GB) and 'meta.cluster.gpus' (0).
Cause: vram_gb requires gpus >= 1 (video memory cannot be allocated without a GPU).
Remedy: declare 'gpus: 1' (or more) under meta.cluster in dvc.yaml (or REQUIRED_GPUS in .cluster-ci), or set vram_gb to 0.
```
* **Cause**: Stage declares positive `vram_gb` but `gpus` is `0` or omitted.
* **Remedy**: Add `gpus: 1` under `meta.cluster` in `dvc.yaml` (or `REQUIRED_GPUS=1` in `.cluster-ci`), or set `vram_gb: 0`.

#### 3. Invalid Value or Type for Resource Fields
```text
File dvc.yaml, stage '<stage_name>': invalid value for 'meta.cluster.cpus': <value>.
Cause: cpus must be a strictly positive integer (>= 1).
Remedy: specify an integer >= 1 for 'cpus' under meta.cluster in dvc.yaml (default: 2).
```
* **Cause**: Float, boolean, string, or negative number provided for an integer field.
* **Remedy**: Supply an integer `>= 1` for `cpus`, integer `>= 0` for `gpus`, or number `>= 0` for `ram_gb`/`vram_gb`/`storage_gb`.

---

### B. DAG & Submission Validation Errors

#### 4. DAG Cycle Detected
```text
Cycle detected in DAG nodes starting from '<stage_name>'
```
* **Cause**: Circular dependency between stages in `dvc.yaml` (e.g. A depends on B, and B depends on A).
* **Remedy**: Run `dvc dag` locally to inspect the execution graph and remove circular references in `deps` / `outs`.

#### 5. Invalid Stage Dependency
```text
Invalid dependency '<dep_name>' declared by node '<stage_name>'
```
* **Cause**: A stage lists a dependency on a stage that does not exist in `dvc.yaml`.
* **Remedy**: Verify stage names in `dvc.yaml` and fix the dependency list.

#### 6. Duplicate Stage Name
```text
Duplicate node name in plan: '<stage_name>'
```
* **Cause**: Multiple stages share the same name in the resolved pipeline.
* **Remedy**: Ensure each stage has a unique identifier in `dvc.yaml`.

---

### C. Execution & Runtime Errors

#### 7. Out of Memory (`OOMKilled`, Exit Code 137)
```text
OOMKilled: Stage '<stage_name>' exceeded allocated memory and was killed by system OOM Killer (Exit code 137)
```
* **Cause**: The process inside the container exceeded the active memory ceiling (`--memory`) calculated from `meta.cluster.ram_gb` (or `ram_gb + vram_gb` on Grace Blackwell GB10 unified memory).
* **Remedy**: Increase `ram_gb` (or `vram_gb` on GB10) in `dvc.yaml` under `stages.<stage_name>.meta.cluster`, or reduce DataLoader batch sizes in your training code.

#### 8. Headnode Safety Ceiling Violation
```text
Requested memory (X.X GB) exceeds headnode safety ceiling (Y.Y GB = total Z.Z GB - reserve 16.0GB).
```
* **Cause**: A job scheduled on the Headnode requested more RAM than the Headnode's safe capacity after deducting the `16.0 GB` system reserve.
* **Remedy**: Allow the job to execute on dedicated worker nodes by omitting restrictive `ALLOWED_WORKERS` constraints, or lower the stage's `ram_gb` requirement.

#### 9. Missing Dependencies After Retry Limit
```text
Missing deps could not be recovered: ['<path>']
```
* **Cause**: An intermediate artifact required by a downstream stage was purged by garbage collection and the upstream producer stage could not reproduce it after 1 forced replay.
* **Remedy**: Resubmit the full pipeline or ensure the producer stage generates the expected output file.

