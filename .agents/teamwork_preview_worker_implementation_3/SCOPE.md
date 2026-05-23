# Scope: Implementation Ghost Jobs Fix (Iteration 3)

## Architecture
- Fixes based on Reviewer VETO of Iteration 2.
- `src/scheduler/submit_job.py` and `src/scheduler/headnode_service.py`: To ensure absolute robustness, **every single** `requests.get`, `requests.post`, and `requests.request` call in these files must have a timeout (e.g. `timeout=10` or `timeout=5`).
- The Reviewer specifically flagged line 226 in `submit_job.py` and others in `headnode_service.py` (like the proxy proxy logic around line 1058).

## Constraints
- **STRICTEMENT AUCUN PUSH**. Commit local sur `main` seulement.
- Rédige un commit message conforme à la règle "commit atomique" (en Anglais).
- "MANDATORY INTEGRITY WARNING": DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
