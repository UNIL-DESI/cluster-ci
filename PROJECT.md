# Project: Cluster-CI Researchers Documentation Site

## Architecture
- Site de documentation statique généré via **MkDocs** avec le thème premium **mkdocs-material**.
- Déploiement automatisé hébergé sur **GitHub Pages** (branche `gh-pages`) via un workflow GitHub Actions (`.github/workflows/deploy-docs.yml`).
- Structure de navigation intuitive définie dans `mkdocs.yml` ciblant le dossier `docs/` existant du projet, avec un sous-dossier `docs/user/` dédié aux chercheurs pour ne pas interférer avec la documentation de développement interne existante.

## Code Layout
Le projet de documentation s'intègre dans l'arborescence existante :
```text
cluster-ci/
├── .github/
│   └── workflows/
│       └── deploy-docs.yml   # Workflow GitHub Actions de déploiement automatique
├── docs/                     # Dossier de documentation existant
│   ├── index.md              # Page d'accueil du site de doc
│   └── user/                 # NOUVEAU dossier de documentation chercheur
│       ├── onboarding.md     # Onboarding chercheur (organisation GH, SSH, etc.)
│       ├── client.md         # Guide d'utilisation du CLI cluster-run
│       ├── dvc.md            # Utilisation de DVC (P2P vs Git cache: false)
│       ├── ci_queue.md       # Architecture de la CI, shadow commits, file d'attente
│       ├── dashboard.md      # Dashboard de suivi temps réel et logs
│       └── support.md        # Support, bugs et contributions (issues GH)
└── mkdocs.yml                # Fichier de configuration de MkDocs et navigation
```

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | Investigation & Draft | Explorer analyse la base de code, les commandes, et rédige les maquettes/contenus de la doc. | None | DONE |
| 2 | Configuration & CI Setup | Worker configure `mkdocs.yml` et le workflow `.github/workflows/deploy-docs.yml`. | M1 | DONE |
| 3 | Documentation Writing | Worker rédige le contenu en anglais pour les chercheurs sous `docs/user/` et la page d'accueil `docs/index.md`. | M2 | DONE |
| 4 | Verification & Quality Gate | Reviewer et Challenger testent localement le build, valident la structure et la navigation, puis Forensic Auditor valide l'intégrité globale. | M3 | IN_PROGRESS |

## Interface Contracts
- **MkDocs CLI**: Le site doit compiler via la commande `uv run mkdocs build` (ou `python -m mkdocs build`).
- **GitHub Workflow Interface**: Le workflow `.github/workflows/deploy-docs.yml` doit se déclencher sur un `push` sur la branche `main` et disposer des permissions `contents: write` pour pouvoir pousser sur la branche `gh-pages`.
