# Command-Line Client & Execution Guide

The `cluster-run` CLI is the central tool for submitting and monitoring computing jobs on Cluster-CI. This guide details how to install the client environment and how to utilize the command-line interface.

---

## 1. Client Installation

The installation process configures your local Git repository to communicate with the cluster orchestrator.

### Prerequisites
Before running the installer, ensure you have:
*   A local Git repository hosting your project.
*   **Python 3.10+** installed locally.
*   **GitHub CLI (`gh`)** installed and authenticated (`gh auth login`).
*   **Astral `uv`** package manager installed (the installer will attempt to fetch it automatically if missing).

### Installation Command
Open your terminal at the root of your local repository and run:

=== "Linux / macOS"
    ```bash
    curl -sSL https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh | bash
    ```

=== "Windows"
    Windows users must run the installation command inside a **Git Bash** terminal:
    ```bash
    curl -sSL https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh | bash
    ```

### What the Installation Script Does
1.  **Validation**: Verifies your local Git repository and detects a working Python interpreter.
2.  **Dependencies Setup**: Ensures `gh` and `uv` are available. It installs `tomlkit` locally to support parsing configuration files.
3.  **Workflow Injections**: Injects `.github/workflows/cluster-ci.yml` which handles the cluster's remote execution steps.
4.  **Repository Settings**: Creates the `.cluster-ci` configuration file template if missing, and updates `.gitignore` to ignore internal logs.
5.  **Pre-flight Scanner Hook**: Installs a Git pre-commit hook (`.git/hooks/pre-commit`) running the dependency validation suite (`validate_pyproject.py`) before allowing commits.
6.  **CLI Installation**:
    - Installs `cluster_run.py` to `$HOME/.local/bin/cluster-run`.
    - **Windows Wrapper**: Creates `cluster-run.cmd` inside the target directory so you can invoke `cluster-run` directly from **PowerShell** or **CMD**.
    - **PATH Configuration**: Appends the binary path to your shell configuration (`.bashrc` / `.zshrc`) or Windows User Environment Variables.

*After installing, restart your terminal or run `source ~/.bashrc` (or equivalent) to reload your PATH.*

---

## 2. The "Shadow Commit" Mechanism

To run experiments without polluting your workspace or Git history, `cluster-run` utilizes a **Shadow Commit** architecture:

```
[Local Workspace] ---(cluster-run)---> [Temporary GIT_INDEX_FILE]
                                                      |
                                           Create detached commit
                                           (Modified + Untracked files)
                                                      |
                                           Force-push to branch:
                                     refs/heads/cluster-draft/<username>
                                                      |
                                           Push tag: cluster-run
                                                      |
                                            [Triggers GHA Workflow]
```

*   **Zero Pollution**: It isolates your workspace's Git index using a temporary index file (`GIT_INDEX_FILE`). Your local working tree and branch status remain completely untouched.
*   **File Scope**: The shadow commit packages all modified, deleted, and untracked files in your repository, ensuring the remote worker runs the exact code currently in your editor.
*   **Git Reference**: The commit is pushed to a remote branch named `refs/heads/cluster-draft/<github_username>`.
*   **Trigger**: A git tag named `cluster-run` is updated on the remote repository, triggering the GitHub Actions workflow.

---

## 3. Real-Time Log Streaming

Cluster-CI streams terminal logs from the executing Docker container back to your local client terminal in real-time.

1.  **Primary Channel (`ppng.io`)**: The worker pipes output directly to the pub/sub service `ppng.io/cluster-ci-log-<commit_sha>`. The `cluster-run` client subscribes to this stream for ultra-low latency.
2.  **Fallback Mode (`gh run view`)**: If `ppng.io` is unreachable or blocked by firewalls, the client falls back to polling the GitHub Actions API via `gh run view --log`. This fallback features higher latency but guarantees you receive the logs.
3.  **Noise Filtering**: The client filters out system telemetry, SSH handshake noise, and Tmux status lines.
4.  **Tqdm Handling**: The client processes carriage returns (`\r`) so that progress bars update in-place without generating thousands of lines of log spam. The full logs saved to disk only contain the final 100% completion lines.
5.  **Local Log Directory**: Logs are duplicated to `.cluster-ci-logs/cluster-ci-run-<timestamp>.log`. The CLI automatically keeps only the 5 most recent files to conserve disk space.

---

## 4. Command Reference

### `cluster-run` (Default Execution)
Triggers a shadow run of your workspace.
```bash
cluster-run
```
*   Performs pre-flight checks, verifies `.gitattributes`, packages the shadow commit, and pushes to the cluster.
*   Streams logs in real-time.
*   **Post-Run Sync**: Automatically pulls back the updated DVC locks, metrics, and plots from the remote branch to your local workspace upon successful completion.

### `cluster-run list`
List recent runs.
```bash
cluster-run list
```
*   Queries GitHub Actions for recent "Cluster-CI Execution" runs.
*   Displays the run ID, status (Completed, In-Progress, Queued), and completion time.

### `cluster-run view`
View logs for a run.
```bash
cluster-run view <run_id>
```
*   If `<run_id>` is omitted, it automatically targets your last triggered run.
*   Streams the log output of the target job (either live or historical).

### `cluster-run cancel`
Terminate a running job.
```bash
cluster-run cancel <run_id>
```
*   If `<run_id>` is omitted, it targets your latest active run.
*   Sends an API request (`POST /api/jobs/<job_id>/stop`) to the headnode scheduler to kill the executing container on the worker and release resources.
*   Cancels the associated GitHub Actions run and cleans up the local `.cluster-ci-run.json` tracking file.

### `cluster-run sync`
Manually synchronize remote results.
```bash
cluster-run sync
```
*   Fetches the latest metrics, plots, and `dvc.lock` from the remote `cluster-draft/<username>` branch and applies them to your local directory.
*   Useful if log streaming was interrupted or if you want to pull results from an older run.

---

## 5. Safety Features & Fault Tolerance

### Orphaned Run Recovery
If your local terminal crashes or you force-kill `cluster-run` (e.g. via `kill -9`), the active job continues executing on the cluster, consuming GPU resources.
*   To prevent resource leaks, `cluster-run` writes metadata (PID and Run ID) to a local `.cluster-ci-run.json` file.
*   The next time you invoke **any** `cluster-run` command, the CLI inspects this file.
*   If it detects the local PID is dead but the run is still marked active, it identifies the run as "orphaned".
*   It immediately sends a termination request to the scheduler, reclaiming cluster RAM and VRAM automatically.

### Line-Ending Protection (`.gitattributes`)
When sharing code between Windows client machines and Linux worker machines, line-ending translation (converting LF to CRLF) can corrupt binary files (such as PyTorch models `.pt` or pickle archives `.pkl`) during DVC/Git synchronization.
*   The `cluster-run` client automatically inspects the repository's `.gitattributes` file.
*   If missing, it configures rules to ensure binary extensions are strictly treated as `binary` and raw text files are normalized properly:
    ```text
    *.pt binary
    *.pkl binary
    *.png binary
    *.jpg binary
    *.gif binary
    *.csv text eol=lf
    *.yaml text eol=lf
    *.json text eol=lf
    ```
This prevents Git from modifying the internal bytes of your files, avoiding model loading failures on the cluster workers.
