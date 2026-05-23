# Résolution définitive des Ghost Jobs

## Contexte
Le système laissait des jobs à l'état "pending" de façon infinie lorsque ceux-ci étaient annulés via GitHub Actions. Bien qu'un mécanisme de protection (interception du `SIGTERM` et endpoint `/clean_ghosts`) ait été précédemment implémenté, des jobs fantômes (ghost jobs) continuaient d'apparaître.

## Investigation
L'investigation a mis en évidence deux problèmes majeurs :
1. **Crash du signal handler (`submit_job.py`)** : Le gestionnaire de signaux crachait prématurément avec une `BrokenPipeError` parce qu'il tentait d'écrire (via `print`) dans un pipe standard déjà fermé par l'OS suite à la mort abrupte de `tee` lors de la réception du signal d'annulation. En conséquence, la requête HTTP (`requests.post`) devant avertir le serveur de l'annulation n'était jamais envoyée.
2. **Endpoint orphelin (`headnode_service.py`)** : La fonction `clean_ghosts()` existait mais constituait du "code mort" car aucun cron ou service ne se chargeait de l'appeler.

## Résolution
Pour résoudre définitivement ce problème, deux correctifs ont été implémentés :
- **Priorisation et sécurisation des appels réseau** : Dans `submit_job.py`, les logs (`print`) et les appels réseaux ont été mis dans des blocs `try...except` séparés.
- **Timeouts explicites** : L'ensemble des appels réseau via la librairie `requests` (notamment dans la boucle de polling et le gestionnaire de signaux) dispose désormais d'un `timeout` explicite (5 ou 10 secondes) pour éviter un blocage infini (deadlock) de la CI si le réseau ne répond pas ou que le serveur cible "hang".
- **Daemon Thread de nettoyage** : Un `daemon thread` a été ajouté au démarrage de `headnode_service.py`. Celui-ci s'exécute en arrière-plan toutes les 60 secondes et déclenche automatiquement `clean_ghosts()` pour assainir la base de données.
