# Règles de l'Agent pour ce Répertoire

<!-- CLUSTER-CI:START -->
## ⚠️ Instructions Spécifiques pour la CI (Cluster-CI)

Ce dépôt est orchestré par le système hybride **Cluster-CI** sur un cluster ARM64 NVIDIA (GPU Blackwell GB10, 128GB RAM, Ubuntu 24.04).
En tant qu'agent autonome, tu DOIS respecter scrupuleusement les contraintes suivantes pour ne pas casser l'infrastructure distante et mener tes expériences avec succès :

1. **Version Python** : Le conteneur d'exécution utilise **Python 3.12**. Si tu utilises `pyproject.toml`, la directive `requires-python` doit include `3.12` (ex: `>=3.10`). Tu peux utiliser les dernières versions de toutes les bibliothèques.
2. **Dépendances GPU (PyTorch)** :
   - Le conteneur NGC (`nvcr.io/nvidia/pytorch:26.04-py3`) embarque déjà **PyTorch 2.12** avec **CUDA 13.2**.
   - Sur ARM64, le système utilise l'héritage natif `pip` : il ne télécharge que les dépendances manquantes et réutilise le `torch` pré-installé.
   - Ne spécifie **aucune contrainte de version stricte** (`==`) pour `torch`, `torchvision`, etc. Utilise `torch` sans version ou avec `>=2.0`.
3. **Exécution des Jobs & Pipeline (DVC + `.cluster-ci`)** :
   - NE MODIFIE PAS le fichier `.github/workflows/cluster-ci.yml`.
   - Les commandes shell natives (`pytest`, `uv run`, etc.) ne doivent pas être exécutées manuellement dans les workflows.
   - **Toute exécution (tests, entraînement, expériences) DOIT passer par un stage DVC** défini dans le fichier `dvc.yaml`.
   - **Comment déclarer ton expérience** :
     1. Définis un stage dans `dvc.yaml` contenant ta commande d'expérience. Exemple :
        ```yaml
        stages:
          my_experiment:
            cmd: python3 src/experiments/run_training.py --epochs 10
            deps:
              - src/experiments/run_training.py
            outs:
              - results/model.pt
        ```
     2. Pour cibler ce stage pour exécution, inscris son nom dans le fichier `.cluster-ci` à la racine (tu peux lister plusieurs stages séparés par des espaces ou retours à la ligne). Exemple :
        ```env
        STAGES=my_experiment
        MAX_RUNTIME_HOURS=24
        REQUIRED_RAM=16GB
        ```
     3. **Paramètres Obligatoires** : Tu DOIS définir `MAX_RUNTIME_HOURS` (max 24) dans `.cluster-ci`. Tu peux aussi définir `REQUIRED_RAM` (ex: `16GB`).
     4. **Interfaces Graphiques** : Pour déployer une interface interactive (Gradio, Streamlit, TensorBoard), ajoute `EXPOSED_PORT=<port>` dans `.cluster-ci`. Le système routera automatiquement le trafic vers ton application.
     5. **Secrets** : Tous les secrets définis dans "GitHub Repository Secrets" sont automatiquement transmis au cluster.
4. **Tests et Itération Rapide (CLI `cluster-run`)** :
   - En tant qu'agent, pour tester ton code ou lancer une expérience sur le cluster, **n'attends pas le déclenchement passif de GitHub**.
   - Utilise la commande locale `cluster-run` dans ton terminal.
   - **Guide d'utilisation de `cluster-run`** :
     - `cluster-run` : Pousse un shadow commit avec l'état actuel de ton espace de travail (y compris les fichiers non commités et fichiers untracked) vers une branche de draft temporaire. Cela va déclencher le runner, soumettre le job au scheduler du Headnode, puis **streamer en direct et en temps réel les logs d'exécution ligne par ligne dans ton terminal local**. C'est la commande principale pour itérer rapidement sans polluer l'historique git.
     - `cluster-run list` : Affiche l'historique et le statut (pending, running, completed, failed) des runs récents.
     - `cluster-run view [run_id]` : Affiche ou reprend le streaming des logs d'un run spécifié (ou du tout dernier run si aucun ID n'est fourni).
     - `cluster-run cancel [run_id]` : Interrompt proprement l'exécution d'un run en cours et nettoie la branche temporaire correspondante.
   - **Règle absolue** : Respecte toujours cette pipeline de soumission pour tester ou exécuter du code sur le cluster. N'essaie jamais de te connecter directement en SSH au Headnode ou aux Workers pour exécuter des scripts manuellement.
<!-- CLUSTER-CI:END -->
