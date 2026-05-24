# Prévention des Processus Orphelins dvc-viewer

Ce document détaille la stratégie et les mécanismes d'éradication des processus orphelins (zombies) `dvc-viewer` sur les workers du cluster.

## 1. Contexte & Problématique

Le `dvc-viewer` est un composant interactif démarré à la demande pour chaque job DVC afin de permettre la visualisation des métriques et des résultats de recherche. Cependant, plusieurs facteurs provoquaient l'accumulation de ces processus en tâche de fond sur les workers :
1. **Partage d'espace de nommage de PID Docker fragile** : L'option `--pid="container:${MAIN_CONTAINER_NAME}"` entraînait des conflits de destruction et empêchait l'arrêt propre du conteneur de viewer lorsque le conteneur principal se terminait brutalement.
2. **Filtrage de chemin pwdx inopérant** : Le script de nettoyage initial utilisait `pwdx "$pid"` sur l'hôte en le comparant avec le dossier de travail interne du conteneur `/workspace`, ce qui échouait systématiquement à identifier les processus orphelins du conteneur s'exécutant sur l'hôte.
3. **Cancellations abruptes de jobs** : L'utilisation de `psutil` pour tuer l'arbre de processus lors d'une annulation de job court-circuitait les gestionnaires de signaux Bash (`trap EXIT`), empêchant ainsi l'exécution des routines de nettoyage classiques.

## 2. Solutions Appliquées

### A. Suppression de la dépendance de PID Docker
Nous avons retiré l'option `--pid="container:${MAIN_CONTAINER_NAME}"` lors du démarrage du conteneur `dvc-viewer`. Ce conteneur n'a besoin que d'un accès en lecture au volume partagé du workspace pour lire les métriques DVC, et non de partager l'espace de nommage PID du conteneur d'exécution. Cela permet au conteneur de viewer d'être géré et détruit de manière totalement isolée et robuste.

### B. Purge proactive et globale sur l'hôte
Puisqu'un worker n'exécute qu'un seul job de recherche à la fois, tout processus `dvc-viewer` résiduel détecté sur l'hôte au démarrage ou à la fin d'un job est par définition un orphelin d'une exécution précédente. 
Nous avons remplacé la vérification restrictive `pwdx` par un balayage proactif global sur l'hôte :
- **Au démarrage du job** : Nettoyage systématique de tout processus `dvc-viewer` résiduel.
- **Pendant le nettoyage Bash (`cleanup_job_resources`)** : Cible explicitement le port du viewer (`--port $VIEWER_PORT`) et force le signal `kill -9` sur tout processus viewer résiduel.

### C. Watchdog d'Agent Worker et GC Hybride (`psutil`)
Pour contrer les cas où Bash est brutalement interrompu (ex: annulation via le Headnode), nous avons doté l'Agent Worker et le Zombie GC de routines actives en Python via `psutil` :
1. **Worker Agent** :
   - `kill_dvc_viewer_processes()` scanne les processus de l'hôte et élimine impérativement tout binaire ou commande contenant `dvc-viewer`.
   - Exécuté systématiquement au démarrage de `execute_job`, dans le bloc `finally` de fin d'exécution de job (que le job soit en succès, en échec ou expiré), ainsi que dans la route de cancellation du job (`/cancel/<job_id>`).
2. **Zombie GC Watchdog (`gc_orchestrator.py`)** :
   - Scan et élimination active de tout processus `dvc-viewer` sur l'hôte si aucun conteneur de job n'est actif sur le worker, assurant une hygiène absolue et continue des ressources.

## 3. Garde-fous Systémiques Avancés & Auto-Guérison (Issue #94)

Pour éradiquer définitivement les processus orphelins (`worker_agent.py` manuels) et les conteneurs Docker enfants qui continuent de s'exécuter indéfiniment après l'arrêt d'un job, nous avons implémenté trois garde-fous systèmes majeurs en mai 2026.

