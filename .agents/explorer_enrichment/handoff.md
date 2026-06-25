# Handoff Report — Explorer Enrichment

Ce document présente l'analyse détaillée des 15 fonctionnalités techniques avancées de `cluster-ci` réparties dans 5 domaines. Il contient les observations concrètes issues du code source, la chaîne logique de fonctionnement, les caveats identifiés, la méthode de vérification, et les brouillons de documentation technique rédigés en anglais prêts à être intégrés.

---

## 1. Observation

L'exploration du codebase a permis de localiser et d'analyser précisément les fichiers suivants :

### Domaine 1 : DVC & Storage
*   **Emergency GC (50 Go)** : Localisé dans `src/runner/gc_orchestrator.py`, constantes `PANIC_THRESHOLD_GB = 50` (ligne 33) et fonction `run_gc()` (lignes 249-291).
*   **Lazy Transfer (`sync_status`)** : Localisé dans `src/runner/gc_orchestrator.py`, fonction `run_transfer_gc()` (lignes 399-478) et `mark_sync_status()` (lignes 156-170).
*   **Visualiseur historique de résultats DVC** :
    *   Gestion du Worktree et cache localisé dans `src/scheduler/worker_agent.py` sous la route `/api/worker/dvc-viewer/start` (lignes 934-1070).
    *   Gestion du cycle de vie et timeouts localisée dans `src/scheduler/headnode_service.py` (lignes 1052-1109, et lignes 1241-1326).
    *   Inactivity Daemon localisé dans `dvc_viewer/server.py` (lignes 1067-1111).

### Domaine 2 : Docker Containers
*   **Calcul du hash composite** : Localisé dans `src/runner/smart_install.sh`, fonction `compute_deps_hash()` (lignes 9-16) et logique de bypass (lignes 21-31).
*   **Protection anti-shadowing de l'image NGC** : Localisé dans `src/runner/smart_install.sh` (lignes 167-190).
*   **Stub NVSHMEM** : Localisé dans `src/runner/smart_install.sh` (lignes 149-160).
*   **Patch de compatibilité CUDA pour bitsandbytes** : Localisé dans `src/runner/smart_install.sh` (lignes 192-203).

### Domaine 3 : CI & Queue Scheduler
*   **Watchdog GPU double seuil** :
    *   Friction de mémoire VRAM au niveau du pilote CUDA injectée par `src/runner/gpu_memory_guard.py` (lignes 1-87).
    *   Script de supervision hôte `src/runner/gpu_watchdog.sh` (lignes 1-132) avec logique Grace Blackwell.
*   **Zombie GC** : Localisé dans `src/runner/gc_orchestrator.py`, fonction `run_zombie_gc()` (lignes 293-398).
*   **DVC Git Watchdog** : Localisé dans `src/runner/dvc_watchdog.sh` (lignes 1-54) et `src/runner/dvc_git_helper.py` (lignes 1-301).

### Domaine 4 : Client (cluster-run)
*   **Redirection intelligente du CWD** : Localisé dans `src/cluster/cluster_run.py`, fonction `check_and_redirect_cwd()` (lignes 483-512).
*   **Validation post-run sync de type base-commune** : Localisé dans `src/cluster/cluster_run.py`, fonction `fetch_cluster_results()` (lignes 926-1017) et plus particulièrement la vérification `git merge-base --is-ancestor` (lignes 958-969).
*   **Auto-correction pre-commit interactive** : Localisé dans `src/runner/validate_pyproject.py` (lignes 1-282).

### Domaine 5 : Administration & Résilience
*   **RunnerManager** : Localisé dans `src/scheduler/runner_manager.py` (lignes 1-209).
*   **Pré-requis système** : Localisé dans `src/cluster/setup_runner.sh`, configuration sudoers (lignes 127-134), watchdog matériel systemd (lignes 136-155).
*   **Mode maintenance** : Localisé dans `src/scheduler/headnode_service.py`, variables globales (ligne 72), routes `/maintenance/on` et `/maintenance/off` (lignes 91-102), et blocage dans `submit_job()` (lignes 233-234).

---

## 2. Logic Chain

La modélisation du comportement système de `cluster-ci` révèle une architecture de type GitOps hautement résiliente conçue pour de l'entraînement distribué et du service de modèles de deep learning (LLMs) sur du matériel de pointe (Grace Blackwell GB10). 

