# Researcher Onboarding Guide

Welcome to the Cluster-CI platform. This guide walks you through the step-by-step onboarding process to set up your credentials, install the CLI client, and configure your repository for running experiments on the cluster.

---

## 1. Joining the GitHub Organization & Creating Your Research Repository

Cluster-CI leverages GitHub for authentication, access control, and GitOps job scheduling.

1.  **Request Organization Membership**: Contact your administrator (hjamet) to receive an invitation to the **UNIL-DESI** GitHub organization.
2.  **Accept the Invitation**: Check your email or visit [github.com/UNIL-DESI](https://github.com/UNIL-DESI) to accept the invitation.
3.  **Create Your Research Repository**: Create a **new repository** on the UNIL-DESI organization for your research project (or clone your own existing research repo locally).
4.  **Install the Cluster-CI Client**: Open your terminal at the root of your local repository and run the install script:

    === "Linux / macOS"
        ```bash
        curl -sSL https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh | bash
        ```

    === "Windows"
        Windows users must run the installation command inside a **Git Bash** terminal:
        ```bash
        curl -sSL https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh | bash
        ```

    This script injects the GitHub Actions workflow (`.github/workflows/cluster-ci.yml`), the `.cluster-ci` configuration file, the pre-commit hook, and the `cluster-run` CLI into your repository.

---

## 2. Configuring Your Personal SSH Key

To push code and interact with your repository on GitHub, ensure your public SSH key is registered under your personal GitHub settings:

1.  Generate a secure key pair if you do not have one:
    ```bash
    ssh-keygen -t ed25519 -C "your_email@example.com"
    ```
2.  Add your public key (`~/.ssh/id_ed25519.pub`) to your GitHub account: **Settings -> SSH and GPG keys -> New SSH Key**.

---

## 3. GitHub Repository Secrets (Environment Variables)

If your pipeline needs external credentials (e.g. HuggingFace tokens, GCP service account keys, API tokens), you can securely inject them as environment variables via **GitHub Repository Secrets**.

### How to Add a Secret

1.  Go to your research repository on GitHub.
2.  Navigate to **Settings → Secrets and variables → Actions**.
3.  Click **New repository secret**.
4.  Enter a name (e.g. `HF_TOKEN`, `GCP_CREDENTIALS`, `WANDB_API_KEY`) and paste the secret value.

### How Secrets Are Injected

**All Repository Secrets you define are automatically injected as environment variables** inside the Docker container when your pipeline runs on the cluster. For example, if you create a secret named `HF_TOKEN`, your Python code can access it via:

```python
import os
hf_token = os.environ["HF_TOKEN"]
```

The injected workflow (`cluster-ci.yml`) passes all secrets to the cluster using `ALL_GITHUB_SECRETS: ${{ toJSON(secrets) }}`, and the orchestrator (`submit_job.py`) parses and injects each secret as an environment variable in the execution container. The only secret excluded from injection is `GITHUB_TOKEN`, which is automatically managed by GitHub.

---

## 4. Google Drive — No Configuration Needed

Google Drive authentication is managed automatically and silently by the cluster workers. No configuration is required on your part — the workers have pre-authorized credentials that handle all DVC push/pull operations to Google Drive transparently.

---

Once you have completed this onboarding, you are ready to install the client-side CLI and run your first job. Proceed to the **[Command-Line Client Guide](client.md)**.
