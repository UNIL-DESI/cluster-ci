# JIT Garbage Collector (LRU)

## 1. Context & Why
To preserve disk space on computing machines while benefiting from local cache speed, we dynamically manage workspaces. The chosen approach is Just-In-Time (JIT) deletion before new jobs are executed.

## 2. Operation
The system relies on a local metadata registry (`repositories/registry.json`) that tracks:
- Project name (owner/repo).
- Last execution date (Timestamp).
- Disk size (bytes).
- Status (`running`, `idle`, or `deleted`).

### Cleanup Policy (Tiered LRU)
Before each job, the orchestrator checks the available disk space on the repository partition.
- **Safety Threshold:** 100 GB (configurable via `GC_FREE_SPACE_THRESHOLD_GB`).
- **Emergency Panic Threshold:** 50 GB (configurable via `GC_PANIC_THRESHOLD_GB`).
- **Protection Window:** Inactive projects are protected for 6 hours (configurable via `GC_PROTECT_HOURS`).
- If free space is below the threshold, the GC executes a **Tiered Cleanup** (Tiers 1 to 5) starting from the oldest (Least Recently Used) idle projects until the threshold is met.

#### Cleanup Tiers:
1. **Tier 1 (Regenerable Caches & Temp Files):** Cleans regenerable caches (`pip`, `uv`, `huggingface`, `torch`, `__pycache__`), temp files (`*.tmp`, `*.log`), and runs `docker system prune` in emergency mode.
2. **Tier 2 (Docker Volumes):** Deletes project-specific home Docker volumes (`cluster-ci-home-<owner-repo>`) for idle projects.
3. **Tier 3 (Local DVC Cache):** Purges `.dvc/cache` for the oldest inactive projects (LRU), ensuring no active executor shares the repository cache.
4. **Tier 4 (Unused Docker Images):** Removes unused Docker images sorted by LRU, never targeting images used by active or protected projects.
5. **Tier 5 (Full Workspace Wipe):** Deletes the entire project repository directory in last resort.

Active projects (`running`) and projects within the protection window (6 hours) are never targeted by the GC.

## 3. Environment Isolation
To prevent dependency collisions between different projects running on the same worker, each project uses a dedicated Docker volume for its home directory. This ensures that `uv` or `pip` installations in one project do not overwrite system packages or caches of another project.

Volume naming convention: `cluster-ci-home-<owner-repo>` (where `/` is replaced by `-`).

## 4. Components
- `src/runner/gc_orchestrator.py`: Python script managing the GC logic and registry.
- `src/runner/run_research_pipeline.sh`: Shell orchestrator integrating GC calls.

## 5. Integration
The orchestrator calls `gc_orchestrator.py` at key moments:
1. **Before the job:** `run-gc` to free space and `update-running` to mark the project as active.
2. **After the job (via EXIT trap):** `update-idle` to update the occupied size and mark the project as inactive.
