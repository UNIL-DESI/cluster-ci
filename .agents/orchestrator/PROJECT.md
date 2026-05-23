# Project: Résolution définitive des Ghost Jobs (cluster-ci)

## Architecture
- `trap TERM INT` / `janitor.py` (Investigation)
- Job scheduling and lifecycle management
- No push to remote, local commits only

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | Investigation | Analyze why `trap` and `janitor` failed | none | DONE |
| 2 | Implementation | Implement robust fix and commit locally | M1 | DONE |
| 3 | Documentation | Update README and docs | M2 | DONE |

## Interface Contracts
- `submit_job.py`: Handle SIGTERM gracefully without crashing on BrokenPipeError. Add timeouts (5 or 10s) to all network calls.
- `headnode_service.py`: Internal background thread to periodically invoke ghost jobs purge mechanism. Timeouts on proxy requests.

## Code Layout
Existing codebase
