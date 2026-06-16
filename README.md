# Cluster CI

L'orchestrateur GitOps minimaliste et décentralisé pour le traitement de données et l'entraînement de modèles sur un **cluster hétérogène multi-architecture** (workers ARM64 Blackwell + headnode AMD x86_64 dual-mode scheduler/executor).
**État actuel** : Système opérationnel. Le cluster hétérogène supporte nativement les architectures ARM64 et AMD64 grâce à la détection automatique de l'architecture hôte (`uname -m`) et la sélection dynamique de l'image Docker appropriée (`DOCKER_IMAGE_AMD64` / `DOCKER_IMAGE_ARM64`). Le conteneur NGC unifié `nvcr.io/nvidia/pytorch:26.05-py3` (Python 3.12, PyTorch 2.12, CUDA 13.2) est utilisé sur les deux architectures avec injection automatique du flag `--platform`. Intégration du système d'init Docker natif (`--init`) pour l'éradication complète et structurelle des processus zombies DVC `[dvc] <defunct>` dans le conteneur principal, support natif du streaming de logs en direct en temps réel ligne par ligne sans perte (via une attente active et un streaming linéaire direct pour éliminer tout bruit ANSI/tmux et prévenir les troncatures et coupures de mots parasites), drainage robuste du buffer de logs distants (ppng.io) de 5 secondes à la complétion du run pour éviter toute perte de trace finale, support d'un tableau de bord interactif et transparent de la file d'attente pour les chercheurs (position de file d'attente, statut détaillé des tâches occupantes par chercheur avec RAM/durée, et diagnostics automatisés de RAM physique insuffisante), synchronisation intermédiaire en temps réel et automatique des métriques, plots et `dvc.lock` localement après chaque étape DVC réussie (évitant toute perte de progression en cours d'exécution), homogénéisation complète inter-workers (liaison SSH RSA et synchronisation automatique du cache de modèles), sauvegarde asynchrone optimisée des gros fichiers de données DVC via le Garbage Collector asynchrone (Lazy GC) pour préserver la bande passante réseau à chaque fin de job CI, système d'auto-annulation avancée par catégorie de branche (cluster-draft vs normales) avec annulation cross-repo par utilisateur pour les cluster-run (un seul cluster-run actif par chercheur) et file d'attente intelligente avec raisons d'attente, streaming de logs en direct hautement résilient aux fluctuations et coupures réseau (avec reconconnexion automatique via exponential backoff, déduplication intelligente à base de lignes traitées, et élimination des blocages d'appels CLI synchrones grâce aux timeouts stricts) éliminant tout blocage des workflows GitHub Actions à la fin des exécutions, Watchdog souverain centralisé garantissant l'application stricte des limites de temps d'exécution (timeouts) avec grâce de 5 minutes, mécanisme de retry robuste avec exponential backoff pour l'agent worker résistant aux micro-coupures de l'API Headnode pendant les phases de redémarrage/GitOps, auto-guérison complète de l'hôte via un mécanisme d'instance unique stricte (Single Instance Enforcement) couplé à une purge JIT (Just-In-Time) ultra-déterministe des conteneurs zombies et processus runners orphelins (protégeant les runners GHA actifs en mode delegation lors du dual-mode headnode-as-worker), et **système de réconciliation ultra-rapide des ressources physiques** (libération de la RAM physique et de la VRAM d'Ollama sur Blackwell en moins de 5 secondes) propageant les signaux d'annulation externes de bout en bout pour une éfficacité et réactivité de file d'attente optimales.

Asynchronous continuous integration system for research pipelines, designed as a pull-based replacement for the legacy SlurmRay push-based architecture. This repository hosts the scripts necessary to configure a GitHub Actions Self-Hosted Runner on the target Ubuntu machine, orchestrating `uv run dvc repro` executions in local environments and managing silent authentication with Google Drive. It also provides the client script allowing any research repository to interface with this cluster.

## Cluster Hardware Specifications

| Property | Workers (ARM64) | Headnode (AMD x86_64, dual-mode) |
|----------|----------|-------|
| **Role** | Executor | Scheduler + Executor (dual-mode) |
| **GPU** | NVIDIA GB10 (Blackwell) | 2× NVIDIA RTX 3090 (48 GB VRAM) |
| **CPU** | ARM64 — Cortex-X925 + Cortex-A725 | AMD Ryzen 9 3900X (24 threads) |
| **RAM** | 128 GB unified memory | 125 GB (séparée) |
| **OS** | Ubuntu 24.04.4 LTS | Ubuntu 20.04 |
| **Docker Image** | `nvcr.io/nvidia/pytorch:26.05-py3` (`DOCKER_IMAGE_ARM64`) | `nvcr.io/nvidia/pytorch:26.05-py3` (`DOCKER_IMAGE_AMD64`) |
| **Python** | 3.12 | 3.12 |
| **PyTorch** | 2.12 (CUDA 13.2) | 2.12 (CUDA 13.2) |
| **Storage** | ~3.2 TB | ~938 GB |

# Installation

### 1. Client Installation (Projet de recherche)

Exécutez cette commande à la racine de votre dépôt Git pour l'intégration automatique :

```bash
curl -H 'Cache-Control: no-cache, no-store' -sSL "https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh?v=$(date +%s)" | bash
```

> [!IMPORTANT]
> **Windows User Note**: Execute this command using a **Git Bash** terminal. Executing it directly in PowerShell will fail because `curl` is aliased to `Invoke-WebRequest`, which handles headers differently. Alternatively, run: `bash -c "curl -H 'Cache-Control: no-cache, no-store' -sSL \"https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh?v=\$(date +%s)\" | bash"`.

Ce script injecte :
1. Le workflow Github Actions (`.github/workflows/cluster-ci.yml`)
2. Le fichier de contrôle DVC (`.cluster-ci`)
3. **Le fichier de directives pour agents (`AGENTS.md`)** contenant les contraintes d'architecture du cluster (Python 3.12, PyTorch 2.12, CUDA 13.2) afin d'éviter les erreurs de dépendances de l'IA sur ce dépôt.
4. **Le Scanner Pre-flight (Git Hook)** : Un hook de pre-commit interactif qui valide la compatibilité locale avec le cluster ARM64 et propose des corrections automatiques (avec détection robuste du binaire Python évitant le stub factice du Windows Store sur Windows).
5. **Le CLI `cluster-run`** : Commande locale pour soumettre et suivre des jobs directement depuis votre terminal (voir ci-dessous).

#### Commande `cluster-run`

La commande `cluster-run` est **100% compatible avec Windows (PowerShell/CMD), Linux et macOS**. Elle utilise le mécanisme de "Shadow Push" pour soumettre vos modifications locales (y compris les fichiers non commités et fichiers untracked) au cluster distant sans polluer votre historique git.

- **Sur Linux / macOS** : Après exécution du script `install.sh` ci-dessus, le binaire est disponible dans `~/.local/bin/cluster-run`.
- **Sur Windows (Natif)** : Les scripts wrappers `cluster-run.bat` et `cluster-run.ps1` sont directement disponibles à la racine de votre dépôt de recherche. Vous pouvez exécuter `.\cluster-run` sous PowerShell ou `cluster-run` sous CMD en toute transparence. Pour y accéder globalement, ajoutez simplement le dossier de votre dépôt à votre `PATH` Windows.

| Commande | Description |
|---|---|
| `cluster-run` | Soumet un job et streame en temps réel et en direct les logs d'exécution ligne par ligne dans votre terminal d'origine sans perte |
| `cluster-run --background` | Soumet un job sans bloquer le terminal |
| `cluster-run list` | Liste les runs récents |
| `cluster-run view [run_id]` | Affiche les logs d'un run (dernier par défaut) |
| `cluster-run cancel [run_id]` | Annule un run et nettoie la branche |
| `cluster-run sync` | Rapatrie manuellement les résultats (métriques, plots, dvc.lock) depuis le cluster |

**Robustesse** : Les résultats partiels sont automatiquement synchronisés localement quelle que soit l'issue du run (succès, échec, Ctrl+C). En cas de force-kill du processus local, le prochain appel à `cluster-run` détecte et nettoie automatiquement le run orphelin sur GitHub Actions. Les logs complets sont redirigés dans un dossier local `.cluster-ci-logs/` (automatiquement exclu via `.gitignore`), avec affichage intégral en console et duplication complète dans un fichier de logs local (avec rotation automatique conservant uniquement les 5 fichiers les plus récents). Pour éviter tout bruit visuel inutile dans la console, les fichiers d'infrastructure internes (les fichiers sous `.dvc-viewer/hashes/` et `dvc.lock`) sont rapatriés de manière totalement silencieuse, tandis que seuls les métriques et plots utilisateur sont listés explicitement à la complétion.

### Cluster Deployment (Headnode & Workers)

Installation is done via a "One-Liner" curl command that automatically configures the environment and systemd services.

#### 1. Install the Headnode (Scheduler)
The Headnode manages the job queue and ephemeral runners. The script will ask for your **GitHub PAT** and the target to monitor.
```bash
curl -H 'Cache-Control: no-cache, no-store' -sSL "https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh?v=$(date +%s)" | bash -s -- headnode
```

#### 2. Install a Worker (Executor)
Once the Headnode is installed, it will provide a ready-to-use command to run on your Workers. Alternatively, you can start the installation manually:
```bash
curl -H 'Cache-Control: no-cache, no-store' -sSL "https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh?v=$(date +%s)" | bash -s -- worker
```
The script will ask for the **Headnode URL** and the **Cluster Token** generated during Headnode installation.

#### Post-Installation Configuration
Once installed, you can add secrets (GCP, HuggingFace) to the `.env.secrets` file located in the installation folder (default `~/cluster-ci`).

To cleanly uninstall everything (systemd services, local cleanup):
```bash
cd ~/cluster-ci
./src/cluster/uninstall_runner.sh owner/repo
```

# Description détaillée

Cluster CI is based on GitOps principles. Instead of the agent trying to maintain a continuous interactive session on the remote machine (a structural issue with the Joules Agent on long research jobs), execution is delegated to a self-hosted GitHub Actions runner installed as a `systemd` service on the machine.

**Execution Flow**:
1. **Pull Request**: Joules (the coding agent) pushes changes to a GitHub PR.
2. **CI Trigger**: GitHub Actions hooks into the self-hosted runner.
3. **Orchestration**: The setup script switches to an untracked local cache directory (`repositories/$ORG/$REPO_NAME`), performs a `git fetch` and a forced `git checkout` of the branch (to keep DVC state intact across branches).
4. **Execution**: The orchestrator detects the `.cluster-ci` file, prepares the environment via `uv sync`, and runs `uv run dvc repro` with the provided arguments.
5. **Authentication**: The runner silently injects credentials (Google Drive) by sourcing the global cluster `.env` and `.env.secrets` files.
6. **CI Feedback**: Joules receives native failure and success notifications via GitHub PR integration.
7. **Configuration `.cluster-ci`**: Les jobs nécessitant d'être schedulés peuvent déclarer les paramètres suivants à la racine :
    - `REQUIRED_RAM=16GB` : Contrainte de placement RAM (défaut : 2GB).
    - `REQUIRED_VRAM=24GB` : Contrainte de placement VRAM GPU (défaut : 0, pas de contrainte). Le scheduler n'assignera le job qu'à des workers disposant d'au moins cette quantité de VRAM.
    - `MAX_RUNTIME_HOURS=24` : Durée maximale d'exécution (**OBLIGATOIRE**, max 24h) pour éviter les processus zombies.
    - `EXPOSED_PORT=8501` : Active le routage vers une interface graphique (ex: Streamlit, Gradio) sur le port spécifié.
   Une fois alloué, le conteneur a accès à 100% de la RAM hôte pour éviter les limites artificielles.
8. **Résilience et Robustesse de l'Agent** : Pour éviter qu'un crash de thread n'isole un worker (problématique historique lors des micro-coupures réseau avec le Headnode ou des verrous SQLite), la boucle de traitement de l'agent intègre un gestionnaire d'exceptions global avec auto-nettoyage d'urgence. Toutes les opérations de libération physique (destruction de conteneur par isolation du PID hôte et déchargement de la VRAM d'Ollama) s'exécutent de façon inconditionnelle dans des blocs `finally` ou dans des daemons asynchrones de nettoyage, garantissant une remise à zéro matérielle propre en moins de 5 secondes.
# Principaux résultats

- **Status**: Operational & secured against zombie processes, featuring robust client/server log streaming heartbeats and auto-reconnection watchdogs (Last updated: 4 June 2026). Includes hardware-level VRAM purging and a clean codebase free from legacy debugging/testing artifacts.

# Documentation Index

| Title (Link) | Description |
|--------------|-------------|
| [Architecture Index](docs/index_architecture.md) | Architecture specifications and design notes |
| [Dashboard Index](docs/index_dashboard.md) | Spécifications du dashboard de monitoring premium et de l'explorateur d'artefacts bidirectionnel |
| [Pre-flight Index](docs/index_preflight.md) | Validation scanner and pre-commit logic |
| [Scheduler Index](docs/index_scheduler.md) | Résilience, réconciliation matérielle JIT (<5s VRAM purge), chaos-engineering et robustesse du scheduler |
| [Security Index](docs/index_security.md) | Sécurité, analyses de risques et failles connues |
| [Tasks Index](docs/index_tasks.md) | Index des spécifications et suivi des tâches de développement |
| [vLLM Index](docs/index_vllm.md) | Technical resolution of C++ ABI incompatibilities under NVIDIA NGC PyTorch containers |

# Plan du repo

```text
cluster-ci/
├── docs/           # Documentation, Index, and Task Specifications
├── install.sh      # Client-side installation script
├── scripts/        # Operational scripts (deployment)
└── src/            # Runner and Orchestrator scripts
    ├── cluster/    # Local runner setup and management (systemd)
    ├── runner/     # GitOps Orchestrator (run_research_pipeline.sh)
    └── scheduler/  # Headnode API, Worker Agent, and Persistence (SQLite)
```

# Scripts d'entrée principaux

| Command | Description |
|----------|-------------|
| `install.sh` | Injects the GitHub Actions workflow and `.cluster-ci` file into a client repository |
| `src/cluster/setup_runner.sh` | Installs and configures the GitHub Actions runner as a `systemd` service |
| `src/cluster/uninstall_runner.sh` | Completely uninstalls the runner (Systemd, GitHub, local) |

# Scripts exécutables secondaires & Utilitaires

| Command | Description |
|----------|-------------|
| `src/scheduler/submit_job.py` | Client-side script (CLI) to manually submit a job, track queue status with interactive dashboard & resource diagnostics |
| `src/scheduler/headnode_service.py` | Headnode HTTP API exposing scheduler routes (including public `/scheduler_status`) |
| `src/scheduler/runner_manager.py` | Manages the lifecycle of ephemeral GitHub Actions runners (slot1, slot2) |
| `update_cluster.sh` | Updates the Headnode and Workers via SSH, uses an `.env` file to store credentials |
| `scripts/get_worker_details.py` | Audit et collecte des caractéristiques matérielles et logicielles des workers distants via SSH |

# Roadmap

**Phase 1 (Foundation — Completed)**
- [x] [Orchestrator Runner Setup](docs/tasks/setup_orchestrator.md)
- [x] [Local Deployment & Runner Test](docs/tasks/deploy_local_cluster.md)
- [x] [Silent DVC Authentication](docs/tasks/dvc_auth.md)
- [x] [Client Installation Script](docs/tasks/client_script.md)
- [x] [Per-Repository Concurrency Management](docs/tasks/concurrency_management.md)

**Phase 2 (Reliability & UX — In Progress)**
- [x] Automated Deployment (`update_cluster.sh`) with E2E tests
- [x] Standard build configuration (`pyproject.toml`)
- [x] GitHub OAuth support for the Dashboard (with reverse proxy and IPv4 fallback support)
- [x] Dashboard UX improvement (date formatting, DVC path corrections under systemd, historical DVC run fixes)
- [x] Migration to Docker Worker execution (NVIDIA/ARM support)
- [x] Real-time Log Streaming via Headnode & Live Direct Terminal Stream (sans sous-terminal interactif ni perte de logs)
- [x] Résolution de la bufferisation GHA : Streaming direct en temps réel via watch natif GHA sans dépendance tmate/SSH
- [x] Propagation du jeton d'authentification (GH_TOKEN) de bout en bout en mode Délégation
- [x] Migration vers conteneur NGC moderne (Python 3.12, PyTorch 2.12, CUDA 13.2)
- [x] [Cluster-CI Pre-flight Scanner & Pre-commit Validator](https://github.com/UNIL-DESI/cluster-ci/issues/55)
- [x] [Auto-génération des contraintes ARM64 via CI](https://github.com/UNIL-DESI/cluster-ci/issues/56)
- [x] [Smart Environment Shims & Dynamic Client Sync](https://github.com/UNIL-DESI/cluster-ci/issues/57)
- [x] [Native GitHub Secrets Injection](https://github.com/UNIL-DESI/cluster-ci/issues/58)
- [x] [Isolation stricte des environnements Python et intégration GC](https://github.com/UNIL-DESI/cluster-ci/issues/59)
- [x] [Full Monitoring Dashboard & Real-time Logs](https://github.com/UNIL-DESI/cluster-ci/issues/60)
- [x] Smart Dependency Caching (hash-based skip of `uv pip install` when `pyproject.toml` unchanged)
- [x] Fix false-positive Exit Code -98 (Heartbeat/Worker crash detection race condition)
- [x] Résolution de l'échec du DVC P2P Pull (fichiers résiduels) dans le cache persistant
- [x] Résolution de l'erreur HTTP 404 du Live DVC Viewer derrière le reverse proxy (chemins relatifs & `<base href>`)
- [x] DVC Historical Extraction: Injection dynamique des identifiants (GITHUB_PAT) dans les miroirs Git locaux pour `dvc get`
- [x] Limitation de la prévisualisation des fichiers texte à 100 lignes dans le Dashboard pour optimisation UI
- [x] Restreindre le *Live Viewer* en mode "Lecture Seule" et corriger la détection des étapes DVC s'exécutant dans les wrappers Bash des workers.
- [x] [Robust Docker Container Lifecycle and Orphan Process Eradication](https://github.com/UNIL-DESI/cluster-ci/pull/66)
- [x] [Hybrid Liveness Watchdog — JIT Zombie Detection](https://github.com/UNIL-DESI/cluster-ci/pull/67)
- [x] Fix Scheduler assigning jobs to busy workers (single-threaded worker exclusion)
- [x] Inversion de l'ordre DVC/P2P (Pull avant le Hash) et suppression des erreurs de suppression Docker.
- [x] Segmented Pipeline Logs: Modal de logs interactif avec navigation par étape (Setup, DVC stages, Sync/GC), regroupement intelligent et déduplication robuste de toutes les exécutions de stages répétées, couleurs d'état cumulatives, lazy loading progressif, indicateur de lignes, copie presse-papier robuste (copiant l'intégralité de la section active avec fallback automatique pour les contextes HTTP non-sécurisés), bouton "Last Error" intelligent restreint à la fin de l'exécution ciblant la section en échec, animation de chargement pour l'étape en cours, et emoji ☠️ avec raison pour les jobs tués
- [x] Fix Bug: `submit_job.py` lisait `.cluster-ci` depuis le CWD cluster-ci au lieu du repo cible → RAM toujours à 2GB en mode Delegation. Correction via shallow clone du `.cluster-ci` distant.
- [x] Fix Bug: Jobs en `pending` infini en cas de demande de RAM dépassant la capacité physique des workers (fail-fast implémenté).
- [x] Fix Bug: `dvc-viewer` connection refused (port binding explicitement forcé sur 0.0.0.0 pour contourner l'isolation IPv6/loopback de Docker).
- [x] Fix Bug: UI Frontend affichait prématurément le label `Post-Run` au lieu de `System/Logs` durant le run de la pipeline.
- [x] Suppression de l'option obsolète `SHARED_MEMORY` (rendue inutile par `--ipc=host` qui alloue automatiquement 50% de la RAM hôte à `/dev/shm`) et ajout de la détection OOM en direct dans `submit_job.py` pour GitHub Actions.
- [x] Fix Bug: Ghost Workers — Le scheduler marque automatiquement les workers offline après 120s sans heartbeat, empêchant le dashboard de mentir sur l'état réel du cluster.
- [x] Hardening: Ajout de `timeout=10` explicite sur toutes les requêtes HTTP du worker agent pour prévenir les deadlocks TCP silencieux (firewall universitaire).
- [x] Support Windows Universel & PATH Automatique (PowerShell/CMD) : Détection et enregistrement automatique de `~/.local/bin` dans le PATH Windows User via PowerShell, wrappers natifs, résolution définitive du bug de figeage du terminal et fin de tâche instantanée dès la complétion du run.
- [x] Transparence de la file d'attente (Interactive Queue Dashboard) : Position dans la file d'attente, logs interactifs en direct des tâches occupantes par chercheur avec RAM/durée, et diagnostics automatisés de RAM physique insuffisante dans `submit_job.py`.
- [x] Homogénéisation complète inter-workers : Liaison inter-worker SSH RSA robuste sans mot de passe et synchronisation automatisée du cache des modèles Ollama (Gemma-4-31B de 20 Go) via rsync.
- [x] Fix Bug: Résolution définitive des Ghost Jobs via timeouts explicites et daemon thread de purge.
- [x] [Global Execution Timeout](docs/tasks/global_timeout.md) : Empêcher le gel du worker sur un job bloqué (arrêt Docker propre et notification chercheur).
- [x] [OOM cgroups silencieux : Ajouter un message d'erreur explicite lors du dépassement de REQUIRED_RAM](https://github.com/UNIL-DESI/cluster-ci/issues/91)
- [x] [Architecture : Implémenter un Watchdog Asynchrone pour la sauvegarde incrémentale de DVC](https://github.com/UNIL-DESI/cluster-ci/issues/92)
- [x] [Architecture : Implémenter le streaming en direct des logs pour les jobs asynchrones (cluster-run view)](https://github.com/UNIL-DESI/cluster-ci/issues/93)
- [x] [Garde-fous Systémiques : Prévention et éradication des processus orphelins et conteneurs zombies](https://github.com/UNIL-DESI/cluster-ci/issues/94)

**Phase 3 (Stability & Correctness — In Progress)**
- [x] [cluster-run CLI : Merge fantôme, affichage DAG inversé et fuite stdout Docker](https://github.com/UNIL-DESI/cluster-ci/issues/105)
- [x] [Orchestrateur : Fallback silencieux de HEADNODE_URL et absence de feedback réseau](https://github.com/UNIL-DESI/cluster-ci/issues/109)
- [x] [Docker : Conteneur bridé à 2 Go de RAM sur machine 128 Go (Fausse alerte)](https://github.com/UNIL-DESI/cluster-ci/issues/107)
- [x] [Fix(logs) : Streaming résilient, reconconnexion et résolution de la fausse erreur d'infrastructure en fin de job](https://github.com/UNIL-DESI/cluster-ci/issues/111)
- [🔄] [Runner DVC : Double exécution de stages et message de commit trompeur](https://github.com/UNIL-DESI/cluster-ci/issues/106)
- [ ] [Bugs Interface Web : Tri aléatoire des dates et heures dans l'Historique DVC](https://github.com/UNIL-DESI/cluster-ci/issues/101)
- [x] Fix Zombie Jobs : Guard branch-level dans le scheduler, cancellation headnode-aware dans `cluster-run`, auto-cancel dans la version déployée, et correction du crash HTTP 500 sur `/api/jobs/{id}/stop`
- [x] Fix Scheduling cluster-run : Annulation cross-repo par utilisateur pour les branches draft (un seul cluster-run par user, tous repos confondus), politique max 1 pending par repo+branche pour les branches normales, et affichage de la file d'attente avec raisons sur le dashboard web
- [x] Fix Dashboard : Scan multi-branches temps réel des artefacts (watchdog commits intermédiaires), correction timezone UTC +2h sur les temps écoulés, et ajout de la date de lancement sur les Active Cluster Runs
- [x] VRAM Tracking & Headnode-as-Worker : Détection automatique GPU/VRAM via `nvidia-smi`, contrainte `REQUIRED_VRAM` dans `.cluster-ci`, headnode enregistré comme worker dual-mode (scheduler + executor), affichage GPU/VRAM dans le dashboard et les diagnostics de file d'attente
- [x] Fix Runner : Masquage des erreurs anxiogènes (`Checkout failed`) de `dvc checkout` en mode Best-Effort
- [x] Fix Runner : Blocage infini du runner sur la phase sync après échec d'un stage (ajout de timeouts sur watchdog cleanup, git push/pull et docker exec sync)
- [x] Job Execution Timeout : Passage de la limite de temps de GitHub Actions de 6h à 24h (via `timeout-minutes: 1440` dans le workflow et dans `install.sh`)
- [ ] [Ordonnancement multi-GPU : Support multi-slot et isolation matérielle GPU](https://github.com/UNIL-DESI/cluster-ci/issues/110)

