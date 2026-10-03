# Parallel DAG Execution

Cluster-CI v3 enables distributed, parallel stage execution across multiple cluster workers directly from your DVC pipeline DAG. Instead of running an entire pipeline sequentially on a single node, independent branches execute concurrently across available cluster machines.

---

## 1. Enabling Parallel Execution

Parallel stage execution is opt-in and controlled via your `.cluster-ci` configuration file.

To enable parallel DAG scheduling, add the following flag to `.cluster-ci`:

```ini
# Enable parallel DAG scheduling across workers
PARALLEL_STAGES=true
```

When `PARALLEL_STAGES=true` is set:
* The submission engine parses your `dvc.yaml` pipeline and builds an execution graph.
* Multiple branch executors across the cluster can claim independent ready stages concurrently.
* If omitted or set to `false`, Cluster-CI runs in legacy sequential mode (one machine per job, running `dvc repro`).

---

## 2. Pipeline Analysis and Stale Node Detection

Before dispatching tasks, the Cluster-CI v3 planner determines which nodes must be executed:

```
[Repository Push] 
       │
       ▼
[Planner: Git Analysis] 
       │
       ├─► Read dvc.yaml & resolve foreach stages
       ├─► Inspect dvc.lock, code diffs, parameters
       └─► Flag 'stale' nodes (code, param, or upstream changed)
       │
       ▼
[Headnode: DAG State Engine]
       └─► Ready queue populated (nodes whose parents are done/skipped)
```

1. **Pure Git Analysis**: The planner evaluates changed stage code, parameter files, and `dvc.lock` hashes **strictly from Git history**. No heavy datasets or DVC cache downloads are required on the headnode.
2. **Dynamic Pruning**: Up-to-date stages with matching dependency hashes are marked as `skipped` at submission time.
3. **DAG Dependency Tracking**: As soon as all parent stages of a node reach `done` or `skipped`, the child node automatically transitions to `ready`.

---

## 3. Branch Executor Lifecycle

Each participating worker runs a dedicated **Branch Executor** managing the containerized execution environment.

```mermaid
flowchart TD
    A["Boot: POST /api/jobs/{id}/next_node"] --> B{Action ?}
    B -->|"run"| D["Execute Node"]
    B -->|"switch_image"| C["Stop old container<br/>Launch new container with image-specific /home/user volume"]
    C --> D
    B -->|"wait"| W["Sleep 5s -> poll next_node"] --> A
    B -->|"yield / finish"| Z["Stop container & release worker"]
    
    subgraph Node_Execution ["Node Execution Step"]
        D --> E["1. git pull --rebase"]
        E --> F["2. Check & transfer missing dep_paths"]
        F --> G["3. dvc repro -s {node}"]
        G --> H["4. Commit & push results (dvc.lock merge driver)"]
        H --> I["5. POST next_node(status=done)"]
    end
    I --> B
```

### Persistent Containers & Image Switching
* **Long-Lived Container**: On each assigned worker, the container remains active between consecutive stages to avoid container startup and teardown latency.
* **Automatic Docker Image Pulling**: When a stage requires a Docker image (`meta.cluster.image`) not present locally on the worker, the branch executor automatically invokes `docker pull` before container instantiation.
* **Dynamic Image Switching**: When the next assigned node requires a different Docker image, the executor stops the previous container and starts the new one.
* **Isolated Per-Image Home Volumes**: Docker named volumes are isolated per image and repository (`cluster-ci-home-${REPO_SLUG}-${IMAGE_SLUG}`) and mounted onto `/home/user`. This preserves package caches (`uv`, wheels, huggingface caches) while preventing binary ABI incompatibilities between different Linux distributions or Python versions.

---

## 4. Multi-Node Packing & Machine Sharing (A11)

When hardware resources permit (`ALLOW_PACKING = True`), multiple branch executors can execute concurrently on the same physical machine:

* **Independent Workspaces**: Each executor operates inside an isolated workspace directory (`repositories/{repo_slug}__runner_{slot}`) or Git worktree, preventing Git lock collisions and working tree conflicts between parallel stages.
* **Shared DVC Cache**: Executors on the same machine share the local DVC cache (`~/.cache/dvc`), eliminating redundant downloads of model weights and dataset shards across parallel stages.
* **Distinct Container Isolation**: Each executor runs inside a separate Docker container with strictly enforced per-container resource boundaries.

---

## 5. Headnode as Worker of Last Resort (A13/A14)

The Headnode functions primarily as the cluster API, database host, and DAG scheduler. To safeguard core cluster services:

