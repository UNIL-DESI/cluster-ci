# Scope: Implementation Ghost Jobs Fix

## Architecture
Fixes based on Explorer report:
1. `src/scheduler/submit_job.py`: Move the `print()` statement inside a `try...except` block or execute the cancellation network call BEFORE printing, to avoid `BrokenPipeError` crashing the signal handler when `SIGTERM` is received.
2. `src/scheduler/headnode_service.py`: Add an internal daemon background thread (similar to `cleanup_inactive_viewers`) that runs a `while True` loop with a `time.sleep(60)` to periodically trigger the ghost jobs cleanup logic.

## Constraints
- **STRICTEMENT AUCUN PUSH**. Commit local sur `main` seulement.
- Rédige un commit message conforme à la règle "commit atomique" (en Anglais).
- "MANDATORY INTEGRITY WARNING": DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Dependencies
- L'analyse d'Explorer (chemin: C:\Users\Jamet\Documents\code\cluster-ci\.agents\teamwork_preview_explorer_investigation_1\handoff.md)
