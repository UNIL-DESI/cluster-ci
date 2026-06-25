# Original User Request

## Initial Request — 2026-05-23T22:56:46+02:00

Issue: Résolution définitive des Ghost Jobs dans cluster-ci

Working directory: C:\Users\Jamet\Documents\code\cluster-ci
Integrity mode: development

## Requirements

### R1. Investigation du problème récurrent
Il y a peu, nous avions mis en place un `trap TERM INT` dans le script bash et un `janitor.py` (ou endpoint `/clean_ghosts`) pour empêcher les jobs annulés par GitHub Actions de rester 'pending' à l'infini (ghost jobs). Or, le problème vient de se reproduire ! L'utilisateur indique que ce n'est absolument pas normal et que le problème de base n'est donc pas réglé. Analyse avec rigueur pourquoi la protection précédente a échoué (est-ce que le cron GitHub Actions ne tourne pas ? le trap bash n'est-il pas suffisant ?).

### R2. Implémentation d'une solution robuste
Mets en place un correctif définitif pour que les jobs soient correctement annulés ou purgés sans qu'une intervention manuelle soit requise. Modifie le code source du dépôt `cluster-ci` en conséquence.

### R3. STRICTEMENT AUCUN PUSH (BLOQUANT)
Attention : **NE FAIS AUCUN GIT PUSH**. Un push sur `cluster-ci` déclenche une mise à jour globale du cluster qui tue les jobs en cours d'exécution sur le dépôt principal `llm-as-recommender` ! Fais tes correctifs localement, commite-les sur la branche main de `cluster-ci`, mais **NE PUSH SURTOUT PAS**. L'orchestrateur s'occupera du push une fois le benchmark terminé de l'autre côté.

## Acceptance Criteria

### Functional
- [ ] La cause de la réapparition des ghost jobs est identifiée formellement.
- [ ] Un correctif définitif est codé et commité localement dans `cluster-ci`.

### Verification
- [ ] Fournir un rapport expliquant pourquoi le précédent fix a échoué et comment la nouvelle architecture résout le problème de manière garantie.

## Follow-up — 2026-06-25T12:39:19+02:00

Création d'un site de documentation de haute qualité sous forme de GitHub Pages pour le projet `cluster-ci`, destiné à guider les chercheurs sur le fonctionnement de la CI du cluster, l'installation du client, l'utilisation de DVC (graphes, métriques), et le fonctionnement général du système (files d'attente, etc.).

Working directory: c:\Users\hjamet\Documents\code\cluster-ci
Integrity mode: development

## Context

L'utilisateur (hjamet) est le créateur et l'administrateur principal de ce système de cluster-ci. La documentation s'adresse aux chercheurs utilisant ce cluster. Elle doit explicitement mentionner qu'en cas de bug, besoin d'amélioration ou suggestion, les chercheurs sont invités et encouragés à ouvrir des issues directement sur le dépôt GitHub officiel du projet.

## Requirements

### R1. Générateur de site et Thème
Mettre en place la configuration MkDocs avec le thème `mkdocs-material` dans le dépôt `cluster-ci`. Le site de documentation doit avoir un design moderne, épuré, avec des couleurs harmonieuses, et supporter le markdown natif.

### R2. Automatisation du déploiement (CI/CD)
Créer une GitHub Action dans `.github/workflows/deploy-docs.yml` permettant de compiler automatiquement le site MkDocs et de le déployer sur la branche `gh-pages` à chaque push sur la branche principale (`main`).

### R3. Contenu de la documentation pour les chercheurs
Rédiger une documentation technique claire et structurée en anglais (pour le contenu technique) couvrant :
- **Onboarding chercheur** : comment devenir membre de l'organisation GitHub, cloner le projet, configurer l'accès et les clés SSH.
- **Client `cluster-run`** : guide complet d'installation, configuration et utilisation des commandes clés (`cluster-run`, `cluster-run list`, `cluster-run view`, `cluster-run cancel`).
- **Écosystème DVC** : explication de l'intégration de DVC avec la CI, déclaration des stages dans `dvc.yaml`, et distinction cruciale entre `outs/deps` (stockés en P2P par le cluster) et `metrics/plots` (synchronisés par Git sans cache DVC via `cache: false`).
- **Fonctionnement de la CI et file d'attente** : cycle de vie d'un job, shadow commits, allocation dynamique des ressources (RAM, VRAM, GPU NVIDIA Blackwell GB10), gestion de la file d'attente et priorités du scheduler.
- **Dashboard / Site Web** : comment visualiser les runs, les graphes, et le monitoring des ressources en temps réel.
- **Support & Contribution** : invitation explicite aux chercheurs à ouvrir des issues pour tout bug ou suggestion d'amélioration, en rappelant que le système est conçu et géré par `hjamet`.

### R4. Indexation et Navigation
Organiser la navigation du site de manière intuitive via le fichier `mkdocs.yml` en indexant correctement les pages rédigées.

## Acceptance Criteria

### Génération & Style du site
- [ ] Le fichier `mkdocs.yml` est valide et configure correctement `mkdocs-material`.
- [ ] La documentation locale peut être générée sans erreur via `mkdocs build`.

### Déploiement GitHub Actions
- [ ] Le fichier `.github/workflows/deploy-docs.yml` est présent et configuré avec les permissions nécessaires pour écrire sur `gh-pages` (`contents: write`).

### Richesse du Contenu
- [ ] La documentation contient une section claire expliquant l'installation et l'usage de `cluster-run`.
- [ ] La documentation détaille la différence entre le stockage P2P (outs/deps) et la synchronisation Git (metrics/plots avec `cache: false`).
- [ ] La documentation décrit le cycle de vie des shadow commits et le scheduler du cluster.
- [ ] Une section invite clairement à ouvrir des issues GitHub pour signaler des bugs ou soumettre des améliorations.

## Verification Plan

### Automated Tests
- La commande `uv run mkdocs build` (ou équivalente via `python -m mkdocs build`) s'exécute avec succès dans le répertoire du projet.
- Validation syntaxique et structurelle des fichiers de configuration YAML (`mkdocs.yml` et le workflow GitHub).

### Manual Verification
- Vérification visuelle de la structure du site généré dans le dossier `site/` après exécution du build local.
