# Règles de l'Agent pour ce Répertoire

<!-- CLUSTER-CI:START -->
## ⚠️ Cluster-CI — Instructions Agent

Ce dépôt utilise **Cluster-CI** : un cluster ARM64 NVIDIA (GPU Blackwell GB10, 128GB RAM, Ubuntu 24.04).

### 🚫 Règle n°1 — NE TOUCHE PAS à l'infrastructure cluster

Les dossiers et fichiers suivants sont gérés automatiquement par le cluster. **Ne les modifie pas, ne les débogue pas, n'essaie pas de les "réparer"** :
- `.cluster-ci-tools/`, `.cluster-ci-logs/`, `.dvc/`, `.dvc-viewer/`
- `.github/workflows/cluster-ci.yml`
- Tout script temporaire installé par le système (hooks, bootstrap, viewers, hashes)

Si tu vois des messages inhabituels dans les logs de `cluster-run` concernant ces composants (DVC viewer, hashes, mémoire, queues, allocation), **c'est normal** — ignore-les.

### 🐍 Environnement d'exécution

- **Python 3.12** — `requires-python` doit inclure `3.12` (ex: `>=3.10`).
- **PyTorch 2.12 + CUDA 13.2** pré-installés dans le conteneur NGC. Ne mets **aucune version fixe** (`==`) pour `torch`, `torchvision`, etc. Utilise `torch` sans version ou `>=2.0`.
- Tu peux utiliser les dernières versions de toutes les autres bibliothèques.

### 🔬 Pipeline DVC + `.cluster-ci`

Toute exécution (tests, entraînement, expériences) **DOIT passer par un stage DVC** dans `dvc.yaml`.

**Déclarer un stage** :
```yaml
stages:
  my_experiment:
    cmd: python3 src/run.py --epochs 10
    deps: [src/run.py]
    outs: [results/model.pt]
    metrics: [results/metrics.json: {cache: false}]
    plots: [results/plot.png: {cache: false}]
```

**Fichier `.cluster-ci`** (paramètres d'exécution) :
```env
REQUIRED_RAM=2GB
REQUIRED_VRAM=24GB
MAX_RUNTIME_HOURS=1
```
- `MAX_RUNTIME_HOURS` (max 24) : **obligatoire**.
- `REQUIRED_RAM` : contrainte de placement RAM (défaut: 2GB).
- `REQUIRED_VRAM` : contrainte de placement VRAM GPU (défaut: 0, pas de contrainte). Le scheduler n'assignera le job qu'à des workers disposant d'au moins cette quantité de VRAM.
- `EXPOSED_PORT=<port>` : pour interfaces web (Gradio, Streamlit, TensorBoard).
- `STAGES` : **laisser vide par défaut** → exécute toute la pipeline (`dvc repro`), optimal pour le debug progressif. Si besoin de cibler un sous-ensemble, indique uniquement le **dernier stage voulu** — DVC réexécutera automatiquement les dépendances nécessaires.
- Les secrets GitHub Repository sont automatiquement transmis au cluster.

### 📊 Outs/Deps vs Métriques/Plots — Deux circuits distincts

| | `deps` / `outs` | `metrics` / `plots` |
|---|---|---|
| **Stockage** | Peer-to-peer géré par le cluster | Synchronisés via Git |
| **À gérer ?** | Non — le cluster s'en charge | Oui — les déclarer avec `cache: false` |
| **Recommandation** | Déclare-les dans `dvc.yaml`, ne t'occupe pas de leur transfert | **Au moins 1 métrique + 1 plot par stage** pour le suivi |

**Conflits Git attendus** : Les métriques et plots sont automatiquement committés par le cluster à chaque nouveau résultat (commits sans déclenchement CI). Lors d'un `git pull`, des conflits sur ces fichiers sont **normaux et voulus**. Choisis simplement quelle version garder (résultats locaux via `cluster-run` ou résultats du dernier commit automatique).

### 🚀 CLI `cluster-run`

Pour tester/itérer sans attendre GitHub, utilise `cluster-run` dans ton terminal :
- `cluster-run` : pousse un shadow commit, déclenche l'exécution et **stream les logs en temps réel**.
- `cluster-run list` : statut des runs récents.
- `cluster-run view [id]` : reprend le streaming d'un run.
- `cluster-run cancel [id]` : annule un run en cours.

**Jamais de SSH direct** sur le cluster. Toujours passer par `cluster-run`.
<!-- CLUSTER-CI:END -->
