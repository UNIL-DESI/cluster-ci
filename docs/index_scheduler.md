# Index de Documentation - Scheduler

Cet index regroupe l'ensemble des notes techniques, guides d'architecture, et documentations relatives au scheduler et au système d'ordonnancement de **Cluster-CI v3**.

| Titre de la note | Courte Description | Dernière modif | Tag |
|------------------|-------------------|----------------|-----|
| [CI Pipeline & Queue Scheduling](user/ci_queue.md) | Ordonnancement multi-machines, machine prioritaire (home worker), équité fair-share aux frontières de nœuds et règles d'admission physique. | 2026-10-03 | `v3` |
| [Ressources par étape (`meta.cluster`)](user/stage_resources.md) | Spécification granulaire des ressources (CPU, RAM, VRAM, stockage, image, workers) dans `dvc.yaml`. | 2026-10-03 | `v3` |
| [Exécution Parallèle du DAG](user/parallel_execution.md) | Ordonnancement distribué des branches DAG, exécuteur de branche, volumes dédiés par image et pilote de fusion `dvc.lock`. | 2026-10-03 | `v3` |
| [Gestion de la Concurrence](concurrency_management.md) | Modèle de concurrence dual-mode (draft vs non-draft), propagation des signaux d'annulation et isolation multi-workers. | 2026-10-03 | `v3` |
| [Résilience et Chaos Testing](scheduler/resilience_and_chaos_testing.md) | Analyse des blocages historiques (SQLite deadlocks, Broken Pipes) et guide d'exécution du framework de stress-test. | 2026-05-24 | `Up to date` |
| [Réconciliation des ressources physiques et purge de la VRAM Ollama](scheduler/physical_resource_reconciliation.md) | Détails de la propagation réactive des annulations et de la purge active de la VRAM d'Ollama sur le host pour libérer le GPU en <5s. | 2026-05-25 | `Up to date` |
| [Protocole de Déploiement et de Mise à jour du Cluster](scheduler/deployment_and_reconciliation_protocol.md) | Protocole opérationnel de déploiement manuel sécurisé et d'évaluation des risques pour mettre à jour à chaud le cluster via le script update_cluster.sh. | 2026-06-01 | `Up to date` |
