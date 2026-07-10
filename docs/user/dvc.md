# DVC & Storage Guide

## What is DVC?

[DVC (Data Version Control)](https://dvc.org/) is a tool that brings Git-like version control to **data, models, and experiments**. It lets you track large files, define reproducible pipelines, and compare experiment results — all from the command line.

**Cluster-CI requires DVC** to orchestrate your experiments. Instead of running scripts manually, you describe your pipeline steps in a `dvc.yaml` file, and the cluster executes them automatically on GPU workers.

!!! info "Why DVC is mandatory"
    - **Reproducibility**: Every experiment is described as a pipeline with explicit inputs and outputs.
    - **Live monitoring**: The cluster reads your `dvc.yaml` to display real-time progress on the [Web Dashboard](dashboard.md) (stage-by-stage status, metrics, and plots).
    - **Automatic result sync**: Metrics and plots are committed back to your Git repository after each stage, so you can track your progress without manual intervention.

---

## 1. Defining Your Pipeline (`dvc.yaml`)

Create a `dvc.yaml` file at the root of your repository. Each **stage** defines a command to run, its dependencies, and its outputs.

### Minimal Example

```yaml
stages:
  train:
    cmd: python3 src/train.py --epochs 10
    deps:
      - src/train.py
    outs:
      - models/model.pt
    metrics:
      - reports/eval.json: {cache: false}
    plots:
      - reports/loss.png: {cache: false}
```

### Multi-Stage Example

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
    cmd: python3 src/train.py --input data/clean.csv --model models/model.pt
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

The cluster automatically determines the execution order based on stage dependencies.

!!! warning "Declare metrics and plots with `{cache: false}`"
    All files listed under `metrics:` and `plots:` **must** be declared with `{cache: false}`. This tells DVC to track them through Git (not the DVC cache), so the cluster can commit them automatically after each stage. If you forget, the platform enforces it anyway — but it's best practice to declare it explicitly.

---

## 2. What You Must Declare

Here's a quick reference for declaring files in your `dvc.yaml`:

| Declaration | Use for | Synced via | Size limit |
| :--- | :--- | :--- | :--- |
| `deps:` | Source code, input data | — (inputs only) | — |
| `outs:` | Large outputs: models, datasets, features | DVC cache (peer-to-peer between workers) | Several GB |
| `metrics:` | JSON/YAML evaluation files | Git (auto-committed by the cluster) | < 5 MB |
| `plots:` | PNG, CSV, SVG charts and tables | Git (auto-committed by the cluster) | < 5 MB |

!!! tip "Best practice"
    Declare at least **one metric and one plot per stage** so you can monitor progress on the [Dashboard](dashboard.md).

---

## 3. How Results Are Synchronized

After your pipeline runs on the cluster, results flow back to you through two channels:

### Metrics & Plots → Git (automatic)
After **each stage** completes (or fails), the cluster:

1.  Scans files listed under `metrics:` and `plots:` in your `dvc.yaml`.
2.  Commits files under 5 MB to your branch with the author `cluster-ci-bot`.
3.  Pushes the commit automatically.

You can retrieve them locally with:
```bash
git pull --rebase origin <your-branch-name>
```

### Large Outputs → DVC Cache (on-demand)
Files declared as `outs:` (models, datasets) are stored in the DVC cache on the worker that ran your job. They are **not** pushed to Git.

To retrieve them locally (after Google Drive backup or when connected to the cluster network):
```bash
dvc pull
```

---

## 4. How to Retrieve Results Locally

After a cluster run completes:

1.  Pull the Git-tracked results (metrics, plots, DVC locks):
    ```bash
    git pull --rebase origin <your-branch-name>
    ```

2.  If you have local modifications, stash them first:
    ```bash
    git stash
    git pull --rebase origin <your-branch-name>
    git stash pop
    ```

3.  To pull large outputs (models, datasets) from the DVC cache:
    ```bash
    dvc pull
    ```

??? note "How does data move between workers?"
    When a job is scheduled, the cluster checks which worker already has your data cached locally and prioritizes that worker. If the data is on a different worker, it transfers files directly over the internal network (peer-to-peer) instead of downloading from Google Drive. This is automatic and requires no configuration on your part. For technical details, see [Infrastructure Internals](../admin/infrastructure_internals.md).

