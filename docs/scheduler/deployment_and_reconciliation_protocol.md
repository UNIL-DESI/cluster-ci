# Protocole de Déploiement et d'Auto-Mise à jour Transparente (Zéro Interruption)

## 1. Contexte & Problématique
Dans le cadre de la résolution des problèmes de fuites de ressources physiques (RAM/VRAM) sur les workers NVIDIA Blackwell (HEC45803/HEC45801), des modifications majeures ont été apportées à l'orchestrateur `cluster-ci` (commits `0436a09` et `a94c62f`). 

Afin de déployer ces correctifs en production sur l'infrastructure physique du cluster sans interrompre ou corrompre le **Run de benchmark en cours** (`26393262605` sur le dépôt `llm-as-recommender`), ce document établit une analyse rigoureuse des services système et définit le protocole opérationnel pour une transition 100% transparente (zéro interruption).

---

## 2. Analyse de l'Architecture de Déploiement

L'orchestrateur `cluster-ci` est déployé sous forme de démons **systemd** s'exécutant dans l'espace utilisateur sur le Headnode et les Workers physiques.

```mermaid
graph TD
    subgraph Headnode ["Headnode (IP: 130.223.73.209)"]
        HS[cluster-scheduler.service<br>API Flask - Port 5000]
        SL[cluster-scheduler-loop.service<br>Boucle d'ordonnancement]
        RM[cluster-runner-manager.service<br>Gestionnaire GHA éphémères]
        DB[(cluster_scheduler.db<br>SQLite WAL)]
    end

    subgraph Workers ["Workers (Blackwell GB10)"]
        W1[cluster-worker.service<br>Worker 1 - Port 6000<br>Actif / Benchmark]
        W2[cluster-worker.service<br>Worker 2 - Port 6000<br>Inactif / IDLE]
    end

    HS --> DB
    SL --> DB
    W1 -- Poll & Heartbeat --> HS
    W2 -- Poll & Heartbeat --> HS
```

### Détail des Services Installés

