# Global Execution Timeout

## 1. Contexte & Discussion (Narratif)
> *Inspiré du style Handover : Raconter pourquoi on fait ça.*
- Lors de l'exécution de pipelines de recherche complexes sur le cluster, des processus peuvent occasionnellement se figer indéfiniment (deadlocks de sockets réseau, boucles infinies de code de recherche, attente de ressource externe bloquée).
- Pour éviter le gel permanent d'un Worker (bloquant ainsi la file d'attente pour tous les autres chercheurs), il est indispensable d'instaurer une sécurité de fin de tâche globale et automatique.
- La discussion avec les chercheurs a mis en évidence le besoin de sécurité active sans intervention SSH manuelle.

## 2. Fichiers Concernés
- `src/scheduler/worker_agent.py`
- `src/scheduler/scheduler_loop.py`

## 3. Objectifs (Definition of Done)
* Décrire ce que l'on veut obtenir à la fin (High Level) :
  - Un mécanisme robuste de détection de dépassement de limite de temps globale (durée maximale spécifiée par le job ou limite par défaut du cluster).
  - Un arrêt propre et forcé du conteneur Docker et de tous ses processus enfants sur le Worker dès le franchissement de la limite.
  - La mise à jour automatique du statut du job dans la base de données Headnode à `failed` avec une raison explicite (`TimeoutReached`) pour que le chercheur sache précisément pourquoi son travail a été interrompu.
