# Suite de Scénarios E2E & Audit Cluster-CI (Lot G)

Ce dossier héberge l'orchestrateur de recette en conditions réelles `e2e_scenarios.py` couvrant les 7 scénarios critiques du pipeline Cluster-CI.

> [!WARNING]
> Le placement forcé sur HEC45801/HEC45803 dans le `dvc.yaml` par défaut (`branch_b_step1` et `branch_b_step2`) bloque le run nominal si un des deux workers est hors ligne.