| Service Systemd | Rôle | Fichier Script | Machine |
|---|---|---|---|
| `cluster-scheduler.service` | API REST centrale du scheduler | `src/scheduler/headnode_service.py` | Headnode |
| `cluster-scheduler-loop.service` | Boucle périodique d'assignation des jobs | `src/scheduler/scheduler_loop.py` | Headnode |
| `cluster-runner-manager.service` | Manager des runners GitHub Actions éphémères | `src/scheduler/runner_manager.py` | Headnode |
| `cluster-worker.service` | Agent local du worker (Flask + boucle d'exécution) | `src/scheduler/worker_agent.py` | Chaque Worker |

---

## 3. Évaluation des Risques de Redémarrage

### A. Redémarrage des Services du Headnode (Scheduler)
> [!NOTE]
> **NIVEAU DE RISQUE : MINIME / SANS DANGER**
- **Impact technique** : Le redémarrage de l'API Flask (`cluster-scheduler`) coupe brièvement les requêtes pendant moins de 2 secondes. La base SQLite (WAL) garantit la cohérence.
- **Résilience** : L'agent worker dispose d'un mécanisme de retry robuste avec backoff exponentiel pour toutes ses mises à jour de statut (`update_job_status` effectue 7 tentatives espacées sur plus d'une minute). Si l'API est indisponible momentanément, l'agent réessaie de manière transparente.
- **Indépendance d'exécution** : Le runner GHA sur le worker tourne de façon autonome dans son conteneur Docker et dialogue directement avec GitHub Actions pour son exécution de pipeline. Il n'est pas interrompu par un redémarrage temporaire du scheduler central.

### B. Redémarrage du Service Worker (`cluster-worker`)
> [!CAUTION]
> **NIVEAU DE RISQUE : CRITIQUE (Si effectué brutalement en cours de run)**
- **Impact technique** : Lors de l'arrêt du service `cluster-worker.service`, systemd envoie un signal `SIGTERM` au processus python `worker_agent.py`.
- **Comportement destructeur** : La méthode `cleanup_active_jobs_and_containers()` intercepte ce signal et déclenche instantanément le nettoyage forcé :
  1. Purge immédiate de la VRAM Ollama.
  2. Commande `docker rm -f cluster-job-<id>` et `cluster-viewer-<id>` pour détruire les conteneurs d'exécution.
  3. Destruction de l'arbre de processus du runner local.
  4. Notification d'échec du job au scheduler avec `exit_code = -15` (SIGTERM).
- **Conséquence** : Un redémarrage direct via `systemctl restart cluster-worker` sur le worker actif **tuera instantanément le benchmark en cours**.

---

## 4. Stratégie de Transition Transparente (Zéro Interruption)

Pour appliquer nos correctifs système immédiatement tout en protégeant le benchmark actif, nous exploitons l'endpoint GitOps intelligent de mise à jour à chaud (`/webhook/update_self`) implémenté dans le worker agent.

### Le Mécanisme `/webhook/update_self`
Lorsqu'il reçoit ce webhook, le worker effectue les opérations suivantes :
1. `git fetch origin main && git reset --hard origin/main` : Récupère le nouveau code source.
2. `uv sync` : Synchronise les dépendances.
3. **Vérification d'activité** : 
   - *Si aucun job n'est en cours* : Il planifie un redémarrage du démon systemd après 5 secondes (`_trigger_deferred_restart()`).
   - *Si un job tourne en ce moment* (le cas de notre benchmark) : Il positionne le drapeau `pending_update_restart = True` et **diffère le redémarrage**. Le benchmark continue de tourner normalement sans interruption.
4. **Application post-job** : À la fin de l'exécution du job, le hook de finalisation détecte le drapeau `pending_update_restart` et déclenche le redémarrage automatique à ce moment précis.

---

## 5. Protocole Opérationnel (Commandes à Exécuter)

Pour appliquer la mise à jour de façon 100% sécurisée, suivez scrupuleusement la séquence d'actions ci-dessous.

### Étape 1 : Mise à jour du Headnode (Sans Risque)
Connectez-vous au Headnode (`130.223.73.209`) en SSH et exécutez les commandes suivantes pour mettre à jour le scheduler :

```bash
# 1. Naviguer dans le dossier de production
cd /home/henri/cluster-ci

# 2. Mettre à jour le code source proprement (écarte les modifications locales éventuelles)
git fetch origin main
git reset --hard origin/main

# 3. Synchroniser l'environnement éditable et les dépendances avec uv
uv pip install -e .

# 4. Redémarrer les services du scheduler (le redémarrage prend ~1.5s, sans danger pour le job actif)
sudo systemctl restart cluster-scheduler
sudo systemctl restart cluster-scheduler-loop
sudo systemctl restart cluster-runner-manager

# 5. Valider le statut des services
sudo systemctl status cluster-scheduler cluster-scheduler-loop cluster-runner-manager --no-pager
```

### Étape 2 : Déploiement GitOps Différé sur les Workers (100% Sécurisé)
Au lieu de redémarrer manuellement les workers (ce qui tuerait le benchmark), déclenchez la mise à jour GitOps via l'API REST. Cette action peut être lancée depuis le Headnode ou n'importe quelle machine ayant accès aux IPs privées des workers :

```bash
# Mise à jour du Worker 1 (HEC45803/HEC45801 - Actif ou IDLE)
curl -X POST "http://130.223.170.123:6000/webhook/update_self" \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer VyrGjOvgDzuLHJHm4st0yh9yKIfUCbZS"

# Mise à jour du Worker 2 (Inactif)
curl -X POST "http://130.223.169.200:6000/webhook/update_self" \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer VyrGjOvgDzuLHJHm4st0yh9yKIfUCbZS"
```

### Résultats de la requête sur le Worker actif :
Le worker répondra immédiatement `HTTP 202 Accepted` avec le message suivant :
`{"status": "accepted", "message": "Update in progress, restart scheduled"}`

- **Sur le Worker inactif** : Le service se mettra à jour et redémarrera de lui-même en 5 secondes.
- **Sur le Worker actif** : Les scripts sur disque seront mis à jour immédiatement, mais le démon restera vivant pour terminer le benchmark `26393262605` sans perturbation. Une fois le benchmark finalisé, le redémarrage systemd s'effectuera automatiquement et appliquera le correctif de libération de RAM/VRAM en <5s pour toutes les exécutions futures.

---

## 6. Synthèse Comparative des Approches

| Critère | Approche Brutale (`systemctl restart` global) | Approche GitOps Transparente (Recommandée) |
|---|---|---|
| **Impact sur le Benchmark en cours** | ❌ **Destruction immédiate du Run** (Conteneur tué, échec CI) |  **Zéro Impact** (Le run continue jusqu'à son terme) |
| **Mise à jour du Code** | Immédiate | Immédiate (sur disque) |
| **Prise d'effet des Correctifs** | Immédiate | Différée à la fin du job pour le worker actif, Immédiate pour le reste |
| **Risque de corruption d'état DB** | Faible | **Nul** |
| **Complexité opérationnelle** | Très simple | Simple (Deux requêtes HTTP `curl`) |
