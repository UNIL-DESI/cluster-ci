# Optimisation de l'Historique Git et de la Synchronisation DVC

## 1. Contexte & Discussion (Narratif)
> *Inspire-toi du style "Handover" : Raconte pourquoi on fait ça.*
Lors de l'utilisation intensive du système `cluster-ci`, les chercheurs ont constaté que le runner créait un nombre excessif de commits intermédiaires et les poussait sur GitHub après chaque étape de DVC (e.g. `download_data`, `data_processing`, `training`). Ce comportement pollue considérablement l'historique Git des dépôts de recherche et induit une latence réseau importante à cause des poussées successives.
Par ailleurs, à la fin d'un run réussi de `cluster-run`, les chercheurs devaient exécuter manuellement un `git pull` local pour rapatrier le fichier `dvc.lock` mis à jour et les métriques/courbes générées par le run.

Nous avons décidé de rendre les commits intermédiaires purement locaux sur le nœud du cluster afin de conserver la possibilité de reprise sur incident (resume state) sans polluer le dépôt distant. Un unique `git push` est désormais exécuté à la fin d'un run réussi. En cas d'échec d'un stage, l'état d'erreur est immédiatement commité et poussé pour exporter les logs d'erreur. Côté client, nous automatisons de manière intelligente le rapatriement des fichiers via un merge transparent ou une suggestion claire en cas de workspace local non propre.

## 2. Fichiers Concernés
- `src/runner/dvc_iterative_repro.py`
- `src/runner/dvc_git_helper.py`
- `src/cluster/cluster_run.py`
- `scripts/cluster_run.py`
- `scripts/cluster-run.sh`
- `README.md`
- `docs/tasks/optimize_git_and_dvc_sync.md`

## 3. Objectifs (Definition of Done)
* **Pas de pushes intermédiaires** : Les commits d'étapes DVC réussies restent locaux sur le worker du cluster.
* **Push final unique** : Un unique `git push` robuste centralisé est déclenché en toute fin de pipeline réussie dans `dvc_git_helper.py`.
* **Push immédiat sur crash** : Si une étape échoue, l'état d'échec est immédiatement commité et poussé vers GitHub.
* **Synchronisation intelligente du client** : Le CLI `cluster-run` vérifie si l'espace de travail local est propre et effectue un merge propre pour conserver l'historique de la branche de draft. Si le workspace est modifié, il fait un fallback sur le checkout ciblé et affiche une suggestion de merge à l'utilisateur.
