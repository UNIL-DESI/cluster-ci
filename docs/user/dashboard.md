# Web Dashboard Monitoring Guide

The Cluster-CI Web Dashboard provides real-time monitoring and visualization of your computing jobs, active workers, queue status, and generated research artifacts. This guide describes the interface's features and how to use them to monitor and debug your runs.

---

## Accessing the Dashboard & Authentication

The dashboard is accessible directly via the cluster's ingress URL or over the internal network:

* **Direct Web Ingress**: Accessible via the cluster's Cloudflare Tunnel endpoint or internal URL (e.g. `http://130.223.73.209:5000/`).
* **Zero-Trust Organization Gate (`UNIL-DESI`)**: To protect research jobs, infrastructure telemetry, and sensitive artifacts, all access requires authentication with an active member account of the **`UNIL-DESI`** GitHub organization.

### Authentication Methods
1. **GitHub OAuth ("Continue with GitHub")**: One-click authentication using your GitHub account. If your account is an active member of `UNIL-DESI`, your dashboard session is authorized immediately.
2. **GitHub Personal Access Token**: If accessing through a dynamic ingress URL where OAuth redirects are not pre-configured, use your GitHub Token (`gh auth token` or personal access token with `read:org` permission) in the sign-in form.
3. **CLI & API Access**: CLI tools like `cluster-run` authenticate via `Authorization: Bearer <token>` using either your GitHub token or the cluster machine token (`CLUSTER_TOKEN`).

Non-members of the `UNIL-DESI` organization are rejected with `403 Forbidden`.

---

## 1. Live Infrastructure & Job Monitoring

The main dashboard view is divided into three monitoring panels:

### A. Worker Diagnostics Node View
Shows the current status of all physical workers in the cluster:
*   **Worker State**: Green indicators for online workers; grey for offline nodes.
*   **Hardware Telemetry**: Displays the active RAM, GPU VRAM, CPU utilization, and free disk space.
*   **Active Allocation**: Shows which job and pipeline node is currently assigned to the worker.

### B. Active Executions
Tracks jobs currently running on the cluster:
*   **Job Metadata**: Displays the researcher `username`, target repository (`repo @branch`), elapsed time, and list of assigned workers (in parallel mode, a job can span multiple workers with a designated **Home Worker**).
*   **Stage DAG Status & UI Badges**: For parallel jobs (`PARALLEL_STAGES=true`), displays the execution graph state per stage:
    - **Status Badges**: `ready` (green badge) vs `pending` (yellow badge), alongside `running`, `done`, `failed`, `blocked`, and `skipped`.
    - **Priority Badges**: Distinct visual badges for job and stage priority:
        * `high`: Red badge with priority arrow icon (`bi-arrow-up-circle-fill`).
        * `normal`: Standard muted grey badge.
        * `low`: Subtle outlined light badge.
    - **Preemption Telemetry**: If a stage was evicted to yield resources to a higher-priority job, a prominent yellow badge `⚡ Preempted: N` appears (`templates/dashboard.html:623`), tracking the exact number of times the stage was gracefully requeued.
*   **Stage Card User Attribution**: Each stage card prominently identifies the owner's `username` alongside `repo @branch` (`dashboard.html:649`), ensuring transparency across multi-tenant cluster activity.
*   **Control Actions**:
    - **Stop**: Issues a cancellation request to terminate execution across all active workers.
    - **Logs**: Opens the live logging window streaming unified tagged logs (`[node@hostname]`).
    - **DVC-Viewer**: Opens the live web monitor for the running pipeline's metrics.

### C. Queue Panel & Diagnosis
Lists queued jobs evaluated under the unified multi-level scheduling order.
*   **Wait Diagnosis**: If a job cannot be scheduled immediately, the panel displays a **wait reason** calculated by the scheduler:
    - `branch_exclusivity`: Blocked because another job is already running on the same repository and branch.
    - `no_free_workers`: All workers satisfying the job's or node's resource constraints are occupied.
    - `insufficient_ram`: No free worker has enough physical RAM available to satisfy the RAM constraint (10 GB default, accounting for the 8 GB OS reserve).
    - `scheduling`: The scheduler is actively evaluating placement scores.
