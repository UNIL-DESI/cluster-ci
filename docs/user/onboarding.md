# Researcher Onboarding Guide

Welcome to the Cluster-CI platform. This guide walks you through the step-by-step onboarding process to set up your credentials, configure secure communications, and authenticate data storage backends.

---

## 1. Joining the GitHub Organization & Project Cloning

Cluster-CI leverages GitHub for authentication, access control, and GitOps job scheduling.

1.  **Request Organization Membership**: Contact your administrator (hjamet) to receive an invitation to the **UNIL-DESI** GitHub organization.
2.  **Accept the Invitation**: Check your email or visit [github.com/UNIL-DESI](https://github.com/UNIL-DESI) to accept the invitation.
3.  **Clone the Repository**: Once added, clone the main cluster repository to your local machine:
    ```bash
    git clone git@github.com:UNIL-DESI/cluster-ci.git
    cd cluster-ci
    ```

---

## 2. Configuring SSH Access & Keys

Secure Shell (SSH) is used for secure communications between your local client, the GitHub Actions runners, and the physical cluster nodes.

### A. Personal SSH Key Configuration
To push code and interact with the repository, ensure your public SSH key is registered under your personal GitHub settings:
1.  Generate a secure key pair if you do not have one:
    ```bash
    ssh-keygen -t ed25519 -C "your_email@example.com"
    ```
2.  Add your public key (`~/.ssh/id_ed25519.pub`) to your GitHub account: **Settings -> SSH and GPG keys -> New SSH Key**.

### B. CI SSH Configuration
The GitHub Actions orchestrator needs access to the physical cluster headnode to submit jobs on your behalf.
1.  The project administrator configures a dedicated SSH private key as a GitHub Repository Secret named `CLUSTER_SSH_PRIVATE_KEY`.
2.  This key matches the public key authorized on the cluster's headnode, allowing the GHA runner to execute `submit_job.py` and communicate with `/api/jobs` endpoint securely.

### C. Passwordless Inter-Worker SSH (RSA Headless Setup)
To facilitate distributed computing and peer-to-peer (P2P) artifact sharing, the cluster workers require passwordless SSH communication between each other.
*   **Key Generation**: During worker provisioning, a standard RSA key pair without a passphrase is created:
    ```bash
    ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa -N ""
    ```
*   **Authorization Distribution**: The public key (`~/.ssh/id_rsa.pub`) of each worker must be appended to the `~/.ssh/authorized_keys` file of all other workers in the cluster.
*   **Host Key Verification**: To prevent interactive prompts blocking automated jobs, add the hostkeys or configure `StrictHostKeyChecking accept-new` in the worker configuration:
    ```text
    Host 10.0.0.*
        StrictHostKeyChecking accept-new
        IdentityFile ~/.ssh/id_rsa
    ```

---

## 3. Google Drive Authorization for Silent DVC Storage

Cluster-CI uses Google Drive as a centralized remote storage for long-term DVC artifact caching. To allow automated, non-interactive (headless) executions on the cluster to pull/push artifacts, Google Drive API authentication must be authorized.

Since cluster runs execute in headless Docker containers, **interactive OAuth logins (`gdrive` browser prompts) are prohibited at runtime**. The credentials must be pre-authorized.

### Standard Headless Authorization Process:

1.  **Generate Credentials Locally**:
    On your local machine, initialize the Google Drive authentication flow:
    ```bash
    uv run dvc remote modify my_gdrive_remote auth gdrive
    ```
2.  **Perform OAuth Authentication**:
    DVC will display a link. Open this URL in your web browser, log in with your authorized institutional Google account, and grant the required permissions.
3.  **Retrieve Token**:
    Copy the authorization code from the browser and paste it back into your local terminal. This creates a local token cache file containing the refresh token.
4.  **Silent Credentials Injection on Workers**:
    The cluster worker agents automatically mount the pre-authorized organization token or fetch the secret token injected from the repository secrets (`GDRIVE_CREDENTIALS_DATA`) at runtime. This configures the file `~/.config/dvc/providers/gdrive/...` silently, ensuring DVC commands (`dvc pull`, `dvc push`) run with full permissions without requiring user interaction.

Once you have completed this onboarding, you are ready to install the client-side CLI and run your first job. Proceed to the **[Command-Line Client Guide](client.md)**.
