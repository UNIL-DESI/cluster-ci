## 2026-06-25T11:43:30Z
Tu es un agent d'exploration (teamwork_preview_explorer). Ton dossier de travail est `.agents/explorer_enrichment/`.
Ta mission est d'analyser le codebase de `cluster-ci` pour identifier et analyser en détail les fonctionnalités techniques suivantes afin de préparer leur documentation (rédaction de brouillons en anglais) :

1. Dans le domaine DVC & Storage :
   - Emergency GC (50 Go) : comment fonctionne la détection de disk panic, le seuil de 50 Go, le nettoyage du cache et la suppression des workspaces inactifs sans backup.
   - Lazy Transfer (statut `sync_status`) : comment est géré et représenté le transfert paresseux.
   - Visualiseur historique de résultats DVC : comment il utilise un Git Worktree temporaire isolé, avec lien symbolique vers le cache et un mécanisme d'auto-suppression avec timeout de 30 minutes.

2. Dans le domaine Docker Containers :
   - Logiciel `smart_install.sh` : comment fonctionne le calcul du hash composite pour le bypass de build, la protection anti-shadowing de l'image NGC, le stub NVSHMEM et le patch de compatibilité CUDA pour bitsandbytes.

3. Dans le domaine CI & Queue Scheduler :
   - Watchdog GPU double seuil : comment fonctionnent la limite Soft de VRAM et la limite Hard de RAM à 90% avec support Grace Blackwell unifié.
   - Zombie GC : comment est détectée l'inactivité multi-dimensionnelle (CPU, GPU, log, réseau) sur un seuil de 10 minutes.
   - DVC Git Watchdog : comment il effectue des commits intermédiaires.

4. Dans le domaine Client (cluster-run) :
   - Redirection intelligente du CWD via `db.json`.
   - Validation post-run sync de type base-commune.
   - Auto-correction pre-commit interactive (`validate_pyproject.py`).

5. Dans le domaine Administration & Résilience :
   - RunnerManager : comment sont gérés les runners éphémères et comment est détectée la boucle de résiliation.
   - Pré-requis système : droits sudoers passwordless, watchdogs matériels systemd d'auto-reboot si gel pilote CUDA.
   - Mode maintenance : suspension des jobs via `/maintenance/on` et variables d'environnement d'infrastructure.

Instructions de travail :
- Utilise des outils de recherche de code (grep, find) pour localiser les fichiers correspondants.
- Rédige un rapport détaillé `handoff.md` dans ton dossier de travail `.agents/explorer_enrichment/` (en Français, avec les brouillons de documentation technique en Anglais) expliquant précisément le fonctionnement de chaque mécanisme identifié, les fichiers sources et lignes concernés, et proposant des paragraphes rédigés en anglais prêts à être intégrés dans la documentation.
- N'oublie pas d'indiquer ton état d'avancement régulier dans ton propre `progress.md` sous `.agents/explorer_enrichment/progress.md`.
- Une fois terminé, envoie un message récapitulatif à l'orchestrateur.
