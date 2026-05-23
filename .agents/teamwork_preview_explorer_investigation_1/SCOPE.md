# Scope: Investigation Ghost Jobs

## Architecture
- Analyse du mécanisme de protection actuel : `trap TERM INT` et `janitor.py`.
- Recherche dans les mémoires AIVC (mcp_aivc) pour retrouver l'historique de cette implémentation.
- Examen du workflow GitHub Actions et du scheduling sur le cluster ARM64.

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | M1 | Investigation of the ghost jobs | none | IN_PROGRESS |

## Interface Contracts
- Produire un rapport d'analyse dans `handoff.md` expliquant la cause exacte de la réapparition des ghost jobs et une proposition de fix robuste (sans code).