* **Placement Priority**: The scheduler uses priority-based worker ordering. Dedicated workers share standard priority `50` (`DEFAULT_PLACEMENT_PRIORITY`), whereas the Headnode has priority `0` (`HEADNODE_PLACEMENT_PRIORITY`). The Headnode is strictly the worker of **last resort**, selected only when no dedicated worker is eligible or available.
* **Strict Host Resource Reserves**: The Headnode reserves `16.0 GB` RAM, `2` CPU cores, and `20.0 GB` disk space (`DEFAULT_HEADNODE_RAM_RESERVE_GB`, `DEFAULT_HEADNODE_CPU_RESERVE`, `DEFAULT_HEADNODE_DISK_RESERVE_GB`) strictly for system and scheduler operations.
* **Parent Cgroup Ceilings**: All job containers running on the Headnode are placed under `--cgroup-parent=/cluster-jobs`, bounding aggregate memory consumption across all packing containers so they cannot starve the operating system.

---

## 6. Active Memory Ceilings & Isolation Flags (A12)

To prevent unconstrained processes from causing host kernel panics or physical server freezes, strict resource isolation flags are unconditionally applied on all machines:

| Docker Flag | Value / Setting | Purpose |
| :--- | :--- | :--- |
| `--memory` | `ram_gb` (discrete) or `ram_gb + vram_gb` (unified) | Hard physical memory ceiling for the container. |
| `--memory-swap` | Equal to `--memory` value | Disables swap entirely, ensuring deterministic memory limits. |
| `--memory-swappiness` | `0` | Disables kernel page swapping for the container. |
| `--oom-score-adj` | `500` | Ensures the Linux kernel OOM Killer kills the job container before any host daemon or systemd service. |
| `--cpus` | `cpus` (from `meta.cluster.cpus`) | Hard limit on CPU core utilization. |
| `--pids-limit` | `4096` | Anti-fork bomb guard preventing process/thread exhaustion. |

### Diagnosing and Resolving `OOMKilled` (Exit Code 137)

When a stage exceeds its allocated physical memory, the Linux kernel terminates the container immediately. Cluster-CI catches this event and emits the following actionable error message:

```text
OOMKilled: Stage '<stage_name>' exceeded allocated memory and was killed by system OOM Killer (Exit code 137)
```

**Cause**: The execution process in `<stage_name>` allocated more memory than declared in `meta.cluster.ram_gb` (or `vram_gb` on Grace Blackwell GB10 unified memory).

**Remedy**:
1. Increase `ram_gb` (or `vram_gb` on GB10) for the failing stage under `stages.<stage_name>.meta.cluster` in `dvc.yaml`.
2. If `meta.cluster` is not used, increase `REQUIRED_RAM` in `.cluster-ci`.
3. Check your Python script for memory leaks or reduce DataLoader batch sizes.

---

## 7. Git Synchronization and Merge Driver

Because multiple workers execute nodes of the same branch simultaneously, Git synchronization is automated to avoid lock file collisions.

1. **Pre-Execution Pull**: Before running a stage, the executor performs a `git pull --rebase origin {branch}` to pull upstream changes committed by parallel workers.
2. **Single-Stage Repro**: The worker executes only the designated stage:
   ```bash
   dvc repro -s <stage_name>
   ```
3. **Atomic Commit & Resilient Push**: Stage metrics, plots, and updated stage sections in `dvc.lock` are committed with retry mechanisms:
   * If a concurrent worker pushed in the meantime, the executor rebases with exponential backoff.
4. **Automated `dvc.lock` Merge Driver**: Cluster-CI registers a specialized merge driver in `.git/info/attributes` for `dvc.lock`. When two branches or nodes finish simultaneously, stage outputs in `dvc.lock` are unioned cleanly per stage without generating merge conflicts.

---

## 8. Heavy Artifact Handling (No Centralization)

Heavy model weights and datasets tracked by DVC are **never centralized** onto the headnode or pushed to intermediate Git commits.

### Data Locality
* When scheduling a ready node, the scheduler evaluates data locality: it prefers placing the node on a worker that already has the required `dep_paths` locally cached from previous stages or past runs.

### Peer-to-Peer Artifact Fetching
* If a node is scheduled on a worker missing certain dependency files, the worker fetches them directly from the peer worker that produced them (`/fetch_artifact` P2P transfer).

### Resilient Recovery on Purged Artifacts
* If an input artifact was purged by the local garbage collector on all machines, the worker reports:
  ```json
  { "status": "missing_deps", "missing_paths": ["data/features.parquet"] }
  ```
* **Automatic Producer Rerun**: The headnode automatically resets the producer node back to `ready` with reason `outputs_missing`. The downstream node returns to `pending` until the producer re-generates the output.
* **Safety Guard**: Cluster-CI allows at most 1 forced replay per producer node per job to prevent infinite rerun loops.

---

## 9. Aggregated Real-Time Logging

When multiple stages run across distinct machines in parallel, their logs are unified and multiplexed.

Every log line emitted by an executor container is tagged with its stage name and worker hostname:

```text
[preprocess@HEC45801] Processing batch 1/50...
[eval_baseline@HEC45803] Loaded checkpoint models/baseline.pt
[preprocess@HEC45801] Processing batch 2/50...
[eval_baseline@HEC45803] Test accuracy: 0.942
```

Both the local CLI (`cluster-run`) and the [Web Dashboard](dashboard.md) stream these tagged logs in real time, with the ability to filter logs by stage or worker.
