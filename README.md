# Cluster CI

L'orchestrateur GitOps minimaliste et décentralisé pour le traitement de données et l'entraînement de modèles.
**État actuel** : Système opérationnel. Le réseau de workers ARM64 NVIDIA Blackwell est fonctionnel avec le conteneur NGC `nvcr.io/nvidia/pytorch:26.04-py3` (Python 3.12, PyTorch 2.12, CUDA 13.2). Support natif du streaming de logs en direct en temps réel ligne par ligne sans perte (via une attente active et un streaming linéaire direct pour éliminer tout bruit ANSI/tmux et prévenir les troncatures et coupures de mots parasites), support d'un tableau de bord interactif et transparent de la file d'attente pour les chercheurs (position de file d'attente, statut détaillé des tâches occupantes par chercheur avec RAM/durée, et diagnostics automatisés de RAM physique insuffisante), homogénéisation complète inter-workers (liaison SSH RSA et synchronisation automatique du cache de modèles), sauvegarde asynchrone optimisée des gros fichiers de données DVC via le Garbage Collector asynchrone (Lazy GC) pour préserver la bande passante réseau à chaque fin de job CI, **fiabilisation complète de la réplication de cache DVC P2P** (intégration systématique de `dvc-http` lors du bootstrap et mécanisme de repli intelligent *fallback pull* depuis le remote distant en cas d'échec du peer), système d'auto-annulation avancée par catégorie de branche (cluster-draft vs normales) prévenant les concurrences inutiles, Watchdog souverain centralisé garantissant l'application stricte des limites de temps d'exécution (timeouts) avec grâce de 5 minutes, mécanisme de retry robuste avec exponential backoff pour l'agent worker résistant aux micro-coupures de l'API Headnode pendant les phases de redémarrage/GitOps, auto-guérison complète de l'hôte via un mécanisme d'instance unique stricte (Single Instance Enforcement) couplé à une purge JIT (Just-In-Time) ultra-déterministe des conteneurs zombies et processus runners orphelins à l'instant précis du démarrage de chaque tâche et de l'agent, **système de réconciliation ultra-rapide des ressources physiques** (libération de la RAM physique et de la VRAM d'Ollama sur Blackwell en moins de 5 secondes) propageant les signaux d'annulation externes de bout en bout pour une efficacité et réactivité de file d'attente optimales, et **visualiseur DVC-Viewer historique déporté à la demande sur les Workers** (avec sélection intelligente basée sur la localité du cache DVC physique pour maximiser la fraîcheur des données, et auto-destruction Keep-Alive après 15 secondes d'inactivité) offrant une exploration performante et découplée des artefacts de recherche sans surcharge pour le Headnode.

Asynchronous continuous integration system for research pipelines, designed as a pull-based replacement for the legacy SlurmRay push-based architecture. This repository hosts the scripts necessary to configure a GitHub Actions Self-Hosted Runner on the target Ubuntu machine, orchestrating `uv run dvc repro` executions in local environments and managing silent authentication with Google Drive. It also provides the client script allowing any research repository to interface with this cluster.

## Cluster Hardware Specifications

| Property | Value |
|----------|-------|
| **GPU** | NVIDIA GB10 (Blackwell architecture) |
| **CPU** | ARM64 — Cortex-X925 + Cortex-A725 (ARMv9) |
| **RAM** | 128 GB unified memory |
| **OS** | Ubuntu 24.04.4 LTS (Noble Numbat) |
| **Docker Image** | `nvcr.io/nvidia/pytorch:26.04-py3` |
| **Python** | 3.12 |
| **PyTorch** | 2.12 (CUDA 13.2) |
| **Storage** | ~3.2 TB |

# Installation

### 1. Client Installation (Projet de recherche)

Exécutez cette commande à la racine de votre dépôt Git pour l'intégration automatique.
⚠️ **Utilisateurs Windows :** Cette commande télécharge et exécute un script Bash. Vous devez impérativement l'exécuter dans **Git Bash** ou **WSL** (elle échouera dans PowerShell ou CMD).

```bash
curl -H 'Cache-Control: no-cache, no-store' -sSL "https://raw.githubusercontent.com/UNIL-DESI/cluster-ci/main/install.sh?v=$(date +%s)" | bash
```

Ce script injecte :
1. Le workflow Github Actions (`.github/workflows/cluster-ci.yml`)
2. Le fichier de contrôle DVC (`.cluster-ci`)
3. **Le fichier de directives pour agents (`AGENTS.md`)** contenant les contraintes d'architecture du cluster (Python 3.12, PyTorch 2.12, CUDA 13.2) afin d'éviter les erreurs de dépendances de l'IA sur ce dépôt.
4. **Le Scanner Pre-flight (Git Hook)** : Un hook de pre-commit interactif qui valide la compatibilité locale avec le cluster ARM64 et propose des corrections automatiques.
5. **Le CLI `cluster-run`** : Commande locale pour soumettre et suivre des jobs directement depuis votre terminal (voir ci-dessous).

#### Commande `cluster-run`

La commande `cluster-run` est **100% compatible avec Windows (PowerShell/CMD), Linux et macOS**. Elle utilise le mécanisme de "Shadow Push" pour soumettre vos modifications locales (y compris les fichiers non commités et fichiers untracked) au cluster distant sans polluer votre historique git.

- **Sur Linux / macOS** : Après exécution du script `install.sh` ci-dessus, le binaire est disponible dans `~/.local/bin/cluster-run`.
- **Sur Windows (Natif)** : Les scripts wrappers `cluster-run.bat` et `cluster-run.ps1` sont directement disponibles à la racine de votre dépôt de recherche. Vous pouvez exécuter `.\cluster-run` sous PowerShell ou `cluster-run` sous CMD en toute transparence. Pour y accéder globalement, ajoutez simplement le dossier de votre dépôt à votre `PATH` Windows.

| Commande | Description |
|---|---|
| `cluster-run` | Soumet un job et streame en temps réel et en direct les logs d'exécution ligne par ligne dans votre terminal d'origine sans perte |
| `cluster-run list` | Liste les runs récents |
| `cluster-run view [run_id]` | Affiche les logs d'un run (dernier par défaut) |
| `cluster-run cancel [run_id]` | Annule un run et nettoie la branche |

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
    - `REQUIRED_RAM=16GB` : Contrainte de placement et limite stricte par cgroups Docker (défaut : 2GB).
    - `MAX_RUNTIME_HOURS=24` : Durée maximale d'exécution (**OBLIGATOIRE**, max 24h) pour éviter les processus zombies.
    - `EXPOSED_PORT=8501` : Active le routage vers une interface graphique (ex: Streamlit, Gradio) sur le port spécifié.
   Une fois alloué, le conteneur est bridé physiquement par cgroups Docker à la limite exacte déclarée par `REQUIRED_RAM` (convertie en Mo entiers), prévenant ainsi les OOM silencieux au niveau de l'hôte et garantissant une interception immédiate avec un message d'erreur clair et un code retour `137` en cas de dépassement.
8. **Résilience et Robustesse de l'Agent** : Pour éviter qu'un crash de thread n'isole un worker (problématique historique lors des micro-coupures réseau avec le Headnode ou des verrous SQLite), la boucle de traitement de l'agent intègre un gestionnaire d'exceptions global avec auto-nettoyage d'urgence. Toutes les opérations de libération physique (destruction de conteneur par isolation du PID hôte et déchargement de la VRAM d'Ollama) s'exécutent de façon inconditionnelle dans des blocs `finally` ou dans des daemons asynchrones de nettoyage, garantissant une remise à zéro matérielle propre en moins de 5 secondes.
9. **Simplification & Orchestration Dynamique (Inférence Adaptative)** : Afin de simplifier drastiquement l'écriture et l'entretien des pipelines de recherche DVC, le système supporte l'inférence dynamique basée sur les profils matériels physiques. Plutôt que de configurer en dur des surcharges de parallélisme (`OLLAMA_NUM_PARALLEL`) et de modèles directement dans le fichier `dvc.yaml` (qui variaient d'une machine ou d'un nœud à l'autre, provoquant des conflits et du gaspillage de ressources), les scripts d'évaluation exploitent la détection automatique du sweet-spot physique. En lisant le fichier de profil stable généré par les étapes de benchmark physique d'Ollama, le pipeline s'adapte en temps réel à l'infrastructure hôte (par exemple le GPU NVIDIA Blackwell GB10), maximisant le débit de calcul sans intervention humaine et sans risque d'OOM.
# Principaux résultats

- **Status**: Operational & secured against zombie processes (Last updated: 25 May 2026). Includes a hot-deployment GitOps protocol for zero-downtime cluster-wide updates and hardware-level VRAM purging.

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
├── scripts/        # Operational scripts (auto-update, deployment)
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
| `scripts/self_update_deferred.sh` | GitOps auto-update script (Pull & Defer pattern): pulls code, signals workers, schedules deferred headnode restart |
| `update_cluster.sh` | Updates the Headnode and Workers via SSH, uses an `.env` file to store credentials |
| `scripts/get_worker_details.py` | Audit et collecte des caractéristiques matérielles et logicielles des workers distants via SSH |

# Roadmap

### En cours

### À faire (par ordre d'exécution)

### Terminé
- [x] [#99 — Supprimer --background et garantir un streaming bloquant robuste dans cluster-run](https://github.com/UNIL-DESI/cluster-ci/issues/99) — ✅ Terminé (Merged PR #100)

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
- [x] Segmented Pipeline Logs: Modal de logs interactif avec navigation par étape (Setup, DVC stages, Sync/GC), couleurs d'état, lazy loading progressif, indicateur de lignes, copie presse-papier, bouton d'erreur rapide, animation de chargement pour l'étape en cours, et emoji ☠️ avec raison pour les jobs tués
- [x] Fix Bug: `submit_job.py` lisait `.cluster-ci` depuis le CWD cluster-ci au lieu du repo cible → RAM toujours à 2GB en mode Delegation. Correction via shallow clone du `.cluster-ci` distant.
- [x] Fix Bug: Jobs en `pending` infini en cas de demande de RAM dépassant la capacité physique des workers (fail-fast implémenté).
- [x] Fix Bug: `dvc-viewer` connection refused (port binding explicitement forcé sur 0.0.0.0 pour contourner l'isolation IPv6/loopback de Docker).
- [x] Fix Bug: UI Frontend affichait prématurément le label `Post-Run` au lieu de `System/Logs` durant le run de la pipeline.
- [x] Suppression de l'option obsolète `SHARED_MEMORY` (rendue inutile par `--ipc=host` qui alloue automatiquement 50% de la RAM hôte à `/dev/shm`) et ajout de la détection OOM en direct dans `submit_job.py` pour GitHub Actions.
- [x] Fix Bug: Ghost Workers — Le scheduler marque automatiquement les workers offline après 120s sans heartbeat, empêchant le dashboard de mentir sur l'état réel du cluster.
- [x] Hardening: Ajout de `timeout=10` explicite sur toutes les requêtes HTTP du worker agent pour prévenir les deadlocks TCP silencieux (firewall universitaire).
- [x] GitOps Auto-Update (Pull & Defer): Déploiement automatique du cluster sur merge vers `main` via workflow GitHub Actions + webhook `/webhook/update_self` sur les workers + restart différé des services headnode.
- [x] Fix Bug: Streaming des logs en direct — Résolution de la détection du `job_id` via le shadow commit hash transmis de bout en bout par GHA au scheduler dans la table `jobs` SQLite.
- [x] Fix Bug: Auto-Update — Fiabilisation du script `self_update_deferred.sh` pour cibler également le dossier de production global `/home/henri/cluster-ci`, les dépendances via `uv sync` et sa base de données lors des déploiements (ainsi que la normalisation universelle des fins de lignes en format LF).
- [x] Support Windows Universel & PATH Automatique (PowerShell/CMD) : Détection et enregistrement automatique de `~/.local/bin` dans le PATH Windows User via PowerShell, wrappers natifs, résolution définitive du bug de figeage du terminal et fin de tâche instantanée dès la complétion du run.
- [x] Transparence de la file d'attente (Interactive Queue Dashboard) : Position dans la file d'attente, logs interactifs en direct des tâches occupantes par chercheur avec RAM/durée, et diagnostics automatisés de RAM physique insuffisante dans `submit_job.py`.
- [x] Homogénéisation complète inter-workers : Liaison inter-worker SSH RSA robuste sans mot de passe et synchronisation automatisée du cache des modèles Ollama (Gemma-4-31B de 20 Go) via rsync.
- [x] Fix Bug: Résolution définitive des Ghost Jobs via timeouts explicites et daemon thread de purge.
- [x] [Global Execution Timeout](docs/tasks/global_timeout.md) : Empêcher le gel du worker sur un job bloqué (arrêt Docker propre et notification chercheur).
- [x] [OOM cgroups silencieux : Ajouter un message d'erreur explicite lors du dépassement de REQUIRED_RAM](https://github.com/UNIL-DESI/cluster-ci/issues/91)
- [x] [Architecture : Implémenter un Watchdog Asynchrone pour la sauvegarde incrémentale de DVC](https://github.com/UNIL-DESI/cluster-ci/issues/92)
- [x] [Architecture : Implémenter le streaming en direct des logs pour les jobs asynchrones (cluster-run view)](https://github.com/UNIL-DESI/cluster-ci/issues/93)
- [x] [Garde-fous Systémiques : Prévention et éradication des processus orphelins et conteneurs zombies](https://github.com/UNIL-DESI/cluster-ci/issues/94)
- [x] [Fix P2P and Cancellation Logs](docs/tasks/fix_p2p_and_cancellation_logs.md) : Correction de l'IP invalide P2P et ajout des notifications de log d'annulation.
- [x] [Optimize Git and DVC Sync](https://github.com/UNIL-DESI/cluster-ci/issues/96) : Optimisation de l'historique des commits intermédiaires Git et synchronisation DVC automatique et intelligente.
- [x] Sécurisation et optimisation de la récupération des artefacts récents (`api_latest_artifacts`) via l'historique Git local (`git ls-tree`), éliminant les freezes de l'interface.
- [x] Ajout d'un bouton d'accès direct premium "Historique DVC-Viewer" avec `e.stopPropagation()` sur chaque carte de projet du Dashboard.
- [x] Implémentation de la gestion stricte d'instance unique pour les processus DVC-Viewer locaux lors de l'accès à la route de visualisation.
- [x] DevOps : Déploiement et validation E2E en production de `dvc-viewer` avec support de l'argument `--host 127.0.0.1` et extinction automatique par heartbeat après 15 secondes d'inactivité.
- [x] [Feature: DVC-Viewer historique déporté en P2P sur les Workers](https://github.com/UNIL-DESI/cluster-ci/issues/97)
- [x] [Fix DVC lock issues and improve P2P pull logging](https://github.com/UNIL-DESI/cluster-ci/issues/98)
- [x] [Rescue et Robustesse du Runner Actions (SSH)](https://github.com/UNIL-DESI/llm-as-recommender/issues/65) : Durcissement du service systemd du runner permanent du Headnode (Restart=always, RestartSec=5, KillMode=process) et éradication complète des processus orphelins sur les slots de runners éphémères.





