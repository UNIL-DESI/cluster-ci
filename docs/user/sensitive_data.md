# Sensitive Data & Local Execution Guide

Cluster-CI provides a dedicated **Direct Headnode Local Execution** mode (`cluster-run --local`) designed specifically for researchers handling sensitive, confidential, or IP-restricted datasets that cannot be uploaded or pushed to GitHub repositories.

---

## 1. Overview & Confidentiality Guarantees

When running standard `cluster-run`, Cluster-CI uses a "Shadow Push" mechanism to transmit workspace code to a temporary draft branch (`cluster-draft/<username>`) on GitHub.

However, for research projects involving **confidential medical records**, **GDPR-restricted personal data**, **proprietary industry datasets**, or **non-disclosure agreements (NDAs)**, pushing code or data references to external Git remotes may violate governance rules.

### Confidentiality Guarantees of `cluster-run --local`
* **Zero GitHub Upload**: Code, scripts, configuration files, and data never leave the Headnode filesystem. No git pushes or shadow commits are created.
* **No `GITHUB_TOKEN` Required**: Bypasses GitHub authentication and GitHub Actions infrastructure completely.
* **On-Premise Processing**: Code ingestion, scheduling, and container execution take place 100% on the internal cluster infrastructure (`130.223.73.209`).
* **Direct Workspace Sync**: Outputs, metrics, and plots write directly back to your local folder on the Headnode.

---

## 2. Requesting SSH Access Credentials

To use Direct Headnode Local Execution, researchers must have a local user account on the Headnode server.

> [!IMPORTANT]
> **Requesting Access**:
> Contact **Henri Jamet** ([`henri.jamet@unil.ch`](mailto:henri.jamet@unil.ch)) to request SSH access credentials for the Headnode (`130.223.73.209`).

When submitting your request, please include:
1. **Full Name** and **UNIL Email Address**.
2. **Project Name** and brief description of the sensitive data requirements.
3. Your **Public SSH Key** (found in `~/.ssh/id_ed25519.pub` or `~/.ssh/id_rsa.pub`).

---

## 3. Step-by-Step Local Execution Workflow

Once your Headnode SSH account has been created:

### Step 1: Connect to the Headnode via SSH
Open your local terminal and log into the Headnode:

```bash
ssh <username>@130.223.73.209
```
*(Replace `<username>` with your assigned Headnode username).*

### Step 2: Set Up Your Project Repository on the Headnode
Copy or clone your research repository into your user home directory on the Headnode (`/home/<username>/`).

You can copy files from your local workstation using `scp` or `rsync`:
```bash
# Example: Syncing a local project folder to the Headnode
rsync -avz --exclude '.venv' ./my-sensitive-project <username>@130.223.73.209:/home/<username>/
```

Navigate to your project folder on the Headnode:
```bash
cd /home/<username>/my-sensitive-project
```

### Step 3: Ensure Cluster-CI Client is Installed
If `cluster-run` is not yet installed on your Headnode user account, install it with:

```bash
curl -sSL https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh | bash
```

Verify that the CLI is accessible:
```bash
cluster-run --help
```

### Step 4: Execute `cluster-run --local`
Submit your job using the `--local` flag:

```bash
cluster-run --local
```

The CLI will detect that it is operating in Direct Headnode Mode, bypass GitHub Actions, and stream execution logs directly to your SSH terminal session.

---

## 4. Technical Architecture: How `cluster-run --local` Operates

```
┌────────────────────────────────────────────────────────────────────────┐
│                        HEADNODE (130.223.73.209)                       │
│                                                                        │
│  ┌─────────────────────────┐        HTTP POST /api/jobs (is_local=True)│
│  │ Local Project Workspace │ ───────────────────────────────────────┐  │
│  │ /home/<user>/project    │                                        │  │
│  └─────────────────────────┘                                        │  │
│               ▲                                                     ▼  │
│               │ Direct Sync                      ┌─────────────────────┐│
│               │ (Metrics, Plots, dvc.lock)       │ Headnode Scheduler  ││
│               │                                  │ SQLite & API        ││
│               └───────────────────────────────── │ (headnode_service)  ││
│                                                  └─────────────────────┘│
│                                                             │          │
│                                       Local Worker          │ Assign   │
│                                       Assignment            ▼          │
│                                                  ┌─────────────────────┐│
│                                                  │ Isolated Container  ││
│                                                  │ NGC PyTorch Docker  ││
│                                                  │ (uv run dvc repro)  ││
│                                                  └─────────────────────┘│
└────────────────────────────────────────────────────────────────────────┘
```

When `cluster-run --local` is invoked, the platform executes the following stages:

1. **Local Filesystem Ingestion**: The CLI scans your current working directory (`os.path.abspath(os.getcwd())`), creates a local workspace snapshot, and passes the path directly to the Headnode scheduler. No git commits or remote branch pushes take place.
2. **Fail-Fast Validation**: Prior to queue insertion, the Headnode API (`headnode_service.py`) validates your `.cluster-ci` parameters (`REQUIRED_RAM`, `REQUIRED_VRAM`, `MAX_RUNTIME_HOURS`). If validation fails (e.g. invalid syntax or excessive RAM request), the submission returns an immediate `HTTP 400 Bad Request` with exact diagnostic error messages.
3. **Isolated Container Workspace**: The job is assigned to an available worker (or local Headnode executor slot) and launched inside an isolated Docker container running the unified NGC PyTorch image (`nvcr.io/nvidia/pytorch:26.05-py3`). The container is granted access to requested GPU hardware (NVIDIA Blackwell GB10 / RTX 3090) while remaining isolated from host system files.
4. **Local Metrics & Plots Sync**: As stages in `dvc.yaml` complete, metrics, plots, and updated lockfiles (`dvc.lock`) are synchronized directly back to your project directory on the Headnode filesystem in real-time.
5. **Zero GitHub Upload**: No network requests are made to GitHub's servers, guaranteeing complete isolation of sensitive code and data within the local infrastructure.

---

## 5. Controlling Local Jobs

### Monitoring & Streaming Logs
By default, `cluster-run --local` streams logs directly to stdout in your SSH session. If your connection drops, log outputs are preserved in `.cluster-ci-logs/` in your project directory.

### Cancelling a Local Run
To stop an active local job and immediately release allocated GPU hardware:

```bash
cluster-run cancel
```

The CLI contacts the Headnode API, terminates the running Docker container, purges VRAM, and clears local run tracking states.

---

## 6. Summary Comparison: Standard vs. Local Execution

| Feature | Standard `cluster-run` | Sensitive Data `cluster-run --local` |
| :--- | :--- | :--- |
| **Execution Trigger** | GitHub Actions Workflow (`cluster-ci.yml`) | Headnode Local API (`/api/jobs`) |
| **Code Transmission** | Shadow git push to `cluster-draft/<user>` | Local Headnode path ingestion |
| **GitHub Dependency** | Required (`gh` auth & repo secrets) | **None** (100% offline / GitHub-free) |
| **Data Boundary** | Transits via GitHub remote repository | **Stays on Headnode (`130.223.73.209`)** |
| **Log Streaming** | `ppng.io` live stream or `gh run view` | Direct local HTTP socket stream |
| **Result Retrieval** | Git draft branch sync back to workstation | Direct filesystem write on Headnode |
| **Access Requirement** | GitHub Organization membership | Headnode SSH account (`henri.jamet@unil.ch`) |
