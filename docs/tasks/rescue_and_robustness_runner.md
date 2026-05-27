# Rescue and Robustness of the Actions Runner

## 1. Context & Motivation
Following the sudden cancellation of research jobs by the GitHub Actions concurrency rule (Issue #65), the Actions Runner daemon crashed on the Headnode and ceased to process incoming jobs. This resulted in the entire research pipeline queue being blocked.

## 2. Investigation Findings
- **Ephemeral Runners (Slots)**: The `runner_manager.py` (c:\Users\hjamet\Documents\code\cluster-ci\src\scheduler\runner_manager.py) spawns ephemeral runners inside slots (slot1, slot2, admin).
- **Zombie Processes**: Upon a violent cancellation, the runner's Listener/Worker processes received termination signals, but their child processes (specifically the bash script executing `run_research_pipeline.sh` and the `Runner.Worker` spawnclient) remained active as orphan/zombie processes, hogging slots and SQLite states.
- **Permanent Admin Runner**: The administrative GHA runner (`UNIL-Henri`) on the headnode was running, but lacking robustness settings, it would fail to recover properly or could crash entirely without automatic systemd restarts.

## 3. Implementation Details & Resolution
We applied a full-stack recovery and resilience hardening protocol:

### Systemd Resilience Hardening
We updated `/etc/systemd/system/actions.runner.UNIL-DESI-cluster-ci.cluster-local-UNIL-Henri.service` on the headnode to ensure it never stays down. We added:
- `Restart=always`
- `RestartSec=5`
- `KillMode=process` (to prevent GHA from bringing down the parent systemd service during violent job abort signals)

We also hardened the project's standard service installer (`src/cluster/setup_runner.sh`) by default for all systemd services (`cluster-runner-manager`, `cluster-scheduler`, `cluster-scheduler-loop`, `cluster-worker`) using identical robust properties:
- `RestartSec=5`
- `KillMode=process`

### Deep Node Cleanup & Active Recovery
To immediately unlock the pipeline, we:
1. Stopped the `cluster-runner-manager.service`.
2. Mass-killed all lingering slot processes on the Headnode (`slot1`, `slot2`, `admin`) to release the filesystems and tokens.
3. Purged `.runner` configurations and old metadata files (`.credentials`, `.credentials_rsaparams`) in the slot directories to force the manager to register clean, brand-new ephemeral runners with GitHub.
4. Restarted the manager, which successfully re-registered all slots and started listening for jobs.

## 4. Verification & Status
- **Actions Runner permanent service**: `Active: active (running)` with automatic recovery policy.
- **Runner Manager service**: `Active: active (running)`, listening on all 3 slots (`admin`, `slot1`, `slot2`).
- **Pipeline Queue**: Fully recovered and ready to accept new jobs.
