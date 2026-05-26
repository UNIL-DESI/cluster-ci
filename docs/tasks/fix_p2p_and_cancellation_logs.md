# Correction de l'IP P2P Remote & Notification d'Annulation de Run

## 1. Contexte & Discussion (Narratif)
> *Suite aux retours de l'utilisateur, deux anomalies ont été identifiées dans le système Cluster-CI :*
- **Typo d'adresse IP du remote P2P** : Les logs de pipeline montraient une tentative de connexion à l'adresse IP invalide `1300.223.169.200` (contenant `1300` au lieu de `130`). Bien que l'IP configurée dans le fichier `.env` local soit correcte (`130.223.169.200`), le worker physique ou la configuration dynamique l'a propagée avec cette coquille. Afin de pallier ce problème de manière robuste, nous appliquons un filtre correcteur dynamique à toutes les étapes de récupération, de stockage ou de transmission de cette URL dans le scheduler, le loop d'ordonnancement, et les agents workers.
- **Log d'Information lors de l'Annulation** : Lorsque la soumission d'une nouvelle tâche déclenche l'annulation automatique d'une tâche précédente sur la même branche ou par le même utilisateur (mécanisme d'auto-cancellation), le nouveau run doit afficher clairement dès sa première ligne de log que la tâche précédente a été annulée. Nous allons transmettre l'ID de la tâche annulée à la nouvelle tâche via ses variables d'environnement (`CLUSTER_CANCELLED_RUNS`), puis le `worker_agent.py` inscrira de façon inconditionnelle cette notification d'information au tout début de son fichier de logs.

## 2. Fichiers Concernés
- `src/scheduler/headnode_service.py` : Correction du typo lors de la réception de `service_url` dans `/register_worker` et transmission des RUN_ID annulés via `env_vars` dans `/submit_job`.
- `remote_headnode_service.py` : Correction identique de `service_url` dans `/register_worker` par souci d'alignement de l'environnement de production.
- `src/scheduler/scheduler_loop.py` : Correction robuste de l'IP dans `service_url` lors de la construction de `p2p_url`.
- `remote_scheduler_loop.py` : Alignement identique pour la construction robuste de `p2p_url`.
- `src/scheduler/worker_agent.py` : Écriture de la ligne de log d'information au début de la tâche si `CLUSTER_CANCELLED_RUNS` est présente, et correction robuste de la variable d'environnement locale `SERVICE_URL`.

## 3. Objectifs (Definition of Done)
- [ ] **Correction de l'IP P2P** : Toute URL contenant `1300.223.169.200` est automatiquement nettoyée et remplacée par `130.223.169.200` de manière dynamique. Aucun log n'affiche plus l'IP incorrecte.
- [ ] **Notification de logs d'annulation** : Lorsqu'un run est écrasé/annulé par une nouvelle soumission, le fichier de log de la nouvelle tâche commence immédiatement par une ou plusieurs lignes formatées de la façon suivante :
  `[YYYY-MM-DD HH:MM:SS] ℹ️  Previous active run [RUN_ID] has been cancelled by this new submission.`
- [ ] **Intégrité de la base de données** : SQLite ne subit aucun lock concurrent et la persistance des données reste parfaitement intègre.
- [ ] **Documentation et Roadmap** : Le README.md et les index de tâches sont parfaitement mis à jour pour refléter la complétion de la tâche.