Chaque composant répond à un besoin spécifique d'automatisation et de robustesse :
1.  **DVC & Storage** : Le stockage des workers est maintenu de manière dynamique par une cascade de Garbage Collection (GC) allant d'un transfert paresseux (100 Go restants) vers le Headnode jusqu'à un nettoyage destructif d'urgence (50 Go restants). Les visualisations historiques se font sans polluer l'espace grâce à l'isolation par Git Worktree et à la désactivation intelligente des téléchargements réseaux via le partage par lien symbolique du cache DVC local.
2.  **Docker Containers** : L'utilisation de conteneurs NGC optimisés pour les architectures ARM64 NVIDIA nécessite un processus d'installation sélectif. Le script `smart_install.sh` garantit que les optimisations bas niveau apportées par NVIDIA ne soient jamais écrasées (shadowed) par des paquets génériques issus de PyPI, tout en résolvant à chaud les stubs de communication GPU manquants (`libnvshmem.so`) ou les instabilités de chargement de pilotes tiers (`bitsandbytes` CUDA wrapper).
3.  **CI & Queue Scheduler** : Le système applique une double supervision matérielle. Au niveau de la VRAM, une limite logicielle est d'abord imposée lors de l'initialisation de Python via `PYTHONSTARTUP`, complétée par un watchdog hôte qui tue immédiatement tout conteneur dépassant le seuil de 90% de RAM totale pour éviter les gels d'OS. Le Scheduler identifie quant à lui les jobs orphelins (Zombies) via un audit tridimensionnel d'inactivité (CPU, GPU, logs, réseau).
4.  **Client (cluster-run)** : Le client simplifie l'expérience développeur en détectant le contexte du projet (redirection intelligente via `db.json`) et sécurise les synchronisations bidirectionnelles en vérifiant l'ascendance Git (`merge-base`) avant toute opération destructive de checkout. Le pré-flight scanner interactif (`validate_pyproject.py`) résout les anomalies communes de configuration en local.
5.  **Administration & Résilience** : La supervision du cluster est résiliente face aux blocages. `RunnerManager` gère l'auto-healing des runners GitHub Actions éphémères en détectant à chaud les boucles de résiliation infinies (cancellation spams). Les configurations de bas niveau au niveau de l'OS (`/etc/sudoers.d/cluster-ci` et watchdogs systemd `/etc/systemd/system.conf`) préviennent les blocages physiques liés aux pilotes CUDA.

---

## 3. Caveats

*   **Dépendance POSIX (fcntl)** : Le script `gc_orchestrator.py` et les fichiers de test associés effectuent des imports de la librairie standard UNIX `fcntl`. Par conséquent, l'exécution locale des tests de GC échoue sur une plateforme Windows hôte en raison de `ModuleNotFoundError: No module named 'fcntl'`. Les tests doivent impérativement être exécutés dans un environnement POSIX (Linux/Docker).
*   **Hypothèse d'agencement NGC** : La protection anti-shadowing de `smart_install.sh` assume que le conteneur dispose de paquets systèmes NVIDIA hautement optimisés installés sous `/usr/local/lib/python3.*/dist-packages`. Si l'image de base Docker est modifiée et ne contient plus ces distributions pré-compilées, le nettoyage local peut priver l'environnement de ces paquets sans alternative.

---

## 4. Conclusion : Technical Documentation Drafts (English)

Cette section contient les projets de documentation technique en anglais rédigés en détail pour chacun des cinq domaines.

---

### Section 4.1: DVC & Storage Management

#### 4.1.1 Emergency Garbage Collection (50 GB Threshold)
The worker node implements an automatic, destructive **Emergency Garbage Collection** loop within `src/runner/gc_orchestrator.py` designed to prevent disk space exhaustion.

