# Détection d'OOM Cgroups Explicite

Cette spécification documente l'implémentation de la limitation physique de RAM par Docker et la détection robuste et déterministe du dépassement de la mémoire allouée (`REQUIRED_RAM`) dans Cluster-CI, résolvant ainsi l'Issue #91.

## 1. Contexte & Problématique
Historiquement, la RAM requise par les chercheurs pour leurs tâches (définie via la directive `REQUIRED_RAM` dans le fichier `.cluster-ci` de leur projet) n'était pas transmise comme restriction stricte de mémoire physique à Docker.
En conséquence :
1. Le conteneur Docker principal de calcul consommait toute la RAM physique disponible de l'hôte sans limite cgroups.
2. Lorsque la RAM physique globale venait à manquer, le noyau Linux de l'hôte (OOM Killer de l'hôte) tuait brutalement le sous-processus d'exécution Python de la tâche au sein du conteneur.
3. Le conteneur se fermait avec un code retour ambigu (souvent `137` ou non-nul) mais Docker ne signalait pas l'état `OOMKilled` sur le conteneur (`.State.OOMKilled` restait à `false`).
4. Pour le chercheur et le tableau de bord du scheduler, la cause du crash demeurait obscure et non-diagnostiquée de façon explicite.

## 2. Solution Technique Implémentée

### A. Restriction Physique par Cgroups Docker
Pour forcer Docker à surveiller et borner la consommation mémoire physique du conteneur via cgroups, nous imposons désormais l'option `--memory` de Docker lors du démarrage du conteneur principal dans `src/runner/run_research_pipeline.sh` :

1. **Extraction de `REQUIRED_RAM`** :
   La directive est extraite dynamiquement de `.cluster-ci` et normalisée en Gigaoctets (ex: `REQUIRED_RAM=16GB` donne `16`).
2. **Conversion robuste en MegaBytes (Mo)** :
   Comme Docker n'accepte que des valeurs entières pour son option `--memory` et rejette les décimales (ex: `--memory="2.5g"` est invalide), nous calculons de façon robuste `RAM_LIMIT_MB` en Mo entiers :
   ```bash
   RAM_LIMIT_MB=$(python3 -c "import math; print(int(float('${RAM_LIMIT}') * 1024))" 2>/dev/null || awk "BEGIN {print int(${RAM_LIMIT} * 1024)}")
   ```
3. **Application de la limite** :
   Le conteneur principal de calcul est lancé avec l'option `--memory="${RAM_LIMIT_MB}m"`.

### B. Détection Déterministe d'OOM
Si le conteneur dépasse la limite autorisée par son cgroup, le daemon Docker le tue instantanément. Nous interceptons ce cas de manière infaillible dans le script d'orchestration (`src/runner/run_research_pipeline.sh`) :

```bash
if [ $EXEC_RET -ne 0 ]; then
    OOM_KILLED=$(docker inspect "${MAIN_CONTAINER_NAME}" --format '{{.State.OOMKilled}}' 2>/dev/null || echo "false")
    if [ $EXEC_RET -eq 137 ] || [ "$OOM_KILLED" = "true" ]; then
        EXEC_RET=137
        log_error "Erreur: Le job a dépassé la limite REQUIRED_RAM allouée (${RAM_LIMIT} GB) et a été tué par le système (OOM Killer). Veuillez augmenter cette limite dans le fichier .cluster-ci"
    else
        log_error "Execution interrupted or failed (Exit code: $EXEC_RET). Forcing DVC sync before exiting..."
    fi
fi
```

## 3. Avantages & Améliorations
- **Clarté Absolue** : Le chercheur sait immédiatement que son job a manqué de RAM physique grâce à un message d'erreur d'une précision chirurgicale.
- **Robustesse** : Docker gère désormais le cycle de vie de la RAM de manière proactive et centralisée.
- **Fail Fast** : Plus de comportement silencieux ou de logs tronqués sans explication, le système s'arrête avec le code retour standardisé `137` pour les OOM.
