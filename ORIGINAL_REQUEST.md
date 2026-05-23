# Original User Request

## Initial Request — 2026-05-23T22:56:46+02:00

Issue: Résolution définitive des Ghost Jobs dans cluster-ci

Working directory: C:\Users\Jamet\Documents\code\cluster-ci
Integrity mode: development

## Requirements

### R1. Investigation du problème récurrent
Il y a peu, nous avions mis en place un `trap TERM INT` dans le script bash et un `janitor.py` (ou endpoint `/clean_ghosts`) pour empêcher les jobs annulés par GitHub Actions de rester 'pending' à l'infini (ghost jobs). Or, le problème vient de se reproduire ! L'utilisateur indique que ce n'est absolument pas normal et que le problème de base n'est donc pas réglé. Analyse avec rigueur pourquoi la protection précédente a échoué (est-ce que le cron GitHub Actions ne tourne pas ? le trap bash n'est-il pas suffisant ?).

### R2. Implémentation d'une solution robuste
Mets en place un correctif définitif pour que les jobs soient correctement annulés ou purgés sans qu'une intervention manuelle soit requise. Modifie le code source du dépôt `cluster-ci` en conséquence.

### R3. STRICTEMENT AUCUN PUSH (BLOQUANT)
Attention : **NE FAIS AUCUN GIT PUSH**. Un push sur `cluster-ci` déclenche une mise à jour globale du cluster qui tue les jobs en cours d'exécution sur le dépôt principal `llm-as-recommender` ! Fais tes correctifs localement, commite-les sur la branche main de `cluster-ci`, mais **NE PUSH SURTOUT PAS**. L'orchestrateur s'occupera du push une fois le benchmark terminé de l'autre côté.

## Acceptance Criteria

### Functional
- [ ] La cause de la réapparition des ghost jobs est identifiée formellement.
- [ ] Un correctif définitif est codé et commité localement dans `cluster-ci`.

### Verification
- [ ] Fournir un rapport expliquant pourquoi le précédent fix a échoué et comment la nouvelle architecture résout le problème de manière garantie.
