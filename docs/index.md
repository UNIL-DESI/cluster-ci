# Welcome to Cluster-CI Documentation

Cluster-CI is a GPU-accelerated computing cluster designed for training deep learning models, running LLM inference, and executing scientific computing pipelines. Powered by **NVIDIA Grace Blackwell GB10 GPUs (128 GB Unified Memory)** and running on **Ubuntu 24.04**, the cluster lets you submit jobs directly from your terminal, monitor them via a web dashboard, and receive results automatically.

---

## Documentation Structure

The documentation is organized into three tracks, depending on your role:

### 🔬 User Guides (For Researchers)
If you are a researcher looking to run experiments on the cluster, start here:

*   **[Onboarding Guide](user/onboarding.md)**: Join the GitHub organization, set up your SSH key, install the CLI client, and configure your repository.
*   **[Command-Line Client (`cluster-run`)](user/client.md)**: Install the CLI, submit jobs, stream logs in real-time, and retrieve results.
*   **[Sensitive Data & Local Execution](user/sensitive_data.md)**: Process confidential or IP-restricted datasets directly on the Headnode via `cluster-run --local` with zero GitHub upload.
*   **[DVC & Storage Guide](user/dvc.md)**: Learn how to define your experiment pipeline in `dvc.yaml` and how results are synced back to you.
*   **[Docker Containers & Environments](user/containers.md)**: Understand the default PyTorch environment, customize Docker images, and set up libraries like Unsloth.
*   **[CI Pipeline & Queue Scheduler](user/ci_queue.md)**: How jobs are queued, scheduled across workers, and cancelled automatically.
*   **[Monitoring Dashboard](user/dashboard.md)**: Navigate the real-time web dashboard to monitor jobs, browse artifacts, and inspect experiment results.
*   **[Configuration Reference (`.cluster-ci`)](user/configuration.md)**: Complete reference for all hardware, runtime, and Docker parameters.
*   **[Support & Troubleshooting](user/support.md)**: Error code lookup table and pre-commit scanner guidelines.

### ⚙️ Platform Administration (For System Administrators)
If you are a system administrator responsible for maintaining the cluster infrastructure:

*   **[System Administration](admin/administration.md)**: RunnerManager slots, prerequisites, Maintenance Mode, SSH configuration, and Google Drive authorization.
*   **[Infrastructure Internals](admin/infrastructure_internals.md)**: Garbage collection, container hardening, data locality scoring, P2P transfers, and runtime watchdogs.

### 🛠️ Developer Docs (For Platform Maintainers)
If you are a developer maintaining the Cluster-CI platform, these specifications detail the cluster internals:

*   **Architecture & Executions**: [Docker ARM64 Strategy](architecture/docker_arm_strategy.md), [Resilient Logging](architecture/resilient_logging.md), [Zombie Prevention](architecture/zombie_prevention.md).
*   **Scheduling & Resources**: [Deployment Protocol](scheduler/deployment_and_reconciliation_protocol.md), [Resource Reconciliation](scheduler/physical_resource_reconciliation.md), [Chaos Testing](scheduler/resilience_and_chaos_testing.md).
*   **Security**: [Threat Modeling & Risk Analysis](security/risk_analysis.md).
*   **Internals & Tasks**: [Client Script](tasks/client_script.md), [Concurrency](tasks/concurrency_management.md), [Local Deployment](tasks/deploy_local_cluster.md), [DVC Auth](tasks/dvc_auth.md), and more.

---

## Quick Start for Researchers

Get your environment up and running in less than 5 minutes:

1.  Join the GitHub organization and configure your SSH key → **[Onboarding Guide](user/onboarding.md)**.
2.  Install the CLI client in your local repository:
    ```bash
    curl -sSL https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh | bash
    ```
3.  Configure your hardware requirements in [`.cluster-ci`](user/configuration.md) and define your pipeline in [`dvc.yaml`](user/dvc.md).
4.  Run `cluster-run` to start your first GPU-accelerated job!
5.  Monitor your job on the **[Dashboard](user/dashboard.md)** (requires [VPN UNIL](user/dashboard.md#accessing-the-dashboard)).
