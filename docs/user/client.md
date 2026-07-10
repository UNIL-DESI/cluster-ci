# Command-Line Client & Execution Guide

The `cluster-run` CLI is the central tool for submitting and monitoring computing jobs on Cluster-CI. This guide details how to install the client and how to use it.

---

## 1. Client Installation

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
2.  **Dependencies Setup**: Ensures `gh` and `uv` are available.
3.  **Workflow Injection**: Injects `.github/workflows/cluster-ci.yml` for remote execution.
4.  **Repository Settings**: Creates the [`.cluster-ci` configuration file](configuration.md) and updates `.gitignore`.
5.  **Pre-flight Scanner Hook**: Installs a Git pre-commit hook that validates your `pyproject.toml` before each commit.
6.  **CLI Installation**:
    - Installs `cluster-run` to `$HOME/.local/bin/`.
    - **Windows**: Creates `cluster-run.cmd` so you can invoke `cluster-run` from PowerShell or CMD.
    - Appends the binary path to your shell configuration (`.bashrc` / `.zshrc`) or Windows environment variables.

*After installing, restart your terminal or run `source ~/.bashrc` (or equivalent) to reload your PATH.*

---

## 2. How `cluster-run` Works

When you run `cluster-run`, the CLI:

1.  **Packages your code** — Creates a temporary "shadow commit" containing all your modified and untracked files, without touching your local branch or Git history.
2.  **Pushes to GitHub** — The shadow commit is pushed to a remote branch (`cluster-draft/<username>`), triggering the CI pipeline.
3.  **Streams logs** — You see real-time output from the cluster worker directly in your terminal.
4.  **Pulls results** — When the job completes, metrics and plots are automatically synced back to your local workspace.

Your local working tree and branch status remain **completely untouched** throughout this process.

---

## 3. Real-Time Log Streaming

Cluster-CI streams logs from the executing container back to your terminal in real-time.

*   **Primary channel**: Low-latency streaming via `ppng.io`.
*   **Fallback**: If `ppng.io` is unreachable, the client polls the GitHub Actions API (`gh run view --log`).
*   **Progress bars**: `tqdm` progress bars update in-place without generating log spam.
*   **Local copies**: Logs are saved to `.cluster-ci-logs/` (last 5 runs kept automatically).

---

## 4. Command Reference

### `cluster-run` (Default Execution)
Triggers a shadow run of your workspace.
```bash
cluster-run
```
*   Performs pre-flight checks, packages the shadow commit, and pushes to the cluster.
*   Streams logs in real-time.
*   Automatically pulls back updated metrics and plots on completion.

### `cluster-run list`
List recent runs.
```bash
cluster-run list
```
*   Displays the run ID, status (Completed, In-Progress, Queued), and completion time.

### `cluster-run view`
View logs for a run.
```bash
cluster-run view <run_id>
```
*   If `<run_id>` is omitted, it targets your last triggered run.

### `cluster-run cancel`
Terminate a running job.
```bash
cluster-run cancel <run_id>
```
*   If `<run_id>` is omitted, it targets your latest active run.
*   Sends a cancellation request to the cluster and cleans up local tracking files.

### `cluster-run sync`
Manually synchronize remote results.
```bash
cluster-run sync
```
*   Fetches the latest metrics, plots, and `dvc.lock` from the remote branch and applies them to your local directory.
*   Useful if log streaming was interrupted or if you want to pull results from an older run.

---

## 5. Safety Features

### Orphaned Run Recovery
If your terminal crashes or you force-kill `cluster-run`, the active job continues on the cluster. The next time you run any `cluster-run` command, the CLI detects the orphaned run and automatically sends a termination request, freeing cluster resources.

### Line-Ending Protection
When sharing code between Windows and Linux, line-ending differences can corrupt binary files (models, images). The `cluster-run` CLI automatically configures `.gitattributes` to prevent this:
```text
*.pt binary
*.pkl binary
*.png binary
*.csv text eol=lf
*.yaml text eol=lf
```

### Pre-commit Validation
The installation injects a Git pre-commit hook that validates your `pyproject.toml` before each commit:

*   **Python version**: Ensures `requires-python` accepts Python 3.12 (the cluster's runtime version).
*   **PyTorch pinning**: Blocks strict version pins (`torch==2.1.2`) that conflict with the cluster's pre-installed CUDA-optimized PyTorch. Use `torch>=2.0` or leave unpinned.
*   **ARM64 compatibility**: Simulates dependency resolution for the cluster's ARM64 architecture to catch build failures early.
