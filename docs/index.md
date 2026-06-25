# Welcome to Cluster-CI Documentation

Cluster-CI is an enterprise-grade, GPU-accelerated computing cluster designed specifically for training deep learning models, running large language model (LLM) inference, and executing scientific computing pipelines. Powered by state-of-the-art **NVIDIA Grace Blackwell GB10 GPUs (128GB Unified Memory)** and running on **Ubuntu 24.04**, the cluster delivers high-performance compute resources with a GitOps-driven workflow.

The core philosophy of Cluster-CI is to bridge the gap between local development environments and physical high-performance computing (HPC) nodes. By combining standard Git workflows, **Data Version Control (DVC)**, and an intelligent HTTP-based Peer-to-Peer (P2P) artifact distribution network, researchers can submit jobs directly from their local terminal, monitor them in real-time via a web dashboard, and receive results automatically.

---

## Documentation Structure

The documentation is split into two primary tracks, depending on your role and objectives:

### 🔬 User Guides (For Researchers)
If you are a researcher or data scientist looking to run experiments on the cluster, start here. These guides cover client-side setup, job submission, data tracking, and monitoring:

*   **[Onboarding Guide](user/onboarding.md)**: Learn how to join the GitHub organization, configure SSH keys for secure access, set up inter-worker passwordless SSH, and authenticate Google Drive for DVC storage.
*   **[Command-Line Client (`cluster-run`)](user/client.md)**: Guide on installing the client environment, understanding the "Shadow Commit" mechanism, streaming execution logs in real-time, recovering orphaned jobs, and avoiding line-ending corruption.
*   **[DVC & Storage Management](user/dvc.md)**: Master the crucial distinction between heavy cached outputs (`outs`/`deps` via P2P HTTP CAS) and lightweight tracked parameters (`metrics`/`plots` via Git Sync).
*   **[Docker Containers & Environments](user/containers.md)**: Details about the default PyTorch base image, customizing docker run flags, and configuring fine-tuning environments like Unsloth.
*   **[CI Pipeline & Queue Scheduler](user/ci_queue.md)**: Deep dive into the scheduling loop, branch exclusivity, physical RAM/VRAM constraints, local data caching scores, and JIT worker sanitization (Ollama VRAM unloading).
*   **[Monitoring Dashboard](user/dashboard.md)**: How to navigate the real-time web interface, use the foldable artifact tree with bottom-up search, browse version histories grouped by MD5 hash, and inspect Hydra configurations.
*   **[Support & Contributions](user/support.md)**: Contains the error code lookup table (OOM 137, cancellations, worker crashes), pre-commit scanner guidelines, and GitHub issue template.

### 🛠️ Developer Docs (For Platform Maintainers)
If you are a system administrator, DevOps engineer, or developer maintaining the Cluster-CI platform, these technical specifications detail the cluster internals and microservices:

*   **Architecture & Executions**: Detailed documentation on the [Docker ARM64 Execution Strategy](architecture/docker_arm_strategy.md), [Resilient Logging Systems](architecture/resilient_logging.md), and [Zombie Process Prevention](architecture/zombie_prevention.md).
*   **Scheduling & Resource Management**: In-depth explanations of the [Deployment and Reconciliation Protocol](scheduler/deployment_and_reconciliation_protocol.md), [Physical Resource Reconciliation](scheduler/physical_resource_reconciliation.md), and [Resilience & Chaos Testing](scheduler/resilience_and_chaos_testing.md).
*   **Security & Compliance**: [Security Threat Modeling & Risk Analysis](security/risk_analysis.md) for the multi-tenant cluster environment.
*   **Internals & Tasks**: Step-by-step developer guides on [Client Script internals](tasks/client_script.md), [Concurrency Management](tasks/concurrency_management.md), [Local Cluster Deployment](tasks/deploy_local_cluster.md), [DVC Auth](tasks/dvc_auth.md), and more.

---

## Quick Start for Researchers

To get your environment up and running in less than 5 minutes:

1.  Configure your GitHub SSH credentials by reading the **[Onboarding Guide](user/onboarding.md)**.
2.  Install the unified CLI client in your local repository by following the **[Client Installation Guide](user/client.md)**:
    ```bash
    curl -sSL https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh | bash
    ```
3.  Configure your hardware requirements in `.cluster-ci` and define your pipeline steps in `dvc.yaml`.
4.  Run `cluster-run` to start your first GPU-accelerated job!
