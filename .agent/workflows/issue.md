---
alwaysApply: false
description: Artisan implémenteur. Prend la première issue, l'implémente, produit un artefact walkthrough et s'arrête.
---

# Issue Workflow

**Objectif** : Implémenter l'issue la plus urgente de A à Z.

> **📦 TU ES UN ARTISAN.** Ton livrable doit être propre et vérifié basiquement.
> **🚫 AUCUNE EXÉCUTION LONGUE.** Tu ne DOIS JAMAIS exécuter de commandes longues (pipelines, entraînements, runs complexes, benchmarks, etc.). L'exécution en conditions réelles est le job EXCLUSIF des Reviewers. Toi, tu ne fais que t'assurer que ton code ne leur fera pas perdre leur temps : compilation OK, syntaxe OK, imports OK. Prépare la commande d'exécution et consigne-la dans le walkthrough pour les Reviewers.
> **🚫 PAS DE SOUS-AGENTS.** Tu fais le travail et tu t'arrêtes. Le Reviewer prendra le relais ensuite.

## 1. 🔍 Démarrage
1. Lis la Roadmap (`README.md`).
2. Prends la 1ère issue `⬚ À faire` dont les dépendances sont `✅ Terminée`.
3. Lis l'issue GitHub complète (Context, Goals).
4. Ajoute le label `in-progress` sur GitHub. Mets la Roadmap à `🔄 En cours`.

## 2. 🧠 Contexte & Plan
1. **AIVC** : `get_recent_memories`, `recall` (≥3 queries), `consult_file`.
2. Produis un court `implementation_plan.md` avec un encart `> [!IMPORTANT]` expliquant l'objectif en français.

## 3. 🛠️ Implémentation
- Respecte les conventions. Commits atomiques.
- **Vérifications rapides UNIQUEMENT** (tout doit prendre <30 secondes) :
  - ✅ Compilation / syntaxe (`python -c "import mon_module"`, `tsc --noEmit`, etc.)
  - ✅ Imports corrects, pas d'erreurs évidentes
  - ✅ Tests unitaires ultra-rapides s'ils existent déjà
  - ✅ Linting basique
  - ❌ Pipelines, entraînements, benchmarks, exécutions complètes
  - ❌ Toute commande qui prend plus de 30 secondes
- **Ton objectif** : t'assurer que le code ne fera PAS perdre leur temps aux Reviewers avec des erreurs triviales. Tu ne fais PAS le travail des Reviewers. Tu prépares un livrable propre qu'ils pourront tester en conditions réelles.

## 4. 📝 Livrable (Walkthrough)
Crée un **artefact** `walkthrough.md` (via le système d'artefacts, PAS un fichier physique dans le repo) contenant :
1. Titre et lien de l'issue.
2. Résumé des changements.
3. Commandes exactes pour tester l'implémentation (pour le prochain agent Reviewer).

Cet artefact sera partagé automatiquement avec le Reviewer qui prendra le relais.

## 5. 🛑 Arrêt
1. Rapporte tes actions dans le chat.
2. Fais un `remember` dans AIVC.
3. **ARRÊTE-TOI**. Ne ferme PAS l'issue. Demande à l'utilisateur d'invoquer le Reviewer.
