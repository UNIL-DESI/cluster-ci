# Web Dashboard Monitoring Guide

The Cluster-CI Web Dashboard provides real-time monitoring and visualization of your computing jobs, active workers, queue status, and generated research artifacts. This guide describes the interface's features and how to use them to monitor and debug your runs.

---

## 1. Live Infrastructure & Job Monitoring

The dashboard main view is divided into three monitoring panels:

### A. Worker Diagnostics Node View
Shows the current status of all physical workers in the cluster:
*   **Worker State**: Green indicators for online workers; grey for offline nodes.
*   **Hardware Telemetry**: Displays the active RAM and GPU VRAM utilization.
*   **Active Allocation**: Shows which job is currently assigned to the worker.

### B. Active Executions
Tracks jobs currently running on the cluster:
*   **Job Metadata**: Displays the user, repository, active branch, and elapsed time (automatically calibrated to UTC timezone offsets).
*   **Control Actions**:
    - **Stop**: Issues a cancellation request to terminate execution.
    - **Logs**: Opens the live logging window.
    - **DVC-Viewer**: Opens the live web monitor for the running pipeline's metrics.

### C. Queue Panel & Diagnosis
Lists pending jobs in order of submission (FIFO). If a job cannot be scheduled immediately, the panel displays a **wait reason** calculated by the scheduler:
*   `branch_exclusivity`: Blocked because another job is already running on the same repository and branch.
*   `no_free_workers`: All workers satisfying the job's constraints are occupied.
*   `insufficient_ram`: No free worker has enough physical RAM available to satisfy the `REQUIRED_RAM` constraint (accounting for the 8 GB OS reserve).
*   `scheduling`: The scheduler is actively evaluating placement scores.

---

## 2. Segmented Live Logs & Error Highlighting

Clicking the **Logs** button opens an interactive modal:

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
