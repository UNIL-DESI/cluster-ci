# DVC, Storage, and Data Locality Guide

Data Version Control (DVC) is integrated into Cluster-CI to manage large datasets and models, while standard Git is reserved for code, metrics, and analytical plots. This guide explains how to define your pipelines and how data flows through the cluster.

---

## 1. High-Level Data Flow

Cluster-CI employs a dual-channel architecture to handle experiment files. Depending on how you declare a file in `dvc.yaml`, it will take one of two paths:

```
                  +--------------------------------+
                  |           dvc.yaml             |
                  +--------------------------------+
                     /                          \
       outs: or deps:                            metrics: or plots:
      (Large data, models)                      (JSON, CSV, PNG, SVGs)
           /                                              \
  DVC CAS Cache (Worker)                            cache: false
           |                                              |
    [P2P Transfer]                                  [Git Sync]
  HTTP /fetch_artifact                             Immediate push by
  between workers                                   cluster-ci-bot
           |                                              |
[gc_orchestrator.py GC]                             [Local Pull]
 Pushes to Google Drive                             git pull --rebase
   only if disk < 100GB                               to local repo
```

---

## 2. Defining Pipelines in `dvc.yaml`

To run experiments on the cluster, you must define a pipeline using a `dvc.yaml` file at the root of your repository. 

### Pipeline DAG Example
The following pipeline processes a raw dataset and trains a neural network model:
```yaml
stages:
  process_dataset:
    cmd: python3 src/preprocess.py --input data/raw.csv --output data/clean.csv
    deps:
      - src/preprocess.py
      - data/raw.csv
    outs:
      - data/clean.csv

  train:
    cmd: python3 src/train.py --input data/clean.csv --model models/model.pt --metrics reports/eval.json --plot reports/loss.png
    deps:
      - src/train.py
      - data/clean.csv
    outs:
      - models/model.pt
    metrics:
      - reports/eval.json: {cache: false}
    plots:
      - reports/loss.png: {cache: false}
```

### Sequential Execution (DAG Topological Repro)
Cluster-CI does not run a simple `dvc repro`. To optimize executions and provide real-time updates:
1.  The runner executes `dvc_iterative_repro.py`, which parses your pipeline and builds the execution Directed Acyclic Graph (DAG) using `dvc dag --dot`.
2.  It calculates a topological sort of the stages.
3.  It runs each stage individually using `dvc repro <stage_name> -s` (`--single-item`). The `-s` flag prevents DVC from verifying or rebuilding upstream dependencies, because they have already been processed in the correct topological order.
4.  At the start of each stage, it updates `.dvc/tmp/iterative-status.json` so that the Web Dashboard can display live stage-by-stage status.

---

## 3. Comparison: DVC CAS vs. Git Sync

Understanding the difference between these two tracks is crucial to prevent jobs from failing or bloating your repository.

| Feature | Large Artifacts (`outs` / `deps`) | Small Analytical Data (`metrics` / `plots`) |
| :--- | :--- | :--- |
| **Typical Files** | Datasets, features, weights (`.pt`, `.safetensors`, `.h5`) | Training curves, performance JSONs, PNG graphs |
| **Storage Backend** | Worker Local Disk Cache (Content Addressable Storage) | Remote Git Repository (`origin` branch) |
| **Transport Protocol** | Peer-to-Peer HTTP transfers (`/fetch_artifact` on port 6000) | Git push/pull over HTTPS/SSH |
| **Synchronization** | Pulled on-demand by workers during scheduling | Committed immediately after **each stage** completes |
| **Size Limit** | Up to several gigabytes (subject to worker disk space) | **Strictly < 5 Megabytes** |
| **Auto-Cleanup** | Garbage collected by `gc_orchestrator.py` when disk < 100GB | Retained permanently in Git history |

---

## 4. Large Artifacts (`outs` / `deps`) & P2P Data Plane

When you declare a file in `outs:`, it is stored in the local DVC cache of the worker that executed the job. It is **not** immediately pushed to Google Drive, which saves external network bandwidth.

### Local Cache Checking (Scoring)
When a new job is submitted:
1.  The headnode parses the repository's `dvc.lock` and extracts all expected DVC MD5 hashes.
2.  It contacts all online workers via their `/check_cache` endpoint, checking which worker already has these files in their local directory.
3.  The scheduler awards the job to the worker with the highest number of cache hits (data locality scheduling).