### A. Verrou d'Instance Unique (Single Instance Lock)
Afin d'éviter tout conflit de sockets, de ports (ex. port 6000) et de versions entre plusieurs instances de `worker_agent.py` s'exécutant simultanément (ex: démarrage automatique par systemd et lancement manuel par un chercheur), un verrouillage exclusif physique a été mis en place :
- **Mécanisme** : L'agent tente d'acquérir de manière bloquante un verrou d'écriture exclusif sur un fichier lock physique localisé dans le répertoire temporaire de l'OS (`/tmp/cluster-worker.lock` sous Linux / macOS, ou `TEMP/cluster-worker.lock` sous Windows).
- **Implémentation multi-plateforme** : Utilisation du module standard `fcntl` (`flock`) pour les systèmes UNIX, et bascule transparente vers `msvcrt` pour Windows.
- **Fail-Fast** : Si une autre instance de l'agent détient déjà le verrou, la nouvelle instance s'arrête instantanément avec un message d'erreur clair et descriptif dans les logs, empêchant toute interférence chaotique.

### B. Graceful Shutdown & Signal Trap
Les redémarrages brutaux de service (via systemd ou par l'administrateur) provoquaient auparavant la déconnexion instantanée de l'agent principal, laissant les conteneurs Docker enfants (`cluster-job-*` et `cluster-viewer-*`) s'exécuter à l'infini.
- **Mécanisme** : Capture et gestion propre et thread-safe des signaux système d'arrêt (`SIGTERM`, `SIGINT`, et `SIGHUP` s'il est supporté).
- **Routines de nettoyage** : Dès réception d'un signal, l'agent déclenche immédiatement :
  1. **Purge des Conteneurs** : Destruction forcée et immédiate de tous les conteneurs Docker associés au job en cours (`docker rm -f`).
  2. **Arrêt du Processus Local** : Destruction de l'arbre complet du processus runner local (`psutil.Process.kill()`).
  3. **Notification Réseau** : Envoi immédiat d'une mise à jour de statut `failed` (code de sortie `-15` correspondant au signal) au Headnode pour libérer proprement le slot en base de données.
  4. **Libération du Verrou** : Libération propre du verrou physique et suppression du fichier lock.

### C. Self-Healing Daemon (Routine d'Auto-Guérison Active)
Pour garantir une sécurité ultime sans reposer uniquement sur la bonne communication réseau de l'agent, un daemon d'auto-guérison s'exécute de façon périodique et autonome en tâche de fond sur chaque worker :
- **Fréquence** : Audit complet toutes les 60 secondes.
- **Algorithme d'Auto-Guérison** :
  1. **Détection** : L'agent interroge l'API Docker locale pour lister tous les conteneurs actifs commençant par les préfixes `cluster-job-` et `cluster-viewer-`.
  2. **Vérification de l'activité locale** : Si un conteneur correspond au job actif actuellement exécuté par le worker local, il est préservé.
  3. **Vérification Réseau (Headnode)** : Pour tout autre conteneur (orphelin d'une ancienne session ou d'une interruption), l'agent appelle le Headnode (`/job_status/{job_id}`).
     - Si le Headnode retourne un statut terminal (`completed` ou `failed`), ou un statut `404` (job supprimé ou inexistant), le conteneur local est identifié comme un **zombie** et est immédiatement détruit de force.
     - Si le Headnode retourne un statut `pending` ou `assigned` mais affecté à un autre worker (`worker_id` différent), le conteneur local est également détruit de force.
     - **Fail-Safe Réseau** : Si le Headnode est temporairement injoignable (coupure réseau, maintenance), aucun conteneur n'est supprimé afin d'éviter les faux positifs destructeurs.
  4. **Limite Absolue de Durée de Vie** : Tout conteneur de job s'exécutant sur l'hôte depuis plus de 24 heures (mesuré via sa date de création extraite par `docker inspect`) est considéré comme expiré et est auto-détruit immédiatement de manière autonome par le worker, sans attendre d'ordre externe.
