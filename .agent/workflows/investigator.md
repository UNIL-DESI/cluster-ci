---
alwaysApply: false
description: Enquêteur (Lecture Seule). Lance des sous-agents sur les problèmes complexes du review report pour formuler des hypothèses, sans corriger.
---

# Investigator Workflow

**Objectif** : Enrichir le `review_report.md` avec des hypothèses sur la cause des problèmes identifiés, sans jamais proposer de solution ni altérer le code.

> [!CAUTION]
> **🛑 LECTURE SEULE STRICTE.**
> Tu es un ENQUÊTEUR. Tu n'édites AUCUN fichier du projet.
> **🚫 AUCUNE SOLUTION.** Ne propose aucun correctif. Tes conclusions sont des hypothèses.

## 1. 📖 Préparation & Filtrage
1. Lis l'issue, le `review_report.md` (Reviewer), le `walkthrough.md` (Issue) et les **commentaires utilisateur** (à traiter en priorité).
2. **Filtre** : Sélectionne UNIQUEMENT les problèmes **complexes** (cause inconnue, solution non évidente). Ne lance pas d'enquête pour les bugs triviaux.

## 2. 🔍 Investigation (VIA SOUS-AGENTS)
Délègue l'investigation des problèmes complexes à des sous-agents. Tu es le COORDINATEUR.

1. **Lancement** : Lance un sous-agent en parallèle (`invoke_subagent TypeName="self"`) pour CHAQUE problème complexe.
2. **Supervision (Timeout 3 min, OBLIGATOIRE)** : Utilise `schedule` (DurationSeconds=180). À chaque réveil (toutes les 3 min), vérifie que tes agents ne sont pas bloqués. Relance-les ou questionne-les si besoin, puis relance un timer de 3 min.
3. **Agrégation** : Rassemble les retours (`send_message`) de chaque sous-agent.

**Prompt OBLIGATOIRE du Sous-Agent :**
```text
Tu es un Enquêteur. Mission : investiguer CE problème précis : [Symptôme + Logs].
🔒 LECTURE SEULE : Tu ne DOIS PAS et tu ne PEUX MÊME PAS éditer de fichiers. Tu n'as le droit QUE de consulter des fichiers et d'exécuter des commandes de diagnostic.
🎯 Objectifs :
1. Trace le flux d'exécution et cible la zone concernée.
2. Formule des HYPOTHÈSES (confiance haute/moyenne/basse) sur la source du problème.
3. Ne propose AUCUNE solution ni correctif.
Envoie ton rapport (hypothèses, contexte, fichiers clés) via send_message.
```

## 3. 📝 Enrichissement du Rapport (AJOUTS UNIQUEMENT)

> [!CAUTION]
> **🛑 LE TEXTE DU REVIEWER EST SACRÉ.**
> N'efface/modifie AUCUN mot du Reviewer. Tu AJOUTES tes blocs en dessous des défauts.

Dans l'artefact `review_report.md`, ajoute sous chaque défaut enquêté :
```markdown
### 🔎 Investigation
**Contexte/Fichiers** : [Zone concernée, flux tracé]
**Hypothèses** :
- 🟢 [Haute] : [Hypothèse la plus probable + Preuves]
- 🟡 [Moyenne] / 🔴 [Basse] : [Pistes alternatives]
```

*(Si l'utilisateur a laissé des commentaires, ajoute une section `## 💬 Réponses aux commentaires` et réponds à chacun).*

## 4. 🛑 Arrêt
1. Vérifie l'enrichissement du rapport et les réponses à l'utilisateur.
2. Fais un `remember` (AIVC).
3. **ARRÊTE-TOI**. L'Architecte prendra le relais.