*   **Foldable Per-Machine Waiting Queue**: To keep the dashboard concise when numerous jobs are queued, each worker card displays the **Top 3** pending jobs directly. Any additional queued stages beyond the Top 3 are cleanly folded into an interactive accordion:
    ```html
    <details><summary>Voir les N autres jobs en attente...</summary></details>
    ```
    Expanding this accordion reveals all queued stages with their respective priority and preemption badges (`dashboard.html:678`).

---

## 2. Segmented Live Logs & Error Highlighting

Clicking the **Logs** button opens an interactive modal:

*   **Multi-Node Log Streaming**: In parallel mode, logs from multiple worker nodes are multiplexed in real time with `[stage@worker]` prefixes (e.g. `[train@HEC45801] Epoch 1/10`).
*   **Stage-by-Stage Segmentation**: The dashboard parses the log stream and separates it into distinct tabs based on execution phases (e.g. Setup, DVC stages like `preprocess` or `training`, and Git Synchronization/GC).
*   **Live Status Indicator**: Active tabs display a loading spinner. If the run fails, a skull emoji `☠️` appears along with the shell exit code.
*   **"Last Error" Button**: If a stage fails, this button highlights the last line containing the error trace. Clicking it automatically opens the failing stage's tab and scrolls directly to the error line.

---

## 3. Foldable Artifact Tree & Bottom-Up Search

The **Current Artifacts** tab displays DVC outputs produced by successful runs in the repository.

*   **Tree Reconstruction**: A flat list of DVC-tracked paths is reconstructed client-side into a nested directory tree. You can click on directories to fold/unfold them.
*   **Bottom-Up Search**: Type a filename or path filter in the search bar. The tree will dynamically expand all parent directories leading to matching files, while folding and hiding non-matching directories. This allows you to find deep files instantly.

---

## 4. Unique Version Selector (MD5 Clustering)

Clicking a file in the artifact tree opens the file viewer, which contains a version control interface:

```
[Selected Artifact: reports/loss.png]
      |
      v
[MD5 Clustering Engine] ---> Groups duplicate runs with identical hashes
      |
      v
[Timeline Slider] ---------> Browse physical changes chronologically:
                             Version 1: MD5: a1b2... Size: 15KB (Commit 3b5f)
                             Version 2: MD5: e9f8... Size: 18KB (Commit f9a2)
```

*   **MD5 Clustering**: If you run multiple experiments that produce the exact same file (matching MD5 hash), the selector groups those runs together. This removes duplicates, letting you focus only on runs that physically changed the output.
*   **Chronological Slider**: Drag the slider to browse different physical versions of the file over time. The dashboard displays the creation date, file size, commit SHA, and branch for each version.

---

## 5. File Previews & Bidirectional Navigation

When selecting a file version:

*   **Tabular View**: CSV and TSV files are automatically rendered as interactive HTML tables.
*   **Image Rendering**: PNG, JPG, and SVG plots are rendered in the preview area.
*   **Text Truncation**: Text and raw log files are displayed with syntax highlighting. Large files are truncated to the first **100 lines** to preserve browser performance.
*   **Bidirectional Navigation ("Consulter le Run")**: If you are inspecting an older version of a plot or metric table and want to understand how it was generated, click the **Consulter le Run** button. The dashboard will close the artifact preview and open the log modal of the exact GHA execution run that wrote that physical version of the file.

---

## 6. Hydra Configuration Inspector

If your workspace utilizes the Hydra configuration manager, the dashboard automatically detects YAML parameters files (e.g. `config.yaml`, `params.yaml`).
*   It reads these parameters at the selected commit.
*   It displays them on the right-hand panel of the project view as a collapsible YAML tree.
*   You can inspect hyperparameters (e.g. learning rate, batch size) side-by-side with your plots without opening code files.
