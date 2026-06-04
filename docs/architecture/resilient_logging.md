# Résilience du Streaming de Logs et Reconnexion Automatique

Ce document décrit le mécanisme de streaming de logs en direct depuis les workers distants du cluster vers le terminal local du chercheur, avec une tolérance élevée aux instabilités réseau et une reconnexion automatique.

## 1. Architecture du Pipeline de Logs (ppng.io)

La transmission de logs utilise un relais HTTP public unidirectionnel (`ppng.io`).
- **Serveur (Worker Cluster)** : Écrit les logs d'exécution de la pipeline DVC dans un fichier temporaire et envoie les nouvelles lignes en continu vers `ppng.io/<channel_id>` via une commande `curl -X POST`.
- **Client (Machine Locale)** : Effectue une requête `curl -X GET` sur `ppng.io/<channel_id>` pour recevoir et afficher le flux de logs ligne par ligne dans la console.

```
[Worker Exec] ---> [Log File] ---> [curl POST] ---> [ppng.io] ---> [curl GET] ---> [Terminal Chercheur]
```

## 2. Problématique des Connexions Zombies

`ppng.io` est un relais HTTP simple et sans état. Il couple de manière stricte une requête `POST` active avec une requête `GET` active.
Si la connexion réseau du client flanche temporairement :
1. Le client coupe sa connexion `GET` locale.
2. Le serveur (sur le cluster) n'est pas immédiatement notifié. Sa commande `curl POST` reste active en tant que connexion TCP zombie (le keepalive du noyau Linux peut maintenir la socket ouverte jusqu'à 13 minutes).
3. Le client tente de se reconnecter en ouvrant un nouveau `GET`.
4. `ppng.io` bloque le nouveau `GET` indéfiniment car il considère qu'il y a déjà un `POST` actif appairé avec un ancien canal mort.
5. Résultat : Le streaming de logs se fige de manière permanente et ne se reconnecte jamais spontanément, même lorsque le réseau revient.

## 3. Stratégie de Résilience Mutuelle (Serveur/Client)

Pour résoudre ce problème de manière structurelle sans dépendance à SSH ou aux APIs GitHub, un protocole de heartbeat + watchdog a été implémenté.

### A. Serveur (Worker) — Émission de Heartbeats et Timeout POST de Sécurité

Fichiers concernés : `src/runner/run_research_pipeline.sh`

1. **Heartbeat `♥`** : Un sous-processus daemon vérifie toutes les 10 secondes si le fichier de logs a grossi. Si aucune écriture n'a eu lieu, il injecte un caractère spécial heartbeat (`♥\n`) dans le flux de logs. Cela force un transfert de données actif sur la socket HTTP et évite l'inactivité.
2. **Timeout de sécurité POST `--max-time 300`** : La commande `curl -T` d'envoi du serveur est configurée pour expirer après **5 minutes**. Ce timeout ne devrait **jamais** se déclencher en fonctionnement normal (les heartbeats maintiennent le canal actif). Il existe uniquement comme filet de sécurité pour libérer le canal ppng.io si un POST TCP zombie persiste après un crash client.

### B. Client (Local) — Watchdog et Filtrage des Heartbeats

Fichiers concernés : `src/cluster/cluster_run.py`

1. **Filtrage des Heartbeats** : Le client intercepte le caractère `♥` du flux de logs. Il ne l'affiche pas dans la console et ne l'enregistre pas dans les fichiers de logs locaux pour garder des logs propres. Cependant, la réception du heartbeat remet à zéro l'horloge d'activité réseau du watchdog.
2. **Watchdog souverain de 60s** : Un thread de surveillance (Watchdog) s'exécute localement. Si aucune donnée (y compris les heartbeats) n'est reçue pendant plus de 60 secondes :
   - Le client considère la connexion comme réellement coupée (6+ heartbeats manqués).
   - Il termine proprement le processus `curl` en cours (avec drainage complet via `wait` et `join` du thread de lecture).
   - Il réinitie une nouvelle connexion `GET`.
3. **Détection du curl mort** : Si le processus curl se termine de manière inattendue (code retour, fermeture de connexion par ppng.io), le client vérifie si le run GHA est toujours actif avant de tenter une reconnexion avec un exponential backoff.

### C. Pourquoi `--max-time` court est dangereux

L'approche initiale utilisait `--max-time 30` pour forcer un recyclage rapide du POST. Cela s'est avéré catastrophique car :
- Chaque recyclage de POST cause une déconnexion visible côté client
- Le client interprète chaque déconnexion comme une instabilité réseau et augmente son délai de reconnexion (exponential backoff)
- Le replay complet du fichier de logs (`tail -c +1`) à chaque recyclage consomme une partie du budget temps du nouveau cycle
- Sur un réseau parfaitement stable, le streaming se brise de manière paradoxale

### D. Fenêtre Temporelle de Reconnexion (Worst-Case)

En cas de coupure réseau **réelle** suivie d'un retour à la normale :
- Le client détecte la perte de heartbeat après **60 secondes** maximum
- Le serveur libère le canal POST zombie après **5 minutes** maximum (via `--max-time 300`)
- La reconnexion effective est bornée à **~5 minutes** dans le pire cas (zombie TCP)

Sur un réseau stable, **aucune reconnexion ne devrait jamais avoir lieu** — les heartbeats maintiennent le canal actif indéfiniment.