1.  **Triggering and Panic Threshold**: The emergency GC is launched via `run_gc()`. It checks the free space of the storage partition where repositories reside (derived using `shutil.disk_usage`). The panic threshold is set to a hardcoded limit of **50 GB** (`PANIC_THRESHOLD_GB = 50`).
2.  **Resource Pruning**: Before deleting workspace directories, the script executes a general Docker system cleanup by calling `docker system prune -f` to reclaim space from dangling images, unused networks, and stopped containers.
3.  **Workspace Eviction Strategy (FIFO/LRU)**: If the storage space remains below 50 GB after the Docker prune, the script parses the `registry.json` database. It filters out all projects marked as `status: "idle"`. It then sorts these idle projects chronologically by their `last_execution` timestamp, placing the least recently used (LRU) project at the front.
4.  **Destructive Cleanup**: For each candidate project, the GC invokes `cleanup_level_5(project_path, project_name)`, which executes a recursive directory deletion via `shutil.rmtree(project_path)`. No remote backups or DVC pushes are executed during this emergency routine.
5.  **Termination Condition**: The deletion loop stops immediately once the available disk space climbs back above the 50 GB limit. The database `registry.json` is updated, writing `status: "deleted"` and `size_bytes: 0` for all evicted projects.

#### 4.1.2 Lazy Workspace Transfer (`sync_status`)
For standard maintenance, `src/runner/gc_orchestrator.py` implements a **Lazy Workspace Transfer** routine through `run_transfer_gc()` to offload older workspace folders.

1.  **Threshold and Scan**: Triggered when the worker's free disk space drops below `FREE_SPACE_THRESHOLD_GB` (defaults to 100 GB, configurable via the `GC_FREE_SPACE_THRESHOLD_GB` environment variable). The orchestrator identifies candidate projects marked as `"idle"`.
2.  **DVC Remote Verification**: The worker inspects the workspace configuration at `.dvc/config`. If a remote is configured (determined by searching for the `remote =` string), the project cannot be deleted without pushing its data.
3.  **Headnode Query & Push**: The worker queries the central headnode API endpoint (`HEADNODE_URL/check_space`) to check if the remote repository has sufficient storage capacity. If the headnode is available and has enough space, the worker executes a local `dvc push` inside the project path.
4.  **Eviction & Sync Status Update**:
    *   If the `dvc push` succeeds, the project is evicted from the worker via `cleanup_level_5` (workspace directory deleted). The project status in `registry.json` is set to `"deleted"`, its size is set to `0`, and its `sync_status` is updated to `"done"`.
    *   If the headnode is unreachable, full, or if the `dvc push` fails, the local workspace deletion is deferred. The project's `sync_status` is set to `"pending"` in the registry, ensuring it is kept locally until the next maintenance pass.
    *   If the workspace has no DVC remote configured, it is directly evicted and marked as `status: "deleted"` and `sync_status: "done"`.

#### 4.1.3 Historical DVC Visualizer
The historical visualizer allows researchers to view pipeline DAGs, metrics, and plots for older revisions without interfering with the active workspace. It utilizes isolated Git worktrees and cached symlinks.

1.  **Git Worktree Isolation**: When a historical view request is sent to the headnode for a repository at a specific commit revision, the worker API endpoint `/api/worker/dvc-viewer/start` (in `src/scheduler/worker_agent.py`) is invoked. It creates a deterministic directory in `/tmp/dvc-viewer-<repo>-<rev_short>` and executes:
    `git worktree add --detach <worktree_dir> <target_rev>`
    This checks out the specified commit in an isolated directory without affecting the worker's main branch workspace.
2.  **Local DVC Cache Symlinking**: To avoid time-consuming network pulls, the worker creates a `.dvc` folder inside the newly created worktree, symlinks the main repository's DVC cache (`repo_path/.dvc/cache` -> `worktree_dir/.dvc/cache`), and copies the `config` and `config.local` configuration files. It then runs:
    `dvc checkout`
    This restores all heavy outputs instantly from the shared local DVC cache, without making network requests.
3.  **Inactivity Heartbeats**: The `dvc-viewer` server (launched in `dvc_viewer/server.py`) spawns a background thread named `_inactivity_daemon`. This daemon monitors server activity. If no client requests hit the `/api/heartbeat` endpoint for 15 consecutive seconds, the server self-destructs by calling `os._exit(0)`. While the user's browser is active, it pings the proxy on the headnode every 5 seconds, which forwards the heartbeat to the worker to keep the instance alive.
4.  **Headnode Cleaning Task**: In `src/scheduler/headnode_service.py`, a background thread `cleanup_inactive_viewers()` runs every 30 seconds:
    *   For **local** viewers: if the last access exceeds `DVC_VIEWER_TIMEOUT_MIN` (defaults to 30 minutes), it terminates the process (`proc.terminate()`).
    *   For **remote** worker viewers: if the last access is older than 45 seconds (since the worker itself terminates after 15 seconds of inactivity), the headnode prunes the metadata registration entry.

