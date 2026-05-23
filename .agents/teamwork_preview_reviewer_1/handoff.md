# Handoff Report

## 1. Observation
- J'ai vérifié le commit `249b987ff8f582fb35cd68921b3e2a26104f0a14`.
- La compilation de `src/scheduler/submit_job.py` et `src/scheduler/headnode_service.py` s'est déroulée avec succès (vérifié avec `python -m py_compile`).
- Aucun `git push` n'a été effectué sur le repository (le statut git indique que la branche `main` est en avance d'un commit par rapport à `origin/main`).
- Dans `submit_job.py`, la fonction `signal_handler` effectue des requêtes HTTP synchrones pour annuler la tâche :
  ```python
  resp = requests.get(f"{headnode_url}/job_status/{job_id}")
  # [...]
  requests.post(f"{headnode_url}/update_job_status", ...)
  ```
  Aucun paramètre `timeout` n'est défini pour ces appels vers `headnode_url`, bien qu'il y en ait un pour `worker_url` (`timeout=10`).
- Dans `headnode_service.py`, la fonction `periodic_clean_ghosts` utilise `clean_ghosts()` dans un `app.app_context()`, ce qui est compatible avec Flask 3.1.3 et fonctionne de manière asynchrone pour la base de données.

## 2. Logic Chain
- Un gestionnaire de signaux (`SIGTERM`, `SIGINT`) doit exécuter son code de nettoyage et rendre la main de façon déterministe et rapide.
- En Python, la librairie `requests` bloque indéfiniment si le serveur distant accepte la connexion TCP mais ne répond pas (ou en cas de paquets perdus), à moins qu'un argument `timeout` explicite ne soit fourni.
- Si le headnode devient lent ou inaccessible de manière silencieuse, le script `submit_job.py` restera bloqué à l'infini à l'intérieur du `signal_handler`.
- Conséquence critique : le runner CI (par ex. GitHub Actions) finira par envoyer un `SIGKILL` forcé après un timeout système, et le signal d'annulation (qui vient après) ne parviendra jamais au worker. Le job deviendra alors "fantôme" (ghost job) sur le cluster physique, allant à l'encontre même de l'objectif de la PR.

## 3. Caveats
- La logique de nettoyage en background (daemon thread) côté Headnode est correcte et robuste.
- La récupération d'erreur (try/except) entourant les appels réseau permet de continuer vers le `sys.exit`, mais seulement *si* l'appel réseau échoue explicitement, pas s'il se bloque indéfiniment.

## 4. Conclusion
**Verdict : VETO (REQUEST_CHANGES)**
- Le code introduit une faille de robustesse critique dans le processus d'annulation. Les appels réseau sans `timeout` dans un handler de signaux peuvent bloquer totalement la terminaison du script.
- **Action requise** : Ajouter un paramètre `timeout` strict (par exemple `timeout=5`) à **tous** les appels réseau `requests.get` et `requests.post` dans le script `submit_job.py`, en particulier dans `signal_handler` et dans la boucle de polling `wait_for_job`.

## 5. Verification Method
- Compilation : `python -m py_compile src/scheduler/submit_job.py src/scheduler/headnode_service.py`
- Pour reproduire la vulnérabilité du timeout : lancer `submit_job.py` vers un port local qui écoute mais ne répond jamais (`nc -l -p 5000`), puis envoyer un `SIGTERM` au script. On observe alors que le script se fige et ne quitte jamais.