### Peer-to-Peer (P2P) Pulling
If the winning worker lacks some of the dependencies, but another worker possesses them:
1.  The scheduler identifies the best peer worker.
2.  It injects a `DVC_REMOTE_P2P_URL` environment variable containing the peer worker's `/fetch_artifact` address.
3.  The executing worker adds a temporary DVC remote:
    ```bash
    dvc remote add -f peer_remote http://<peer_worker_ip>:6000/fetch_artifact/
    dvc pull --allow-missing -r peer_remote
    ```
4.  This transfers files directly over the internal high-speed network.

### Just-In-Time (JIT) Tiered LRU Garbage Collection
To prevent worker hard drives from filling up, the runner executes a JIT cleanup routine managed by `gc_orchestrator.py` before and after each job run.

*   **Safety Threshold**: The safety threshold is set to **100 GB** (configured via `GC_FREE_SPACE_THRESHOLD_GB` on the worker host).
*   **LRU Registry**: The system maintains a metadata registry (`repositories/registry.json`) tracking project usage (active/idle status, last execution timestamp, and disk size).
*   **Trigger**: If the free space on the partition drops below 100 GB when starting a job, the GC selects the oldest `idle` repositories and applies a **5-level progressive purge** (Least Recently Used first) until the threshold is restored.

#### Progressive Cleanup Levels
The garbage collector frees space in five progressive steps:

| Level | Target | Action & Recovery |
| :--- | :--- | :--- |
| **Level 1** | **DVC History Purge** | Purges historical DVC files in the local cache, preserving only the **last 2 commits** of the repository. |
| **Level 2** | **Large Untracked Files** | Force-deletes any untracked or ignored files in the workspace that are **larger than 500 MB**. |
| **Level 3** | **Docker Dependency Volume** | Deletes the project-specific Docker volume (`cluster-ci-home-<owner-repo>`), clearing cached `uv` and `pip` installations. They will be re-downloaded/reinstalled during the next run. |
| **Level 4** | **Local DVC Cache Wipe** | Wipes the entire local `.dvc/cache` directory. Before wiping, the GC pushes dirty/idle caches to the central backup (Google Drive). The next run will pull them back over the LAN or GDrive. |
| **Level 5** | **Full Repository Wipe** | Deletes the entire repository directory (`repositories/<owner-repo>`) from the worker. |

Active repositories currently running jobs are **never** targeted by the GC.

---

## 5. Metrics & Plots (`metrics` / `plots`) & Git Sync

Metrics and plots are lightweight files used to evaluate models. They must be committed to the Git repository to keep track of performance.

### Automatic Cache Deactivation
The runner automatically runs `dvc_git_helper.py inject` before the pipeline starts. This script parses `dvc.yaml` and forces `'cache': false` on all declared metrics and plots. Even if you forget to write `{cache: false}`, the platform enforces it automatically.

### Post-Stage Commits
Immediately after **each** pipeline stage completes (or if a stage **fails** to preserve logs and crash states):
1.  The runner scans the files listed in `metrics` and `plots`.
2.  **Size Guard**:
    - If a file is **under 5 MB**, it stages it using `git add -f <path>` (which bypasses any `.gitignore` rules).
    - If a file is **5 MB or larger**, the runner ignores it, does not push it, and prints a warning:
      `⚠️ WARNING: File <path> exceeds the 5 MB limit. It will not be synchronized.`
3.  It commits the staged files with the author name `cluster-ci-bot` (`bot@cluster-ci.io`) and the commit message directive `[skip ci]`.
4.  It pushes the commit to your branch. If the push fails because another job pushed changes first, the runner executes `git pull --rebase origin HEAD` to merge the changes automatically and then pushes.

---

## 6. How to Retrieve Results Locally

Because the cluster runner automatically pushes commits to your branch during execution, your local Git history will become outdated.

To retrieve DVC locks, metrics, and plots:
1.  Open your terminal in your local repository.
2.  Run a rebase pull to download the commits generated by `cluster-ci-bot`:
    ```bash
    git pull --rebase origin <your-branch-name>
    ```
3.  If you have local modifications, stash them first:
    ```bash
    git stash
    git pull --rebase origin <your-branch-name>
    git stash pop
    ```
4.  To pull heavy outputs (e.g. models) that were stored in the DVC cache (if you are on the same worker or after GC pushed to Google Drive):
    ```bash
    dvc pull
    ```