---

### Section 4.2: Docker Container Dependencies & NGC Optimization

#### 4.2.1 Composite Dependency Hashing
The `src/runner/smart_install.sh` shell script implements a **Composite Dependency Hashing** mechanism to speed up container launch times by skipping redundant dependency installations.

1.  **Hash Calculation**: The function `compute_deps_hash()` aggregates all files defining Python project dependencies: `pyproject.toml`, `uv.lock` (if present), `requirements.txt` (if present), and `setup.py` (if present). It computes a composite MD5 hash:
    `md5sum $files 2>/dev/null | md5sum | cut -d' ' -f1`
2.  **State Verification**: The resulting hash is compared against the value stored in the persistent Docker home volume at `/home/user/.cluster-ci-deps-hash`.
3.  **Sanity Check Bypass**: If the current and cached hashes match, the script performs a quick sanity check to verify that Python packages are actually installed by looking for `.dist-info` directories in `/home/user/.local/lib/python3.*/site-packages` or `dist-packages`. If the sanity check passes, it exits with `0`, bypassing the installation entirely. If packages are missing, it deletes the hash file and triggers a full dependency install.

#### 4.2.2 NGC Library Shadowing Protection
NVIDIA NGC containers contain highly-optimized, hardware-accelerated builds of libraries (e.g. `torch`, `triton`, `vllm`, `nvidia-*`) pre-installed in system directories (such as `/usr/local/lib/python3.12/dist-packages/`).

1.  **The Shadowing Problem**: When running local pip/uv installations (e.g., `pip install -e .` with local prefixes or user directories), transitive dependencies can pull standard public PyPI packages into `/home/user/.local/lib/python3.12/site-packages/`. Since Python prioritizes local paths over system paths, these generic PyPI packages shadow the optimized NGC builds, causing massive performance drops or CUDA crashes.
2.  **Protection Mechanism**: To prevent this library shadowing, `smart_install.sh` executes a post-install hook that scans all Python site-packages and dist-packages paths under the user's home folder (`/home/user/.local/...`, `/workspace/.venv/...`, etc.) and forcibly removes (`rm -rf`) any directory matching:
    `torch`, `torch-*`, `torchvision`, `torchvision-*`, `nvidia*`, `nvshmem*`, `triton*`, `xformers*`, and `vllm*`.
    This ensures the Python runtime always falls back to the vendor-optimized system libraries provided in the NGC container.

#### 4.2.3 vLLM NVSHMEM Stub Symlinking
When compiling or launching `vLLM` in multi-GPU clusters, the build searches for the NVSHMEM communication library (`libnvshmem.so`).

1.  **Single-GPU Worker Absence**: On single-GPU worker nodes, the NVSHMEM runtime is typically absent, which causes vLLM startup to fail with library loading errors.
2.  **Stub Symlink Fix**: During the dependency setup phase, `smart_install.sh` uses a Python script to locate the active PyTorch library directory (`torch/lib`) and automatically symlinks the NVIDIA CUDA stub:
    `ln -sf /usr/local/cuda/lib64/stubs/libnvshmem.so <torch_lib_path>/libnvshmem.so`
    This provides vLLM with the required interface definitions, preventing initialization errors on single-GPU nodes.

#### 4.2.4 bitsandbytes CUDA Compatibility Patch
The `bitsandbytes` quantization package loads pre-compiled CUDA backend libraries (e.g., `libbitsandbytes_cuda120.so`).

1.  **Compatibility Barrier**: When running on cutting-edge platforms like Grace Blackwell (GB10) with newer CUDA drivers (e.g., CUDA 13.2), `bitsandbytes` fails because it does not ship with a matching pre-compiled library for that major/minor version.
2.  **Dynamic Patching**: `smart_install.sh` detects the system CUDA version via `nvcc --version` (e.g. `132` for `13.2`), and scans the `bitsandbytes` site-packages directory to locate the highest pre-compiled `.so` file available (e.g. `libbitsandbytes_cuda126.so` for `12.6`). If the host CUDA version exceeds the highest pre-compiled version and the corresponding `.so` is missing, it dynamically symlinks:
    `ln -s libbitsandbytes_cuda126.so libbitsandbytes_cuda132.so`
    This forces `bitsandbytes` to load and run using the latest compatible CUDA library.

