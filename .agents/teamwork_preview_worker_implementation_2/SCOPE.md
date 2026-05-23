# Scope: Implementation Ghost Jobs Fix (Iteration 2)

## Architecture
- Fixes based on Reviewer VETO.
- `src/scheduler/submit_job.py`: The `requests.get` call inside `signal_handler` lacks a timeout, risking a permanent hang. Add `timeout=10` to this call (and any other missing timeouts in the signal handler).

## Constraints
- **STRICTEMENT AUCUN PUSH**. Commit local sur `main` seulement, en fusionnant avec le fix précédent ou en faisant un nouveau commit atomique.
- Rédige un commit message conforme à la règle "commit atomique" (en Anglais).
- "MANDATORY INTEGRITY WARNING": DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
