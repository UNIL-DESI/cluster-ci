# BRIEFING — 2026-05-23T21:01:21Z

## Mission
Investigate the "Ghost Jobs" issue where cancelled jobs stay 'pending' forever in cluster-ci, by analyzing the previous implementation (`trap TERM INT` and `janitor.py`) and the current source code.

## 🔒 My Identity
- Archetype: Explorer
- Roles: Read-only investigator, analyzing problems, synthesizing findings, producing structured reports.
- Working directory: C:\Users\Jamet\Documents\code\cluster-ci\.agents\teamwork_preview_explorer_investigation_1
- Original parent: 07ebcca8-ab94-44b8-b896-520a998970e4
- Milestone: M1 (Investigation of the ghost jobs)

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Use AIVC MCP tools only to query memory — NEVER run CLI shell commands for AIVC.
- Write chat & artifacts in French, code concepts in English.
- Do not modify source files.

## Current Parent
- Conversation ID: 07ebcca8-ab94-44b8-b896-520a998970e4
- Updated: 2026-05-23T21:01:21Z

## Investigation State
- **Explored paths**: `SCOPE.md`, `src/runner/run_research_pipeline.sh`, `src/scheduler/submit_job.py`, `src/scheduler/headnode_service.py`
- **Key findings**: 
  1. The `SIGTERM` trap in `submit_job.py` fails because it writes to a broken pipe (`tee` dies immediately), raising an unhandled `BrokenPipeError` that aborts the signal handler before it sends the HTTP cancellation request.
  2. The `/clean_ghosts` endpoint exists but is orphaned (no cron job, no internal background thread triggers it).
- **Unexplored areas**: None (issue formally identified and fix logic established).

## Key Decisions Made
- Investigated the Bash script trap and Python signal handling mechanism to find the `BrokenPipeError` race condition.
- Explored the whole repository to verify that `/clean_ghosts` is never invoked.
- Formulated a 5-component handoff report.

## Artifact Index
- C:\Users\Jamet\Documents\code\cluster-ci\.agents\teamwork_preview_explorer_investigation_1\handoff.md — Handoff report with the cause and proposed fix.