---

### Section 4.3: CI & Queue Scheduler

#### 4.3.1 Double-Threshold GPU Memory Guard & Host Watchdog
To prevent jobs from freezing the operating system or starving other tasks on unified memory platforms, `cluster-ci` implements a double-threshold memory protection system.

1.  **Process-Level CUDA Hard-Cap**: The script `src/runner/gpu_memory_guard.py` is injected into the container's `PYTHONSTARTUP` environment variable. Before user code executes, it checks `CLUSTER_CI_VRAM_LIMIT_GB`. If set, it calculates the VRAM fraction and hard-caps PyTorch's memory allocation via:
    `torch.cuda.set_per_process_memory_fraction(fraction, device=0)`
2.  **Unified Memory (Grace Blackwell) Fallback**: On unified systems (e.g. Grace Blackwell GB10), standard CUDA device queries for total memory return `0` or `[N/A]`. The guard detects this and falls back to reading system RAM from `/proc/meminfo` (`MemTotal:`), which serves as the shared CPU-GPU pool.
3.  **Host-Level Dual Threshold Watchdog**: The host script `src/runner/gpu_watchdog.sh` monitors the running container every 2 seconds:
    *   **Discrete GPU Mode**: Queries `nvidia-smi --query-gpu=memory.used`.
    *   **Unified Memory Mode**: Fallback to `/proc/meminfo` (`MemTotal - MemAvailable`).
    *   **Soft VRAM Threshold (User-declared)**: If memory exceeds this limit, it flags a violation. If the violation persists for `SOFT_THRESHOLD = 2` consecutive checks (4 seconds grace period), the watchdog kills the container (`docker kill`).
    *   **Hard RAM Threshold (90% System RAM)**: If memory usage exceeds 90% of total system RAM, the watchdog immediately kills the container on the very first check to protect the host OS from kernel panic/OOM lockups.

#### 4.3.2 Zombie GC (Multi-dimensional Inactivity)
To prevent orphaned or stuck container processes from indefinitely consuming cluster resources, `src/runner/gc_orchestrator.py` runs a **Zombie Garbage Collection** daemon via `run_zombie_gc()`.

1.  **Container Filtering**: The GC scans for running Docker containers matching the name pattern `cluster-job-*`.
2.  **Multi-Dimensional Inactivity Check**: For each container, it evaluates activity across three dimensions:
    *   **Logs**: Checks the modification timestamp (`st_mtime`) of `job_logs/{job_id}.log`. If the log file has not been modified since the last check, it counts as inactive.
    *   **CPU & Network**: Queries `docker stats` for CPU percentage and network IO bytes. If CPU usage is `<= 0.1%` and network traffic is identical to the previous check, it counts as inactive.
    *   **GPU Utilization**: Runs `nvidia-smi` to verify if the sum of GPU utilization percentages is `0%`.
3.  **10-Minute Timeout Termination**: If inactivity is detected across all three dimensions, a persistent timer is incremented in `zombie_registry.json`. If this inactivity exceeds `ZOMBIE_TIMEOUT_MINUTES = 10` minutes, the container is forcibly removed (`docker rm -f`), and any associated viewer containers or host `dvc-viewer` processes are killed.

#### 4.3.3 DVC Git Watchdog (Incremental Backups)
The cluster implements an asynchronous watchdog (`src/runner/dvc_watchdog.sh`) to perform intermediate Git commits of metrics and plots during long-running training stages.

1.  **Lock File Monitoring**: The watchdog runs as a host-level background daemon, polling the project's `dvc.lock` file every 2 seconds.
2.  **DVC Status Interlocking**: When `dvc.lock` is modified, the script waits 2 seconds for disk writes to settle. It inspects `.dvc/tmp/iterative-status.json`. If the JSON file reports `running: true`, it defers the synchronization to prevent file locking conflicts.
3.  **Intermediate Commits**: If no stage is actively writing, it executes `dvc_git_helper.py sync` inside the container:
    *   **Staging changes**: Stages `dvc.lock` if modified. It reads `dvc.yaml` to identify all metrics/plots files configured with `cache: false`.
    *   **Size Constraint**: If a metric file is `< 5 MiB` and has local modifications, it is staged via `git add -f`. Files `>= 5 MiB` are skipped to protect repository size.
    *   **Push Reconciliation**: Commits the files under `cluster-ci-bot` (`bot@cluster-ci.io`) with `[skip ci]` tags. It then runs `git push origin HEAD`. If the push fails, it executes `git pull --rebase` to resolve remote updates, pushes again, and aborts (`git rebase --abort`) if conflicts cannot be resolved.

