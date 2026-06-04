# Résilience du Streaming de Logs et Reconnexion Automatique

Ce document décrit le mécanisme de streaming de logs en direct depuis les workers distants du cluster vers le terminal local du chercheur, avec une tolérance élevée aux instabilités réseau et une reconconnexion automatique instantanée.

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

Pour résoudre ce problème de manière structurelle sans dépendance à SSH ou aux APIs GitHub, un protocole d'inactivité et de cycles courts a été implémenté.

### A. Serveur (Worker) — Émission de Heartbeats et Timeout POST

Fichiers concernés : `src/runner/run_research_pipeline.sh`

1. **Heartbeat `♥`** : Un sous-processus daemon vérifie toutes les 10 secondes si le fichier de logs a grossi. Si aucune écriture n'a eu lieu, il injecte un caractère spécial heartbeat (`♥\n`) dans le flux de logs. Cela force un transfert de données actif sur la socket HTTP et évite l'inactivité.
2. **Timeout de cycle POST `--max-time 30`** : La commande `curl -T` d'envoi du serveur est configurée pour expirer impérativement toutes les 30 secondes. À chaque expiration, le serveur ferme son cycle POST et en démarre un nouveau à partir du dernier octet lu. Ce renouvellement fréquent brise les sockets zombies et permet au client de s'appairer à un nouveau cycle en moins de 30 secondes.

### B. Client (Local) — Watchdog Actif et Timeout de Vitesse

Fichiers concernés : `src/cluster/cluster_run.py`

1. **Timeout de vitesse `--speed-time 45 --speed-limit 1`** : Le client configure sa commande de récupération `curl` pour abandonner et s'arrêter automatiquement si la vitesse de transfert descend en dessous de 1 octet/s pendant plus de 45 secondes.
2. **Filtrage des Heartbeats** : Le client intercepte le caractère `♥` du flux de logs. Il ne l'affiche pas dans la console et ne l'enregistre pas dans les fichiers de logs locaux pour garder des logs propres. Cependant, la réception du heartbeat remet à zéro l'horloge de l'activité réseau.
3. **Watchdog souverain de 60s** : Un thread de surveillance (Watchdog) s'exécute localement. Si aucune donnée (y compris les heartbeats) n'est reçue pendant plus de 60 secondes :
   - Le client considère la connexion comme fluctuante ou coupée.
   - Il termine proprement le processus `curl` en cours (avec drainage complet via `wait` et `join` du thread de lecture).
   - Il réinitie une nouvelle connexion `GET`.

### C. Fenêtre Temporelle de Reconnexion (Worst-Case)

En cas de coupure réseau complète suivie d'un retour à la normale, le temps maximal nécessaire pour que les logs reprennent est borné à environ **75 secondes** :
$$\text{Temps de reconconnexion max} = \text{Client speed-time (45s)} + \text{Serveur max-time (30s)}$$
Cette approche élimine tout blocage infini et résiste aux micro-coupures ainsi qu'aux interruptions prolongées du réseau universitaire.