---

### Section 4.4: Client (cluster-run)

#### 4.4.1 Smart CWD Redirection
The `cluster-run` client executable (defined in `src/cluster/cluster_run.py`) redirects the current working directory (CWD) to allow execution from outside the target repository.

1.  **Repository Validation**: Upon startup, the client checks if the CWD contains a `.cluster-ci` configuration file.
2.  **Fallback Database Scan**: If the file is missing, the client searches for a local `db.json` database associated with the `antigravity-apk` server (at `C:\Users\hjamet\Documents\code\antigravity-apk\server\db.json` or `~\Documents\code\antigravity-apk\server\db.json`).
3.  **Project Resolution**: It parses the `projects` array in `db.json`. For each project path, it checks for a `.cluster-ci` file.
4.  **Automatic Redirect**: If a matching project is found, it updates the process directory via `os.chdir(proj_path)` and prints:
    `[CWD Redirect] Redirecting cluster-run to Cluster-CI project: <path>`
    If no project matches, it aborts execution with an error.

#### 4.4.2 Common-Base Post-Run Sync Validation
During post-job execution, `cluster-run` fetches metrics, plots, and metadata from the cluster's temporary draft branch (`origin/cluster-draft/<user>`) via `fetch_cluster_results()`.

1.  **The Overwrite Risk**: If a user cancels a run before the shadow push completes, the draft branch HEAD will point to a previous run. Performing a simple diff and checkout against this stale ref would treat local uncommitted code changes as modifications to revert, wiping out the developer's work.
2.  **Common-Base Ancestorship Check**: To prevent this, the client checks out files only after validating that the local shadow commit (`base_ref`) is an ancestor of the remote branch HEAD (`remote_sha`):
    `git merge-base --is-ancestor <local_shadow_sha> origin/<draft_branch>`
3.  **Checkout Filter**: If the check returns non-zero (meaning they do not share our local commit as an ancestor), the sync is aborted with the message `"Run was cancelled before push completed. No new results to sync."`. If it succeeds, the client diffs the commits (`git diff --diff-filter=AM`) and checks out the updated metrics, plots, and `dvc.lock` files.

#### 4.4.3 Interactive Pre-commit Validation
The script `src/runner/validate_pyproject.py` acts as a pre-commit check to validate package requirements.

1.  **Python Version Compatibility**: It uses `tomlkit` to parse `pyproject.toml`. It verifies that `project.requires-python` does not exclude Python 3.12 (e.g. `<3.11`). In interactive mode, it prompts the user to auto-correct it to `">=3.12"`.
2.  **Torch Pinning Relaxation**: Strict dependency pinning (e.g., `torch==2.1.2`) conflicts with custom optimized CUDA builds pre-installed in the cluster's NGC container. The script identifies these pins and, in interactive mode, prompts the user to relax them (e.g. `torch>=2.0`, `torchvision>=0.15`).
3.  **ARM64 Resolution Simulation**: It fetches the central constraints file `cluster_constraints.txt` from GitHub. To prevent false positives, it excludes packages declared as direct project dependencies. It then runs:
    `uv pip compile --python-platform aarch64-unknown-linux-gnu --python 3.12 -c <filtered_constraints> pyproject.toml`
    This simulates dependency resolution for the ARM64 platform before code is pushed to the cluster.

---

### Section 4.5: Administration & Resilience

#### 4.5.1 RunnerManager
The self-hosted runners are managed by `src/scheduler/runner_manager.py`, which coordinates standard execution slots and an exclusive admin slot.

1.  **Threaded Slots & Staggered Startup**: The manager launches `num_slots` runner threads (each handling one standard slot) plus an admin thread (`admin` slot). To prevent API rate-limiting or local file locks during concurrent `./config.sh` calls, it implements a staggered delay (`slot_id * 2` seconds) before spawning standard slots.
2.  **Atomicity and Stale File Cleanup**: When provisioning a slot, it copies the GitHub Actions runner binaries from `runners/template` atomically (coping to a temporary folder before renaming). Before launching, it deletes stale configurations (`.runner`, `.credentials`, etc.) to prevent "already configured" errors.
3.  **Cancellation Loop Active Watchdog**: Standard runners are configured with `--ephemeral` to handle exactly one job before terminating. To prevent runners from hanging during cancellation requests, the manager scans the runner log `_diag/Runner_*.log` every 10 seconds. If it detects multiple `"Job cancellation request" received` (8 or more lines in the last 15 lines) OR if it finds that registration is missing (unauthorized runner), it increments a `cancellation_spam_count`. If this count reaches 3, the manager kills the process (`process.kill()`) and restarts a fresh runner.

#### 4.5.2 System Prerequisites
The cluster worker nodes require specific operating system privileges and hardware-level recovery watchdogs, which are provisioned by `src/cluster/setup_runner.sh`.

1.  **Passwordless Sudoers Policy**: The system configures a sudoers drop-in file at `/etc/sudoers.d/cluster-ci`:
    `$USER ALL=(ALL) NOPASSWD: /bin/systemctl restart cluster-runner-manager, /bin/systemctl restart cluster-scheduler, ...`
    This allows the unprivileged user running the runner manager or scheduler agents to restart systemd services, inspect `dmesg`, or read system logs without manual password prompts.
2.  **Systemd Hardware Watchdog**: To recover from total system lockups (which can occur during driver crashes on unified memory GPU nodes), the installer configures systemd's built-in watchdog in `/etc/systemd/system.conf`:
    *   `RuntimeWatchdogSec=30`: Instructs systemd to check in with the hardware watchdog device (e.g. `/dev/watchdog`) every 30 seconds. If the kernel or driver freezes and systemd fails to pet the watchdog, the hardware motherboard controller triggers an immediate hard reboot.
    *   `RebootWatchdogSec=60`: Sets a 60-second watchdog timer during reboot sequences to recover from hangs at shutdown.

#### 4.5.3 Maintenance Mode
The headnode scheduler API in `src/scheduler/headnode_service.py` provides a **Maintenance Mode** toggle to suspend job submissions.

1.  **State Management**: The API stores the state in an in-memory global variable `MAINTENANCE_MODE = False`.
2.  **API Controls**: The mode is controlled via POST routes `/maintenance/on` and `/maintenance/off`, which require Bearer token authorization using the `CLUSTER_TOKEN` environment variable.
3.  **Job Suspension**: When maintenance mode is active, any call to the `/submit_job` endpoint is blocked. The API immediately returns:
    `503 Service Unavailable: {"error": "Service Unavailable: Maintenance Mode Active"}`
    This prevents new tasks from entering the scheduling queue while workers undergo maintenance or upgrades.

---

## 5. Verification Method

Pour vérifier de manière indépendante le fonctionnement des différents modules :

1.  **Exécution des tests unitaires** :
    *   **Contrainte** : Les scripts de GC utilisent le package POSIX `fcntl`. Les tests doivent donc être exécutés sous Linux (WSL, Docker ou Worker du Cluster).
    *   **Commande** :
        ```bash
        # Déclarer une URL de headnode fictive pour l'import de worker_agent
        export HEADNODE_URL="http://localhost:5000"
        
        # Exécuter les tests avec pytest
        pytest
        ```
    *   **Fichiers de tests à inspecter** :
        *   `src/runner/test_gc.py` : Valide les différents niveaux de nettoyage et le comportement d'urgence.
        *   `src/runner/test_tiered_gc.py` : Valide la Garbage Collection étagée et le lazy transfer.
        *   `src/runner/test_dvc_git_helper.py` : Valide la détection de dépendances et le marquage `cache: false`.
        *   `src/scheduler/tests/test_watchdog_logic.py` : Valide l'analyse multi-dimensionnelle d'inactivité (zombies).

2.  **Vérification de la simulation de dépendances** :
    *   Vérifier le fonctionnement du pre-commit hook de `validate_pyproject.py` en exécutant :
        ```bash
        python3 src/runner/validate_pyproject.py --pyproject pyproject.toml --interactive
        ```
